"""End-to-end tests over real HTTP: CostCop's server, its provider clients and its
PayPal client, talking to local fakes that speak the real wire formats."""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import fakes
from helpers import agent, config, engine

from costcop.money import Money
from costcop.paypal import PayPal
from costcop.server import App, make_handler
from costcop.store import Store


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (json.loads(raw) if "json" in ctype else raw), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


class TestRealProviders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm, cls.llm_url = fakes.start(fakes.FakeLLMHandler, fakes.llm_state())

    def setUp(self):
        self.llm.state.update(fakes.llm_state())

    def test_anthropic_auto_cache_and_usage(self):
        eng, store = engine(config(self.llm_url, prompt_cache_min_tokens=100))
        a = agent(store)
        self.llm.state["report_cache"] = 900
        body = {"model": "claude-opus-5-5", "system": "Policy text. " * 200, "max_tokens": 100,
                "messages": [{"role": "user", "content": "Classify: refund request"}]}
        status, resp, h = eng.handle("anthropic", body, a["key"])
        self.assertEqual(status, 200)
        sent = self.llm.state["requests"][-1][2]
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(sent["model"], "claude-haiku-5-5")  # easy step routed down
        row = store.ledger()[0]
        self.assertEqual(row["cache_read_tokens"], 900)
        self.assertIn("prompt caching added", row["note"])
        self.assertGreater(row["baseline_cost"], row["cost"])

    def test_respects_existing_cache_control(self):
        eng, store = engine(config(self.llm_url, prompt_cache_min_tokens=10))
        a = agent(store)
        body = {"model": "claude-sonnet-5-5", "max_tokens": 50,
                "system": [{"type": "text", "text": "x " * 500, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": "hi"}]}
        eng.handle("anthropic", body, a["key"])
        self.assertNotIn("prompt caching added", store.ledger()[0]["note"])

    def test_tool_calls_never_remembered(self):
        eng, store = engine(config(self.llm_url))
        a = agent(store)
        self.llm.state["tool_use"] = True
        body = {"model": "claude-sonnet-5-5", "max_tokens": 50, "messages": [{"role": "user", "content": "look it up"}]}
        eng.handle("anthropic", body, a["key"])
        _, _, h = eng.handle("anthropic", body, a["key"])
        self.assertNotEqual(h["X-CostCop-Model-Used"], "answer memory")

    def test_openai_fallback_on_outage(self):
        eng, store = engine(config(self.llm_url))
        a = agent(store)
        self.llm.state["fail_models"] = {"gpt-small"}
        status, resp, h = eng.handle("openai", {"model": "gpt-big", "messages": [{"role": "user", "content": "Translate: hi"}]}, a["key"])
        self.assertEqual(status, 200)
        self.assertEqual(h["X-CostCop-Model-Used"], "gpt-big")
        self.assertIn("failed", store.ledger()[0]["note"])

    def test_weak_cheap_answer_escalates_over_http(self):
        eng, store = engine(config(self.llm_url))
        a = agent(store)
        self.llm.state["weak_models"] = {"gpt-small"}
        status, resp, h = eng.handle("openai", {"model": "gpt-big", "messages": [{"role": "user", "content": "Translate: hi"}]}, a["key"])
        self.assertEqual(h["X-CostCop-Model-Used"], "gpt-big")
        row = store.ledger()[0]
        self.assertEqual(row["escalations"], 1)
        models_called = [r[2]["model"] for r in self.llm.state["requests"]]
        self.assertEqual(models_called, ["gpt-small", "gpt-big"])

    def test_missing_usage_marked_estimated(self):
        eng, store = engine(config(self.llm_url))
        a = agent(store)
        self.llm.state["no_usage"] = True
        eng.handle("openai", {"model": "gpt-big", "messages": [{"role": "user", "content": "Translate: hi"}]}, a["key"])
        self.assertEqual(store.ledger()[0]["usage_estimated"], 1)


class ServerCase(unittest.TestCase):
    admin = ""

    @classmethod
    def setUpClass(cls):
        cls.pp, cls.pp_url = fakes.start(fakes.FakePayPalHandler, fakes.paypal_state())
        os.environ["COSTCOP_ADMIN_TOKEN"] = cls.admin
        cfg = config()
        cls.tmp_cfg = os.path.join(os.path.dirname(__file__), "_tmp_config_%s.json" % cls.__name__)
        store = Store(":memory:")
        paypal = PayPal("client", "secret", base_url=cls.pp_url)
        cls.app = App(cfg, store, paypal=paypal, config_path=cls.tmp_cfg)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cfg.data["paypal"]["public_base_url"] = cls.url
        os.environ["COSTCOP_ADMIN_TOKEN"] = ""

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        if os.path.exists(cls.tmp_cfg):
            os.remove(cls.tmp_cfg)

    def api(self, method, path, body=None):
        h = {"X-Admin-Token": self.admin} if self.admin else {}
        return http(method, self.url + path, body, h)


class TestServer(ServerCase):
    def test_agent_api_openai_and_stream(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Bot", "daily_budget": 3, "task_budget": 1})
        key = r["agent"]["key"]
        body = {"model": "sim-large", "messages": [{"role": "user", "content": "Translate: hello"}]}
        s, r, h = http("POST", self.url + "/v1/chat/completions", body, {"Authorization": "Bearer " + key})
        self.assertEqual(s, 200)
        self.assertEqual(h.get("X-CostCop-Model-Used"), "sim-small")
        body["stream"] = True
        body["messages"][0]["content"] = "Translate: goodbye"
        s, raw, h = http("POST", self.url + "/v1/chat/completions", body, {"Authorization": "Bearer " + key})
        self.assertEqual(s, 200)
        self.assertIn("text/event-stream", h.get("Content-Type"))
        lines = [l for l in raw.splitlines() if l.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        first = json.loads(lines[0][6:])
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertIn("simulated", first["choices"][0]["delta"]["content"])

    def test_anthropic_stream_events(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Claude bot"})
        key = r["agent"]["key"]
        body = {"model": "sim-large", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "Tag sentiment: great"}]}
        s, raw, h = http("POST", self.url + "/v1/messages", body, {"x-api-key": key})
        events = [l[7:] for l in raw.splitlines() if l.startswith("event: ")]
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        self.assertIn("content_block_delta", events)

    def test_state_receipt_csv_and_controls(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Csv bot"})
        aid, key = r["agent"]["id"], r["agent"]["key"]
        http("POST", self.url + "/v1/chat/completions", {"model": "sim-large", "messages": [{"role": "user", "content": "Summarize: hi"}]},
             {"Authorization": "Bearer " + key})
        s, st, _ = self.api("GET", "/api/state")
        self.assertEqual(s, 200)
        self.assertTrue(any(a["id"] == aid for a in st["agents"]))
        self.assertIn("Saved", st["today"]["text"])
        s, csv_text, _ = self.api("GET", "/api/ledger.csv")
        self.assertTrue(csv_text.startswith("id,time,agent"))
        s, r, _ = self.api("POST", "/api/agents/%d/stop" % aid)
        self.assertEqual(r["agent"]["status"], "stopped")
        s, r, _ = http("POST", self.url + "/v1/chat/completions", {"model": "sim-large", "messages": [{"role": "user", "content": "x"}]},
                       {"Authorization": "Bearer " + key})
        self.assertEqual(s, 403)
        self.api("POST", "/api/agents/%d/start" % aid)
        s, r, _ = self.api("POST", "/api/agents/%d/update" % aid, {"daily_budget": 9})
        self.assertEqual(r["agent"]["daily_budget"], 9)
        s, r, _ = self.api("POST", "/api/agents/%d/update" % aid, {"daily_budget": -1})
        self.assertEqual(s, 400)

    def test_models_and_policy_saved(self):
        s, r, _ = self.api("POST", "/api/models", {"name": "my-local", "provider": "local", "tier": 1, "family": "local",
                                                    "input": 0, "output": 0})
        self.assertIn("my-local", r["models"])
        s, r, _ = self.api("POST", "/api/models", {"name": "bad", "provider": "nowhere", "tier": 1, "family": "x", "input": 1, "output": 1})
        self.assertEqual(s, 400)
        s, r, _ = self.api("POST", "/api/policy", {"loop_max_repeats": 7, "not_a_setting": 1, "mode": "business"})
        self.assertEqual(r["policy"]["loop_max_repeats"], 7)
        self.assertNotIn("not_a_setting", r["policy"])
        self.assertEqual(r["mode"], "business")
        with open(self.tmp_cfg) as f:
            saved = json.load(f)
        self.assertEqual(saved["policy"]["loop_max_repeats"], 7)
        self.api("POST", "/api/policy", {"loop_max_repeats": 4, "mode": "home"})

    def test_demo_and_try(self):
        s, r, _ = self.api("POST", "/api/demo", {})
        self.assertEqual(s, 200)
        aid = r["agent_id"]
        self.assertTrue(all(x["status"] == 200 for x in r["results"]))
        s, rec, _ = self.api("GET", "/api/receipt?agent=%d&days=1" % aid)
        self.assertGreater(rec["totals"]["saved"], 0)
        self.assertGreater(rec["totals"]["memory_hits"], 0)
        self.assertGreater(rec["totals"]["trimmed_tokens"], 0)
        s, r, _ = self.api("POST", "/api/try", {"agent_id": aid, "prompt": "Classify: invoice question", "model": "sim-large"})
        self.assertEqual(r["status"], 200)
        self.assertIn("simulated", r["text"])
        s, r, _ = self.api("POST", "/api/demo", {"loop": True})
        self.assertEqual(r["agent_status"], "paused")
        self.api("POST", "/api/agents/%d/resume" % aid)

    def test_static_and_traversal(self):
        s, raw, h = http("GET", self.url + "/")
        self.assertEqual(s, 200)
        s, raw, h = http("GET", self.url + "/../costcop/server.py")
        self.assertEqual(s, 404)


class TestAdminLock(ServerCase):
    admin = "pw-123"

    def test_locked(self):
        s, r, _ = http("GET", self.url + "/api/state")
        self.assertEqual(s, 401)
        s, r, _ = self.api("GET", "/api/state")
        self.assertEqual(s, 200)
        s, r, h = http("POST", self.url + "/api/login", {"token": "wrong"})
        self.assertEqual(s, 401)
        s, r, h = http("POST", self.url + "/api/login", {"token": "pw-123"})
        self.assertIn("cc_admin=", h.get("Set-Cookie", ""))


class TestMoney(ServerCase):
    def setUp(self):
        self.pp.state.update(fakes.paypal_state())

    def test_topup_flow_is_idempotent(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Wallet bot", "use_wallet": True})
        aid = r["agent"]["id"]
        s, r, _ = self.api("POST", "/api/wallet/topup", {"agent_id": aid, "amount": 10})
        self.assertEqual(s, 200)
        self.assertIn("sandbox.paypal.com", r["approve_url"])
        oid = r["order_id"]
        s, body, _ = http("GET", self.url + "/paypal/return?kind=topup&token=" + oid)  # not approved yet
        self.assertEqual(self.app.store.balance(aid), 0)
        fakes.approve(self.pp.state, oid)
        http("GET", self.url + "/paypal/return?kind=topup&token=" + oid)
        self.assertEqual(self.app.store.balance(aid), 10)
        http("GET", self.url + "/paypal/return?kind=topup&token=" + oid)  # double return
        self.assertEqual(self.app.store.balance(aid), 10)
        s, r, _ = self.api("POST", "/api/wallet/topup", {"agent_id": aid, "amount": 0.5})
        self.assertEqual(s, 400)

    def _task_with_spend(self, budget=2.0, label="report"):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Hold bot"})
        aid, key = r["agent"]["id"], r["agent"]["key"]
        s, r, _ = self.api("POST", "/api/tasks", {"agent_id": aid, "label": label, "budget": budget})
        tid = r["task_id"]
        self.assertIn(r["quote"]["method"], ("rough", "history"))
        s, r, _ = self.api("POST", "/api/tasks/%s/hold" % tid)
        oid = r["order_id"]
        fakes.approve(self.pp.state, oid)
        http("GET", self.url + "/paypal/return?kind=hold&token=" + oid)
        t = self.app.store.task(tid)
        self.assertEqual(t["paypal_status"], "AUTHORIZED")
        return aid, key, tid

    def test_hold_then_charge_only_real_cost(self):
        aid, key, tid = self._task_with_spend()
        for i in range(3):
            http("POST", self.url + "/v1/chat/completions",
                 {"model": "sim-large", "messages": [{"role": "user", "content": "Write a full report section %d " % i * 30}]},
                 {"Authorization": "Bearer " + key, "X-CostCop-Task": tid})
        spent = self.app.store.task_spend(tid)
        self.app.store.record(agent_id=aid, task_id=tid, requested_model="sim-large", used_model="sim-large",
                              cost=0.37, baseline_cost=0.37, source="model", note="test spend")
        s, r, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertEqual(s, 200)
        self.assertEqual(r["method"], "partial capture")
        self.assertAlmostEqual(r["charged"], round(spent + 0.37, 2))
        auth = list(self.pp.state["auths"].values())[0]
        self.assertAlmostEqual(auth["captured"], r["charged"])
        self.assertEqual(auth["status"], "CAPTURED")
        s, r2, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertTrue(r2.get("already"))

    def test_fallback_when_partial_capture_disabled(self):
        aid, key, tid = self._task_with_spend(budget=3.0)
        self.app.store.record(agent_id=aid, task_id=tid, requested_model="sim-large", used_model="sim-large",
                              cost=0.42, baseline_cost=0.42, source="model", note="test spend")
        self.pp.state["partial_capture"] = False
        s, r, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertEqual(r["method"], "capture then refund")
        self.assertAlmostEqual(r["charged"], 0.42)
        cap = list(self.pp.state["captures"].values())[0]
        self.assertAlmostEqual(cap["amount"] - cap["refunded"], 0.42)

    def test_tiny_cost_voids_hold(self):
        aid, key, tid = self._task_with_spend()
        s, r, _ = self.api("POST", "/api/tasks/%s/settle" % tid)
        self.assertEqual(r["method"], "void")
        self.assertEqual(r["charged"], 0)
        self.assertEqual(list(self.pp.state["auths"].values())[0]["status"], "VOIDED")

    def test_client_invoice(self):
        s, r, _ = self.api("POST", "/api/clients", {"name": "Roofing Co", "email": "owner@example.com", "markup_percent": 20})
        cid = r["client"]["id"]
        s, r, _ = self.api("POST", "/api/agents", {"name": "Roof bot", "client_id": cid})
        aid = r["agent"]["id"]
        self.app.store.record(agent_id=aid, requested_model="sim-large", used_model="sim-small", cost=10.0,
                              baseline_cost=25.0, source="model", note="test")
        s, p, _ = self.api("POST", "/api/clients/%d/invoice" % cid, {"preview": True, "days": 30})
        self.assertAlmostEqual(p["amount"], 12.0)
        self.assertIn("Saved: $15.00", p["receipt"])
        s, r, _ = self.api("POST", "/api/clients/%d/invoice" % cid, {"days": 30, "send": True})
        self.assertEqual(s, 200)
        self.assertEqual(r["status"], "SENT")
        inv = self.pp.state["invoices"][r["invoice_id"]]
        self.assertEqual(inv["items"][0]["unit_amount"]["value"], "12.00")
        self.assertEqual(inv["primary_recipients"][0]["billing_info"]["email_address"], "owner@example.com")

    def test_webhook_verification(self):
        os.environ["TEST_WEBHOOK"] = "WH-1"
        self.app.config.data["paypal"]["webhook_id_env"] = "TEST_WEBHOOK"
        event = {"id": "WH-EVT-1", "event_type": "SOMETHING.ELSE", "resource": {}}
        s, r, _ = http("POST", self.url + "/webhooks/paypal", event, {"PAYPAL-TRANSMISSION-SIG": "forged"})
        self.assertEqual(s, 400)
        s, r, _ = http("POST", self.url + "/webhooks/paypal", event, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
        self.assertEqual(r["result"], "ignored")
        s, r, _ = http("POST", self.url + "/webhooks/paypal", event, {"PAYPAL-TRANSMISSION-SIG": "good-signature"})
        self.assertTrue(r.get("duplicate"))

    def test_paypal_requests_carry_idempotency_keys(self):
        self.test_topup_flow_is_idempotent()
        posts = [r for r in self.pp.state["requests"] if r[0].startswith("/v2/")]
        self.assertTrue(posts)
        self.assertTrue(all(r[1].get("Paypal-Request-Id") or r[1].get("PayPal-Request-Id") for r in posts))


if __name__ == "__main__":
    unittest.main()
