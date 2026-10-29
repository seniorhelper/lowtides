"""The Leash, black-box over real HTTP (simulated models only, no real API calls)."""
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from helpers import config

from lowtides.server import App, make_handler
from lowtides.store import Store
from test_http import http

SENTENCE = "Keep replies short. Never send more than 50 pages. No single call over 25 cents."
NO_MEM = {"x-lowtides-memory": "off"}


def words(n, tag=""):
    return (tag + " " if tag else "") + "word " * n


class LeashE2E(unittest.TestCase):
    public = False

    @classmethod
    def setUpClass(cls):
        os.environ["LOWTIDES_ADMIN_TOKEN"] = ""
        if cls.public:
            os.environ["LOWTIDES_PUBLIC_DEMO"] = "1"
        cfg = config(max_output_tokens_cap=8192)
        cls.store = Store(":memory:")
        cls.tmp_cfg = os.path.join(os.path.dirname(__file__), "_tmp_config_%s.json" % cls.__name__)
        cls.app = App(cfg, cls.store, config_path=cls.tmp_cfg)
        os.environ.pop("LOWTIDES_PUBLIC_DEMO", None)
        cls.sent = []
        real = cls.app.engine.providers.call

        def spy(model, work, api, passthru=None):
            cls.sent.append((model, work))
            return real(model, work, api, passthru)
        cls.app.engine.providers.call = spy
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        if os.path.exists(cls.tmp_cfg):
            os.remove(cls.tmp_cfg)

    def api(self, method, path, body=None):
        return http(method, self.url + path, body, {})

    def new_agent(self, name, leashed=True):
        s, r, _ = self.api("POST", "/api/agents", {"name": name, "daily_budget": 100, "task_budget": 50})
        self.assertEqual(s, 200, r)
        aid, key = r["agent"]["id"], r["agent"]["key"]
        if leashed:
            s, r, _ = self.api("POST", "/api/rules/compile", {"text": SENTENCE})
            self.assertEqual(s, 200, r)
            rules = r["rules"]
            s, r, _ = self.api("POST", "/api/agents/%s/rules" % aid, {"rules": rules})
            self.assertEqual(s, 200, r)
            self.rules = r["rules"]
        return aid, key

    def chat(self, key, prompt, model="sim-large", max_tokens=4000, **hdr):
        h = {"Authorization": "Bearer " + key}
        h.update(NO_MEM)
        h.update(hdr)
        b = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        return http("POST", self.url + "/v1/chat/completions", b, h)

    def messages(self, key, prompt, model="sim-large", max_tokens=4000):
        h = {"x-api-key": key}
        h.update(NO_MEM)
        b = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        return http("POST", self.url + "/v1/messages", b, h)

    def events(self, aid, kind):
        return [e for e in self.store.events(500) if e.get("agent_id") == aid and e["kind"] == kind]

    def audits(self, aid, kind):
        return [e for e in self.store.audit_entries(limit=5000) if e["kind"] == kind and str(e.get("ref")) == str(aid)]

    def readable(self, resp):
        msg = resp["error"]["message"]
        self.assertIsInstance(msg, str)
        self.assertGreater(len(msg), 20)
        self.assertNotIn("Traceback", msg)
        return msg

    # ---- scenarios --------------------------------------------------------------------
    def test_1_sentence_compiles_and_clamps_reply(self):
        aid, key = self.new_agent("short")
        r = self.rules
        self.assertTrue(r.get("max_reply_tokens"))
        self.assertTrue(r.get("max_input_tokens"))
        self.assertAlmostEqual(r.get("max_cost_per_call"), 0.25)
        n = len(self.sent)
        s, resp, _ = self.chat(key, "hello there", max_tokens=4000)
        self.assertEqual(s, 200, resp)
        self.assertEqual(self.sent[n][1]["max_tokens"], r["max_reply_tokens"])
        row = self.store.ledger(aid)[0]
        self.assertIn("leash", row["note"])

    def test_2_huge_prompt_refused_openai(self):
        aid, key = self.new_agent("big-oa")
        n = len(self.sent)
        s, resp, _ = self.chat(key, words(32000))  # ~40k tokens
        self.assertIn(s, (402, 413), resp)
        msg = self.readable(resp)
        self.assertIn("leash", msg)
        self.assertEqual(len(self.sent), n)
        self.assertEqual(self.store.ledger(aid)[0]["source"], "blocked")
        self.assertTrue(self.events(aid, "leash"))
        self.assertTrue(self.audits(aid, "leash.blocked"))

    def test_3_huge_prompt_refused_anthropic(self):
        aid, key = self.new_agent("big-an")
        n = len(self.sent)
        s, resp, _ = self.messages(key, words(32000))
        self.assertIn(s, (402, 413), resp)
        self.assertEqual(resp["type"], "error")
        self.readable(resp)
        self.assertEqual(len(self.sent), n)
        self.assertTrue(self.audits(aid, "leash.blocked"))

    def test_4_expensive_call_refused(self):
        # The compiled sentence caps input and reply, which keeps a call under 25 cents by
        # itself, so keep only its dollar rule here and send a 70k-token prompt to sim-large
        # ($4 per million in): about $0.28 before the reply.
        aid, key = self.new_agent("pricey", leashed=False)
        s, r, _ = self.api("POST", "/api/rules/compile", {"text": "No single call over 25 cents."})
        self.assertEqual(s, 200, r)
        self.assertAlmostEqual(r["rules"]["max_cost_per_call"], 0.25)
        s, r, _ = self.api("POST", "/api/agents/%s/rules" % aid, {"rules": {"max_cost_per_call": 0.25}})
        self.assertEqual(s, 200, r)
        n = len(self.sent)
        s, resp, _ = self.chat(key, words(56000), max_tokens=100, **{"x-lowtides-tier": "3"})
        self.assertIn(s, (402, 413), resp)
        msg = self.readable(resp)
        self.assertIn("$", msg)
        self.assertEqual(len(self.sent), n)
        self.assertEqual(self.store.ledger(aid)[0]["source"], "blocked")
        self.assertTrue(self.audits(aid, "leash.blocked"))
    def test_5_dump_alert_lets_through(self):
        aid, key = self.new_agent("dumper", leashed=False)
        s, r, _ = self.api("POST", "/api/agents/%s/rules" % aid, {"rules": {"dump_alert_multiple": 5}})
        self.assertEqual(s, 200, r)
        for i in range(6):
            s, _, _ = self.chat(key, words(40, "small %d" % i), max_tokens=50)
            self.assertEqual(s, 200)
        s, resp, _ = self.chat(key, words(800, "huge"), max_tokens=50)
        self.assertEqual(s, 200, resp)
        self.assertTrue(self.events(aid, "leash") or self.audits(aid, "leash.dump_alert"))
        self.assertTrue(self.audits(aid, "leash.dump_alert"))

    def test_6_no_leash_unchanged(self):
        aid, key = self.new_agent("free", leashed=False)
        n = len(self.sent)
        s, resp, _ = self.chat(key, words(32000), max_tokens=4000)
        self.assertEqual(s, 200, resp)
        self.assertEqual(self.sent[n][1]["max_tokens"], 4000)
        self.assertNotIn("leash", self.store.ledger(aid)[0]["note"])
        self.assertFalse(self.events(aid, "leash"))
        self.assertFalse(self.audits(aid, "leash.blocked"))


class LeashE2EPublicDemo(LeashE2E):
    """Scenario 7: the same behaviour in public demo mode (simulated models only)."""
    public = True

    def test_7_demo_mode_flag_and_real_models_refused(self):
        self.assertTrue(self.app.public_demo)
        aid, key = self.new_agent("demo")
        s, resp, _ = self.chat(key, "hi", model="claude-opus-5-5")
        self.assertEqual(s, 403)
        s, resp, _ = self.chat(key, "hi", model="sim-small")
        self.assertEqual(s, 200, resp)


if __name__ == "__main__":
    unittest.main()

