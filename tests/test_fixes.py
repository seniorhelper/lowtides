"""Regression tests for the issues found in the independent code review."""
import http.client as httpclient
import json
import os
import unittest

import fakes
from helpers import agent, chat, config, engine
from test_http import ServerCase, http

from lowtides.providers import ProviderError


class StubProviders:
    """Stands in for Providers so the engine's decisions can be tested directly."""

    def __init__(self, behave):
        self.behave = behave
        self.calls = []

    def call(self, model, body, api, extra_headers=None):
        self.calls.append((model, body, extra_headers))
        out = self.behave(model, body)
        if isinstance(out, Exception):
            raise out
        text = out
        return {"response": {"choices": [{"message": {"role": "assistant", "content": text}}]}, "text": text,
                "usage": {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0},
                "usage_estimated": False, "tool_calls": False}


def stub_engine(behave, **policy):
    from lowtides.engine import Engine
    from lowtides.store import Store
    store = Store(":memory:")
    stub = StubProviders(behave)
    return Engine(config(**policy), store, stub), store, stub


GOOD = lambda m, b: "A complete answer from %s with enough detail to pass the checks." % m  # noqa: E731


class TestAnswerMemorySafety(unittest.TestCase):
    SYSTEM = "You are the support agent for Acme. " + " ".join("Rule %d: be polite and accurate." % i for i in range(40))

    def _ask(self, eng, a, q, model="sim-large", system=None):
        body = {"model": model, "max_tokens": 200,
                "messages": [{"role": "system", "content": system or self.SYSTEM}, {"role": "user", "content": q}]}
        return eng.handle("openai", body, a["key"])

    def test_shared_system_prompt_does_not_make_questions_match(self):
        eng, store = engine()
        a = agent(store)
        self._ask(eng, a, "Cancel my order please")
        _, _, h = self._ask(eng, a, "What is your refund policy for laptops?")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")

    def test_symbols_and_numbers_matter(self):
        eng, store = engine()
        a = agent(store)
        self._ask(eng, a, "Is 5 > 3 true or false?")
        _, _, h = self._ask(eng, a, "Is 5 < 3 true or false?")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")
        self._ask(eng, a, "What is the status of order 1234 right now")
        _, _, h = self._ask(eng, a, "What is the status of order 1235 right now")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")

    def test_trivial_rephrase_still_matches(self):
        eng, store = engine()
        a = agent(store)
        self._ask(eng, a, "What are your store hours today?")
        _, _, h = self._ask(eng, a, "what are your store hours today")
        self.assertEqual(h["X-Lowtides-Model-Used"], "answer memory")
        self.assertIn("near match", store.ledger()[0]["note"])

    def test_different_requested_model_or_system_never_matches(self):
        eng, store = engine()
        a = agent(store)
        self._ask(eng, a, "What are your store hours today?", model="sim-large")
        _, _, h = self._ask(eng, a, "what are your store hours today", model="sim-medium")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")
        _, _, h = self._ask(eng, a, "what are your store hours today", system="You work for a different shop.")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")

    def test_long_question_never_near_matches(self):
        eng, store = engine()
        a = agent(store)
        doc = "Clause %d applies. " * 40
        self._ask(eng, a, "Summarize: " + (doc % tuple(range(40))) + " The buyer may cancel.")
        _, _, h = self._ask(eng, a, "Summarize: " + (doc % tuple(range(40))) + " The buyer may not cancel.")
        self.assertNotEqual(h["X-Lowtides-Model-Used"], "answer memory")

    def test_stopped_agent_gets_no_free_answers(self):
        eng, store = engine()
        a = agent(store)
        eng.handle("openai", chat("Translate to Spanish: thank you"), a["key"])
        store.update_agent(a["id"], status="stopped")
        status, body, h = eng.handle("openai", chat("Translate to Spanish: thank you"), a["key"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "agent_stopped")


class TestEngineFixes(unittest.TestCase):
    def test_cheaper_model_400_climbs_instead_of_failing(self):
        def behave(m, b):
            if m != "sim-large":
                return ProviderError("Provider returned HTTP 400: unsupported parameter.", 400, False)
            return GOOD(m, b)
        eng, store, stub = stub_engine(behave)
        a = agent(store)
        status, _, h = eng.handle("openai", chat("Translate: hello"), a["key"])
        self.assertEqual(status, 200)
        self.assertEqual(h["X-Lowtides-Model-Used"], "sim-large")
        self.assertIn("failed", store.ledger()[0]["note"])

    def test_400_from_requested_model_is_returned(self):
        eng, store, stub = stub_engine(lambda m, b: ProviderError("bad request", 400, False, {"error": {"message": "bad"}}))
        a = agent(store)
        status, body, _ = eng.handle("openai", chat("Translate: hello", model="sim-small"), a["key"])
        self.assertEqual(status, 400)

    def test_thinking_requests_are_not_capped(self):
        eng, store, stub = stub_engine(GOOD, max_output_tokens_cap=8192)
        a = agent(store, daily=100)
        body = chat("Plan the migration", max_tokens=20000, thinking={"type": "enabled", "budget_tokens": 16000})
        eng.handle("openai", body, a["key"])
        self.assertEqual(stub.calls[-1][1]["max_tokens"], 20000)
        eng.handle("openai", chat("Plan the rollout", max_tokens=20000), a["key"])
        self.assertEqual(stub.calls[-1][1]["max_tokens"], 8192)

    def test_budget_checks_the_priciest_model_the_climb_could_reach(self):
        eng, store, stub = stub_engine(GOOD)
        a = agent(store, daily=0.01)
        prompt = "Translate this note: " + ("word " * 3000)
        status, body, _ = eng.handle("openai", chat(prompt, max_tokens=1000), a["key"])
        self.assertEqual(status, 402)
        self.assertEqual(stub.calls, [])

    def test_upstream_outage_retries_do_not_trip_the_loop_cop(self):
        eng, store, stub = stub_engine(lambda m, b: ProviderError("overloaded", 503, True))
        a = agent(store)
        for _ in range(6):
            status, _, _ = eng.handle("openai", chat("Translate: hi", model="sim-small"), a["key"])
            self.assertEqual(status, 503)
        self.assertEqual(store.agent(a["id"])["status"], "on")

    def test_anthropic_beta_header_is_passed_through(self):
        eng, store, stub = stub_engine(GOOD)
        a = agent(store)
        eng.handle("openai", chat("Translate: hi"), a["key"], {"anthropic-beta": "context-1m-2025-08-07"})
        self.assertEqual(stub.calls[-1][2], {"anthropic-beta": "context-1m-2025-08-07"})

    def test_agent_managed_cache_is_not_trimmed(self):
        eng, store, stub = stub_engine(GOOD)
        a = agent(store, daily=100)
        big = "x" * 6000
        msgs = [{"role": "user", "content": "start"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": "f", "input": {}}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": big}]})
        msgs[-1]["content"][0]["cache_control"] = {"type": "ephemeral"}
        msgs.append({"role": "user", "content": "next"})
        eng.handle("anthropic", {"model": "sim-large", "max_tokens": 100, "messages": msgs}, a["key"])
        self.assertEqual(store.ledger()[0]["trimmed_tokens"], 0)

    def test_blocked_and_shadow_rows_are_not_counted_as_calls(self):
        eng, store, stub = stub_engine(GOOD)
        a = agent(store)
        eng.handle("openai", chat("Translate: hi"), a["key"])
        store.record(agent_id=a["id"], requested_model="sim-large", used_model="sim-large", cost=0.01,
                     baseline_cost=0.01, source="shadow", note="quality check")
        store.record(agent_id=a["id"], requested_model="sim-large", used_model="blocked", source="blocked", note="x")
        t = store.totals()
        self.assertEqual(t["calls"], 1)
        self.assertEqual(t["blocked"], 1)

    def test_usage_stays_with_the_client_it_was_made_for(self):
        eng, store, stub = stub_engine(GOOD)
        c1 = store.add_client("First", "a@example.com")
        c2 = store.add_client("Second", "b@example.com")
        aid = store.add_agent("Bot", 5, 1, client_id=c1)
        store.record(agent_id=aid, requested_model="sim-large", used_model="sim-small", cost=1.0, baseline_cost=2.0, source="model")
        store.update_agent(aid, client_id=c2)
        self.assertAlmostEqual(store.totals(client_id=c1)["cost"], 1.0)
        self.assertAlmostEqual(store.totals(client_id=c2)["cost"], 0.0)

    def test_new_task_id_race_is_safe(self):
        eng, store = engine()
        a = agent(store)
        store.add_task(a["id"], "x", 1.0, tid="job-1")
        store.add_task(a["id"], "x", 1.0, tid="job-1")  # second insert must not raise
        self.assertEqual(store.task("job-1")["agent_id"], a["id"])


