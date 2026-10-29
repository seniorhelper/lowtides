"""The Leash: per-agent limits on data dumps, enforced in the proxy pipeline.
Uses the simulated demo models only; no real API calls."""
import json
import unittest

from helpers import agent, config, engine

NO_MEM = {"x-lowtides-memory": "off"}


def words(n, tag=""):
    # "word " is 5 chars, so about 1.25 tokens per word at 4 chars/token.
    return (tag + " " if tag else "") + "word " * n


def body(api, prompt, model="sim-large", **extra):
    b = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    b.update(extra)
    return b


class LeashBase(unittest.TestCase):
    def setUp(self):
        self.eng, self.store = engine(config(max_output_tokens_cap=8192))
        self.agent = agent(self.store, daily=100.0)
        self.sent = []
        real = self.eng.providers.call

        def spy(model, work, api, passthru=None):
            self.sent.append((model, work))
            return real(model, work, api, passthru)
        self.eng.providers.call = spy

    def leash(self, **rules):
        self.store.update_agent(self.agent["id"], rules=json.dumps(rules))

    def call(self, api, b, **headers):
        h = dict(NO_MEM)
        h.update(headers)
        return self.eng.handle(api, b, self.agent["key"], h)

    def last_note(self):
        return self.store.ledger()[0]["note"]

    def error_message(self, api, resp):
        if api == "anthropic":
            self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["error"]["type"], "lowtides_blocked")
        return resp["error"]["message"], resp["error"]["code"]


class TestReplyClamp(LeashBase):
    def test_clamps_both_endpoints(self):
        self.leash(max_reply_tokens=200)
        for api, key in (("openai", "max_tokens"), ("anthropic", "max_tokens")):
            status, _, _ = self.call(api, body(api, "hello " + api, max_tokens=4000))
            self.assertEqual(status, 200)
            self.assertEqual(self.sent[-1][1][key], 200)
            self.assertIn("reply capped at 200 tokens by leash", self.last_note())

    def test_clamps_max_completion_tokens(self):
        self.leash(max_reply_tokens=150)
        self.call("openai", body("openai", "hi", max_completion_tokens=999))
        self.assertEqual(self.sent[-1][1]["max_completion_tokens"], 150)

    def test_sets_limit_when_absent(self):
        self.leash(max_reply_tokens=120)
        self.call("openai", body("openai", "no limit given"))
        self.assertEqual(self.sent[-1][1]["max_tokens"], 120)
        self.assertIn("reply limit set to 120 tokens by leash", self.last_note())

    def test_smaller_of_global_cap_and_leash(self):
        eng, store = engine(config(max_output_tokens_cap=100))
        a = agent(store, daily=100.0)
        store.update_agent(a["id"], rules=json.dumps({"max_reply_tokens": 500}))
        sent = []
        real = eng.providers.call
        eng.providers.call = lambda m, w, api, p=None: (sent.append(w), real(m, w, api, p))[1]
        eng.handle("openai", body("openai", "x", max_tokens=4000), a["key"], NO_MEM)
        self.assertEqual(sent[-1]["max_tokens"], 100)
        self.assertIn("output capped at 100 tokens", store.ledger()[0]["note"])
        store.update_agent(a["id"], rules=json.dumps({"max_reply_tokens": 50}))
        eng.handle("openai", body("openai", "y", max_tokens=4000), a["key"], NO_MEM)
        self.assertEqual(sent[-1]["max_tokens"], 50)
        self.assertIn("by leash", store.ledger()[0]["note"])

    def test_thinking_budget_fits_under_leash(self):
        self.leash(max_reply_tokens=2000)
        self.call("anthropic", body("anthropic", "think", max_tokens=8000,
                                    thinking={"type": "enabled", "budget_tokens": 4000}))
        sent = self.sent[-1][1]
        self.assertEqual(sent["max_tokens"], 2000)
        self.assertLess(sent["thinking"]["budget_tokens"], 2000)


