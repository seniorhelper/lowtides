"""Public demo mode: no password, simulated models only, sandbox only, resettable."""
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import fakes
from helpers import config
from test_http import http

from lowtides.paypal import PayPal
from lowtides.server import App, make_handler
from lowtides.store import Store


class TestPublicDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pp, cls.pp_url = fakes.start(fakes.FakePayPalHandler, fakes.paypal_state())
        os.environ["LOWTIDES_PUBLIC_DEMO"] = "1"
        os.environ["LOWTIDES_ADMIN_TOKEN"] = "would-normally-be-required"
        try:
            cls.app = App(config(), Store(":memory:"), paypal=PayPal("client", "secret", base_url=cls.pp_url),
                          config_path=os.path.join(os.path.dirname(__file__), "_tmp_config_demo.json"))
        finally:
            os.environ.pop("LOWTIDES_PUBLIC_DEMO", None)
            os.environ["LOWTIDES_ADMIN_TOKEN"] = ""
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.app.self_url = cls.url

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_dashboard_needs_no_password(self):
        s, st, _ = http("GET", self.url + "/api/state")
        self.assertEqual(s, 200)
        self.assertTrue(st["public_demo"])
        self.assertFalse(st["admin_protected"])

    def test_still_refuses_cross_site(self):
        s, _, _ = http("POST", self.url + "/api/stop-all", {}, {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)

    def test_only_simulated_models(self):
        s, r, _ = http("POST", self.url + "/api/agents", {"name": "Visitor"})
        key = r["agent"]["key"]
        s, r, _ = http("POST", self.url + "/v1/chat/completions",
                       {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}, {"Authorization": "Bearer " + key})
        self.assertEqual(s, 403)
        s, r, _ = http("POST", self.url + "/v1/chat/completions",
                       {"model": "sim-small", "messages": [{"role": "user", "content": "hi"}]}, {"Authorization": "Bearer " + key})
        self.assertEqual(s, 200)

    def test_try_box_and_engine_refuse_paid_models(self):
        s, r, _ = http("POST", self.url + "/api/agents", {"name": "Tryer"})
        aid, key = r["agent"]["id"], r["agent"]["key"]
        s, r, _ = http("POST", self.url + "/api/try", {"agent_id": aid, "model": "claude-opus-5-5", "prompt": "hi"})
        self.assertEqual(s, 403)
        # Even a route that reaches the engine directly can't get past it.
        status, resp, _ = self.app.engine.handle("anthropic", {"model": "claude-opus-5-5", "max_tokens": 5,
                                                              "messages": [{"role": "user", "content": "hi"}]}, key, {})
        self.assertEqual(status, 403)
        s, r, _ = http("POST", self.url + "/api/try", {"agent_id": aid, "model": "sim-large", "prompt": "hi"})
        self.assertEqual((s, r["status"]), (200, 200))

    def test_visitors_cannot_change_the_governor(self):
        s, r, _ = http("POST", self.url + "/api/policy", {"loop_max_repeats": "x"})
        self.assertEqual(s, 403)
        s, r, _ = http("POST", self.url + "/api/policy", {"mode": "business"})
        self.assertEqual(s, 200)
        http("POST", self.url + "/api/policy", {"mode": "home"})

    def test_reset_restores_policy_and_body_cap(self):
        self.app.config.data["policy"]["loop_max_repeats"] = 999
        self.app.reset_demo(seed_story=False)
        self.assertNotEqual(self.app.config.policy.get("loop_max_repeats"), 999)
        req = urllib.request.Request(self.url + "/api/agents", data=b"{" + b" " * (1200 * 1024) + b"}", method="POST",
                                     headers={"Content-Type": "application/json"})
        # The server refuses before the body is all sent, so the client sees either the
        # error status or a closed connection; both mean the oversized body was turned away.
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("an oversized body was accepted")
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (400, 413))
        except (urllib.error.URLError, ConnectionError):
            pass

    def test_connections_locked(self):
        s, _, _ = http("POST", self.url + "/api/providers", {"name": "x", "base_url": "https://evil.example", "api_key_env": "X_API_KEY"})
        self.assertEqual(s, 403)
        s, _, _ = http("DELETE", self.url + "/api/models/sim-small")
        self.assertEqual(s, 403)

    def test_reset_loads_the_story(self):
        s, _, _ = http("POST", self.url + "/api/demo/reset", {})
        self.assertEqual(s, 200)
        reqs = self.app.store.spends()
        decisions = sorted(r["decision"] for r in reqs)
        self.assertIn("needs_approval", decisions)
        self.assertIn("denied", decisions)
        self.assertEqual(len([r for r in reqs if r["status"] == "pending"]), 2)  # left for the visitor
        agents = self.app.store.agents()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["status"], "paused")  # the loop guard caught it
        # the seeded outcomes: $12 paid, $19 and $45 held, $480 blocked
        by_amt = {round(r["amount"]): r["decision"] for r in reqs}
        self.assertEqual(by_amt, {12: "approved", 19: "needs_approval", 480: "denied", 45: "needs_approval"})
        # the leash refused the 80k-token data dump with a readable reason
        leash = [e for e in self.app.store.events(200) if e["kind"] == "leash"]
        self.assertEqual(len(leash), 1)
        self.assertIn("blocked a 80", leash[0]["message"])
        self.assertTrue(self.app.store.audit_verify()["ok"])

    def test_story_rules_include_the_leash(self):
        from lowtides.scenario import RULES_TEXT, data_dump
        from lowtides.spend import parse_rules_offline
        rules, _ = parse_rules_offline(RULES_TEXT)
        self.assertEqual(rules["max_input_tokens"], 25000)
        self.assertAlmostEqual(rules["max_cost_per_call"], 0.25)
        self.assertTrue(rules["max_reply_tokens"])
        self.assertGreater(len(data_dump()) / 4, 79000)


class TestDemoResetGuard(unittest.TestCase):
    def test_reset_refused_outside_demo(self):
        app = App(config(), Store(":memory:"), paypal=PayPal("", ""))
        with self.assertRaises(ValueError):
            app.reset_demo()


if __name__ == "__main__":
    unittest.main()