class TestServerFixes(ServerCase):
    def setUp(self):
        self.pp.state.update(fakes.paypal_state())

    def _key(self, name="Bot"):
        s, r, _ = self.api("POST", "/api/agents", {"name": name})
        return r["agent"]["id"], r["agent"]["key"]

    def test_openai_stream_has_no_empty_choices_chunk_unless_asked(self):
        _, key = self._key()
        body = {"model": "sim-large", "stream": True, "messages": [{"role": "user", "content": "Translate: hello there"}]}
        s, raw, _ = http("POST", self.url + "/v1/chat/completions", body, {"Authorization": "Bearer " + key})
        chunks = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        self.assertTrue(all(c["choices"] for c in chunks))
        body["stream_options"] = {"include_usage": True}
        body["messages"][0]["content"] = "Translate: good night"
        s, raw, _ = http("POST", self.url + "/v1/chat/completions", body, {"Authorization": "Bearer " + key})
        chunks = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertIn("prompt_tokens", chunks[-1]["usage"])

    def test_cross_site_requests_are_refused(self):
        s, r, _ = http("POST", self.url + "/api/stop-all", {}, {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        s, r, _ = http("GET", self.url + "/api/state", None, {"Host": "evil.example"})
        self.assertEqual(s, 403)
        s, r, _ = http("POST", self.url + "/api/stop-all", {}, {"Content-Type": "text/plain"})
        self.assertEqual(s, 415)
        s, r, _ = http("GET", self.url + "/api/state", None, {"Origin": self.url})
        self.assertEqual(s, 200)

    def test_dashboard_api_gets_no_cors_preflight(self):
        s, _, h = http("OPTIONS", self.url + "/api/agents")
        self.assertNotIn("Access-Control-Allow-Origin", h)
        s, _, h = http("OPTIONS", self.url + "/v1/chat/completions")
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")

    def test_connections_cannot_read_paypal_or_admin_secrets(self):
        for bad in ("PAYPAL_CLIENT_SECRET", "LOWTIDES_ADMIN_TOKEN", "HOME"):
            s, r, _ = self.api("POST", "/api/providers", {"name": "x", "type": "openai", "base_url": "http://x", "api_key_env": bad})
            self.assertEqual(s, 400, bad)
        s, r, _ = self.api("POST", "/api/providers", {"name": "groq", "type": "openai", "base_url": "https://api.groq.com/openai/v1",
                                                      "api_key_env": "GROQ_API_KEY"})
        self.assertEqual(s, 200)

    def test_bad_content_length_is_rejected(self):
        host, port = self.url.replace("http://", "").split(":")
        c = httpclient.HTTPConnection(host, int(port), timeout=5)
        c.putrequest("POST", "/v1/chat/completions")
        c.putheader("Content-Length", "-1")
        c.putheader("Content-Type", "application/json")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_pending_capture_is_not_credited_until_paypal_completes_it(self):
        aid, _ = self._key("Wallet")
        s, r, _ = self.api("POST", "/api/wallet/topup", {"agent_id": aid, "amount": 20})
        oid = r["order_id"]
        fakes.approve(self.pp.state, oid)
        self.pp.state["capture_status"] = "PENDING"
        http("GET", self.url + "/paypal/return?kind=topup&token=" + oid)
        self.assertEqual(self.app.store.balance(aid), 0)
        self.assertEqual(self.app.store.topup(oid)["status"], "CAPTURE_PENDING")
        os.environ["TEST_WEBHOOK"] = "WH-1"
        self.app.config.data["paypal"]["webhook_id_env"] = "TEST_WEBHOOK"
        done = {"id": "EVT-C1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
                "resource": {"id": "CAP1", "supplementary_data": {"related_ids": {"order_id": oid}}}}
        http("POST", self.url + "/webhooks/paypal", done, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
        self.assertEqual(self.app.store.balance(aid), 20)
        rev = {"id": "EVT-R1", "event_type": "PAYMENT.CAPTURE.REVERSED",
               "resource": {"id": "CAP1", "amount": {"value": "20.00"}, "supplementary_data": {"related_ids": {"order_id": oid}}}}
        http("POST", self.url + "/webhooks/paypal", rev, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
        http("POST", self.url + "/webhooks/paypal", rev, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})  # duplicate
        self.assertEqual(self.app.store.balance(aid), 0)

    def test_failed_webhook_is_retried_not_dropped(self):
        os.environ["TEST_WEBHOOK"] = "WH-1"
        self.app.config.data["paypal"]["webhook_id_env"] = "TEST_WEBHOOK"
        original = self.app.money.on_webhook
        self.app.money.on_webhook = lambda e: (_ for _ in ()).throw(RuntimeError("PayPal hiccup"))
        try:
            ev = {"id": "EVT-FAIL", "event_type": "SOMETHING", "resource": {}}
            s, r, _ = http("POST", self.url + "/webhooks/paypal", ev, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
            self.assertEqual(s, 500)
        finally:
            self.app.money.on_webhook = original
        s, r, _ = http("POST", self.url + "/webhooks/paypal", ev, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
        self.assertEqual(s, 200)
        self.assertFalse(r.get("duplicate"))

    def _task(self, budget=2.0):
        aid, key = self._key("Hold")
        s, r, _ = self.api("POST", "/api/tasks", {"agent_id": aid, "label": "job", "budget": budget})
        return aid, r["task_id"]

    def test_canceled_approval_can_be_held_again(self):
        aid, tid = self._task()
        s, r1, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        s, r2, _ = self.api("POST", "/api/tasks/%s/hold" % tid)  # buyer canceled; try again
        self.assertEqual(s, 200)
        self.assertNotEqual(r1["order_id"], r2["order_id"])
        fakes.approve(self.pp.state, r2["order_id"])
        http("GET", self.url + "/paypal/return?kind=hold&token=" + r2["order_id"])
        s, r3, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        self.assertEqual(s, 400)

    def test_marking_done_releases_the_hold(self):
        aid, tid = self._task()
        s, r, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        fakes.approve(self.pp.state, r["order_id"])
        http("GET", self.url + "/paypal/return?kind=hold&token=" + r["order_id"])
        s, r, _ = self.api("POST", "/api/tasks/%s/done" % tid)
        self.assertEqual(s, 200)
        self.assertEqual(list(self.pp.state["auths"].values())[0]["status"], "VOIDED")

    def test_no_fallback_when_hold_is_dead(self):
        aid, tid = self._task(budget=3.0)
        s, r, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        fakes.approve(self.pp.state, r["order_id"])
        http("GET", self.url + "/paypal/return?kind=hold&token=" + r["order_id"])
        self.app.store.record(agent_id=aid, task_id=tid, requested_model="sim-large", used_model="sim-large",
                              cost=0.42, baseline_cost=0.42, source="model")
        list(self.pp.state["auths"].values())[0]["status"] = "VOIDED"
        s, r, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertEqual(s, 502)
        self.assertEqual(self.pp.state["captures"], {})
        self.assertIn("CAPTURE_FAILED", self.app.store.task(tid)["paypal_status"])

    def test_failed_refund_is_recorded_not_lost(self):
        aid, tid = self._task(budget=3.0)
        s, r, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        fakes.approve(self.pp.state, r["order_id"])
        http("GET", self.url + "/paypal/return?kind=hold&token=" + r["order_id"])
        self.app.store.record(agent_id=aid, task_id=tid, requested_model="sim-large", used_model="sim-large",
                              cost=0.42, baseline_cost=0.42, source="model")
        self.pp.state["partial_capture"] = False
        self.pp.state["refund_fails"] = True
        s, r, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertEqual(s, 400)
        t = self.app.store.task(tid)
        self.assertEqual(t["paypal_status"], "CAPTURED_FULL_REFUND_PENDING")
        self.assertAlmostEqual(t["captured_amount"], 3.0)

    def test_invoices_never_double_bill(self):
        s, r, _ = self.api("POST", "/api/clients", {"name": "Shop", "email": "shop@example.com", "markup_percent": 0})
        cid = r["client"]["id"]
        s, r, _ = self.api("POST", "/api/agents", {"name": "Shop bot", "client_id": cid})
        self.app.store.record(agent_id=r["agent"]["id"], requested_model="sim-large", used_model="sim-small",
                              cost=5.0, baseline_cost=9.0, source="model")
        s, r, _ = self.api("POST", "/api/clients/%d/invoice" % cid, {"days": 30})
        self.assertEqual(s, 200)
        s, p, _ = self.api("POST", "/api/clients/%d/invoice" % cid, {"preview": True, "days": 30})
        self.assertAlmostEqual(p["amount"], 0.0)  # the next period starts where the last invoice ended
        s, r2, _ = self.api("POST", "/api/clients/%d/invoice" % cid, {"days": 30})
        self.assertEqual(s, 400)
        self.assertEqual(len(self.pp.state["invoices"]), 1)


class TestAdminFixes(ServerCase):
    admin = "pw-123"

    def test_non_ascii_password_is_a_clean_401(self):
        s, r, _ = http("POST", self.url + "/api/login", {"token": "pässwörd"})
        self.assertEqual(s, 401)


if __name__ == "__main__":
    unittest.main()