class TestInputBlock(LeashBase):
    def test_blocks_big_prompt_both_endpoints(self):
        self.leash(max_input_tokens=500)
        for api in ("openai", "anthropic"):
            n_before = len(self.sent)
            status, resp, _ = self.call(api, body(api, words(1000, api), max_tokens=100))
            self.assertEqual(status, 413)
            msg, code = self.error_message(api, resp)
            self.assertEqual(code, "leash_input_tokens")
            self.assertRegex(msg, r"^This prompt is about \d+ tokens; this agent's leash allows 500\. Summarize or split it first\.$")
            self.assertEqual(len(self.sent), n_before)  # nothing was sent upstream
            row = self.store.ledger()[0]
            self.assertEqual((row["source"], row["note"], row["cost"]), ("blocked", "leash_input_tokens", 0.0))
        self.assertTrue(any(e["kind"] == "leash" for e in self.store.events()))
        self.assertTrue(any(e["kind"] == "leash.blocked" for e in self.store.audit_entries()))

    def test_small_prompt_passes(self):
        self.leash(max_input_tokens=500)
        status, _, _ = self.call("openai", body("openai", words(50), max_tokens=100))
        self.assertEqual(status, 200)

    def test_counts_after_trimming(self):
        # A long, old tool result gets trimmed before the leash measures the prompt.
        eng, store = engine(config(trim_keep_recent_turns=2, trim_max_old_tool_chars=200))
        a = agent(store, daily=100.0)
        store.update_agent(a["id"], rules=json.dumps({"max_input_tokens": 1500}))
        msgs = [{"role": "user", "content": "start"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function",
                                                                        "function": {"name": "dump", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "t1", "content": "row " * 4000}]
        for i in range(6):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "turn %d" % i})
        status, resp, _ = eng.handle("openai", {"model": "sim-large", "messages": msgs, "max_tokens": 100}, a["key"], NO_MEM)
        self.assertEqual(status, 200, resp)


class TestCostPerCall(LeashBase):
    def test_blocks_expensive_call_both_endpoints(self):
        self.leash(max_cost_per_call=0.0001)  # smallest cap the rules allow; sim-small costs ~$0.00025 here
        for api in ("openai", "anthropic"):
            status, resp, _ = self.call(api, body(api, words(800, api), max_tokens=300))
            self.assertEqual(status, 402)
            msg, code = self.error_message(api, resp)
            self.assertEqual(code, "leash_cost_per_call")
            self.assertIn("leash allows $0.0001 per call", msg)
            self.assertEqual(self.store.ledger()[0]["source"], "blocked")
        self.assertEqual(self.sent, [])

    def test_priced_on_routed_model(self):
        # ~1000 input tokens + 300 output: sim-small ~$0.00025, sim-large ~$0.01.
        self.leash(max_cost_per_call=0.002)
        prompt = words(800)
        status, _, _ = self.call("openai", body("openai", prompt, max_tokens=300), **{"x-lowtides-tier": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(self.sent[-1][0], "sim-small")
        self.assertIn("won't climb to", self.last_note())
        status, resp, _ = self.call("openai", body("openai", words(800, "b"), max_tokens=300), **{"x-lowtides-tier": "3"})
        self.assertEqual(status, 402)
        self.assertIn("sim-large", resp["error"]["message"])


class TestCostCapReplyLimit(LeashBase):
    """A per-call cap must bound the reply too, not just assume 1024 output tokens."""
    LARGE = {"x-lowtides-tier": "3"}  # sim-large: $4/M in, $20/M out

    def worst_case(self, sent):
        from lowtides import pricing
        lim = min(v for k, v in sent.items() if k in ("max_tokens", "max_completion_tokens"))
        return pricing.cost(self.eng.config.model("sim-large"),
                            input_tokens=pricing.estimate_request_tokens(sent), output_tokens=lim)

    def test_injects_limit_when_absent_both_endpoints(self):
        self.leash(max_cost_per_call=0.002)  # ~99 reply tokens on sim-large
        for api in ("openai", "anthropic"):
            status, resp, _ = self.call(api, body(api, "say hi " + api), **self.LARGE)
            self.assertEqual(status, 200, resp)
            model, sent = self.sent[-1]
            self.assertEqual(model, "sim-large")
            self.assertIn("max_tokens", sent)
            self.assertLess(sent["max_tokens"], 100)
            self.assertGreaterEqual(sent["max_tokens"], 16)
            self.assertLessEqual(self.worst_case(sent), 0.002)
            self.assertIn("per-call leash", self.last_note())

    def test_clamps_large_max_tokens(self):
        self.leash(max_cost_per_call=0.002)
        self.call("openai", body("openai", "hello", max_tokens=100000), **self.LARGE)
        sent = self.sent[-1][1]
        self.assertLess(sent["max_tokens"], 100)
        self.assertLessEqual(self.worst_case(sent), 0.002)

    def test_reasoning_request_gets_max_completion_tokens(self):
        self.leash(max_cost_per_call=0.002)
        self.call("openai", body("openai", "hello", reasoning_effort="low"), **self.LARGE)
        sent = self.sent[-1][1]
        self.assertIn("max_completion_tokens", sent)
        self.assertLessEqual(self.worst_case(sent), 0.002)

    def test_smaller_limits_win(self):
        self.leash(max_cost_per_call=0.002, max_reply_tokens=40)
        self.call("openai", body("openai", "hello"), **self.LARGE)
        self.assertEqual(self.sent[-1][1]["max_tokens"], 40)
        self.leash(max_cost_per_call=0.002)
        self.call("anthropic", body("anthropic", "hello", max_tokens=30), **self.LARGE)
        self.assertEqual(self.sent[-1][1]["max_tokens"], 30)
        eng, store = engine(config(max_output_tokens_cap=20))
        a = agent(store, daily=100.0)
        store.update_agent(a["id"], rules=json.dumps({"max_cost_per_call": 0.002}))
        sent = []
        real = eng.providers.call
        eng.providers.call = lambda m, w, api, p=None: (sent.append(w), real(m, w, api, p))[1]
        eng.handle("openai", body("openai", "x", max_tokens=4000), a["key"], dict(NO_MEM, **self.LARGE))
        self.assertEqual(sent[-1]["max_tokens"], 20)

    def test_refuses_when_tiny_reply_cannot_fit(self):
        self.leash(max_cost_per_call=0.0001)  # $100/M budget: 25 input tokens on sim-large use it all
        for api in ("openai", "anthropic"):
            status, resp, _ = self.call(api, body(api, words(100, api)), **self.LARGE)
            self.assertEqual(status, 402)
            self.assertEqual(self.error_message(api, resp)[1], "leash_cost_per_call")
        self.assertEqual(self.sent, [])

    def test_thinking_fits_injected_limit(self):
        self.leash(max_cost_per_call=0.05)  # ~2490 reply tokens on sim-large
        self.call("anthropic", body("anthropic", "think", max_tokens=8000,
                                    thinking={"type": "enabled", "budget_tokens": 6000}), **self.LARGE)
        sent = self.sent[-1][1]
        self.assertLess(sent["max_tokens"], 8000)
        self.assertLess(sent["thinking"]["budget_tokens"], sent["max_tokens"])


B64_PDF = "JVBERi0xLjQK" + "A" * 400000  # ~100k tokens at len/4


class TestNonTextContent(LeashBase):
    def test_estimates(self):
        from lowtides import pricing
        url_img = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(url_img), pricing.IMAGE_URL_TOKENS)
        anth_img = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://example.com/cat.png"}}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(anth_img), pricing.IMAGE_URL_TOKENS)
        b64_img = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Q" * 40000}}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(b64_img), 10000)
        audio = {"messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "U" * 8000, "format": "wav"}}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(audio), 2000)
        unknown = {"messages": [{"role": "user", "content": [{"type": "mystery_blob", "payload": "z" * 4000}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(unknown), 1000)
        nested = {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Q" * 40000}}]}]}]}
        self.assertGreaterEqual(pricing.estimate_request_tokens(nested), 10000)
        plain = {"messages": [{"role": "user", "content": "word " * 80}]}
        self.assertEqual(pricing.estimate_request_tokens(plain), 100)  # text estimate unchanged

    def test_big_pdf_blocked_both_endpoints(self):
        self.leash(max_input_tokens=5000)
        anth = body("anthropic", [{"type": "text", "text": "summarize"},
                                  {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                                  "data": B64_PDF}}], max_tokens=100)
        oa = body("openai", [{"type": "text", "text": "summarize"},
                             {"type": "file", "file": {"filename": "big.pdf",
                                                       "file_data": "data:application/pdf;base64," + B64_PDF}}],
                  max_tokens=100)
        for api, b in (("anthropic", anth), ("openai", oa)):
            status, resp, _ = self.call(api, b)
            self.assertEqual(status, 413, resp)
            self.assertEqual(self.error_message(api, resp)[1], "leash_input_tokens")
        self.assertEqual(self.sent, [])

    def test_big_pdf_blocked_by_cost_cap(self):
        self.leash(max_cost_per_call=0.05)  # 100k input tokens is ~$0.01 on sim-small, ~$0.40 on sim-large
        b = body("anthropic", [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": B64_PDF}}], max_tokens=100)
        status, resp, _ = self.call("anthropic", b, **{"x-lowtides-tier": "3"})
        self.assertEqual(status, 402)
        self.assertEqual(self.error_message("anthropic", resp)[1], "leash_cost_per_call")

    def test_image_url_counted_against_input_cap(self):
        self.leash(max_input_tokens=1000)
        oa = body("openai", [{"type": "text", "text": "what is this"},
                             {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}], max_tokens=50)
        status, resp, _ = self.call("openai", oa)
        self.assertEqual(status, 413, resp)
        self.leash(max_input_tokens=5000)
        status, resp, _ = self.call("openai", oa)
        self.assertEqual(status, 200, resp)


class TestDumpAlert(LeashBase):
    def baseline(self, api="openai"):
        for i in range(5):
            status, _, _ = self.call(api, body(api, words(40, "base %d" % i), max_tokens=50))
            self.assertEqual(status, 200)

    def test_needs_history(self):
        self.call("openai", body("openai", words(4000), max_tokens=50))
        self.assertNotIn("dump alert", self.last_note())

    def test_fires_after_baseline_without_blocking(self):
        for api in ("openai", "anthropic"):
            self.setUp()
            self.baseline(api)
            status, _, _ = self.call(api, body(api, words(2000, "big"), max_tokens=50))
            self.assertEqual(status, 200)
            row = self.store.ledger()[0]
            self.assertEqual(row["source"], "model")
            self.assertIn("dump alert", row["note"])
            self.assertTrue(any(e["kind"] == "leash" and "Dump alert" in e["message"] for e in self.store.events()))
            self.assertTrue(any(e["kind"] == "leash.dump_alert" for e in self.store.audit_entries()))

    def test_normal_call_no_alert(self):
        self.baseline()
        self.call("openai", body("openai", words(60, "normal"), max_tokens=50))
        self.assertNotIn("dump alert", self.last_note())

    def test_custom_multiple_and_off(self):
        self.baseline()
        self.leash(dump_alert_multiple=100)
        self.call("openai", body("openai", words(2000, "big"), max_tokens=50))
        self.assertNotIn("dump alert", self.last_note())
        self.leash(dump_alert_multiple=None)
        self.call("openai", body("openai", words(9000, "bigger"), max_tokens=50))
        self.assertNotIn("dump alert", self.last_note())


class TestNoLeash(LeashBase):
    def test_unchanged_behavior(self):
        for api in ("openai", "anthropic"):
            status, _, _ = self.call(api, body(api, words(3000, api), max_tokens=700))
            self.assertEqual(status, 200)
            self.assertEqual(self.sent[-1][1]["max_tokens"], 700)
            self.assertNotIn("leash", self.last_note())
        self.call("openai", body("openai", "no max"))
        self.assertNotIn("max_tokens", self.sent[-1][1])
        self.assertFalse(any(e["kind"].startswith("leash") for e in self.store.audit_entries()))

    def test_global_cap_still_applies(self):
        eng, store = engine(config(max_output_tokens_cap=100))
        a = agent(store, daily=100.0)
        eng.handle("openai", body("openai", "x", max_tokens=4000), a["key"], NO_MEM)
        self.assertIn("output capped at 100 tokens", store.ledger()[0]["note"])


if __name__ == "__main__":
    unittest.main()
