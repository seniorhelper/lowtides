"""Public demo sandboxes and the Lowtides Pass: one signing key, and a pass verifies against
the sandbox that issued it, whoever asks (a merchant with no cookie, another visitor)."""
import unittest

from test_demo import Browser, demo_server
from test_http import http

from lowtides import ed25519, passport


class TestPassAcrossSandboxes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()
        cls.app.limiter.burst = cls.app.limiter.rate = 10 ** 6  # this class fires requests faster than any visitor

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    # ---- helpers ------------------------------------------------------------
    def visitor_with_pass(self, name="Pass agent"):
        """A fresh visitor, a running agent in their sandbox, and that agent's pass."""
        v = Browser(self.url)
        r = v("POST", "/api/agents", {"name": name})[1]["agent"]
        s, p, _ = v("POST", "/api/agents/%d/pass" % r["id"], {})
        self.assertEqual(s, 200, p)
        return v, r, p["pass"]["token"]

    def verify(self, token, headers=None, **kw):
        s, out, _ = http("POST", self.url + "/v1/pass/verify", dict(kw, **{"pass": token}), headers)
        self.assertEqual(s, 200, out)
        return out

    def sid(self, visitor):
        return self.app.sandboxes.parse(visitor.cookie.split("=", 1)[1])

    # ---- tests --------------------------------------------------------------
    def test_two_cookies_two_sandboxes_one_key(self):
        a, _, ta = self.visitor_with_pass("A")
        b, _, tb = self.visitor_with_pass("B")
        sa, sb = self.app.sandboxes.live[self.sid(a)], self.app.sandboxes.live[self.sid(b)]
        self.assertIsNot(sa, sb)
        kids = {passport.book(x)._key()["kid"] for x in (self.app, sa, sb)}
        self.assertEqual(len(kids), 1)  # every sandbox signs with the root's key
        self.assertEqual(len(passport.book(self.app).jwks()["keys"]), 1)

    def test_cookieless_verify_finds_the_issuing_sandbox(self):
        a, agent, token = self.visitor_with_pass()
        out = self.verify(token)
        self.assertTrue(out["valid"], out)
        self.assertEqual((out["agent"]["id"], out["agent"]["status"]), (agent["id"], "on"))
        # the purchase check runs against that sandbox's rules
        self.assertEqual(self.verify(token, amount=1)["would_allow"], "allowed")
        self.assertEqual(self.verify(token, amount=100000)["would_allow"], "denied")
        # an agent key from another sandbox, or another visitor's cookie, changes nothing
        b = Browser(self.url)
        key_b = b("POST", "/api/agents", {"name": "B"})[1]["agent"]["key"]
        self.assertTrue(self.verify(token, {"Authorization": "Bearer " + key_b})["valid"])
        self.assertTrue(self.verify(token, {"Cookie": b.cookie})["valid"])

    def test_kill_in_a_revokes_and_b_cannot_touch_it(self):
        a, agent, token = self.visitor_with_pass()
        b = Browser(self.url)
        b("GET", "/api/state")
        # B's own agent may have the same id; B killing it, or STOP ALL in B, leaves A's pass alone
        bid = b("POST", "/api/agents", {"name": "B"})[1]["agent"]["id"]
        b("POST", "/api/agents/%d/kill" % bid, {})
        b("POST", "/api/stop-all", {})
        b("POST", "/api/agents/%d/pass/revoke" % agent["id"], {})  # a 404 or B's own agent, never A's
        self.assertTrue(self.verify(token)["valid"])
        s, _, _ = a("POST", "/api/agents/%d/kill" % agent["id"], {})
        self.assertEqual(s, 200)
        out = self.verify(token)
        self.assertFalse(out["valid"])
        self.assertTrue(out["revoked"])
        self.assertIn("Stop all", out["revoked_reason"])

    def test_stop_all_owner_revoke_and_loop_guard_revoke(self):
        a, agent, token = self.visitor_with_pass()
        a("POST", "/api/stop-all", {})
        self.assertTrue(self.verify(token)["revoked"])
        a2, agent2, token2 = self.visitor_with_pass()
        a2("POST", "/api/agents/%d/pass/revoke" % agent2["id"], {})
        self.assertTrue(self.verify(token2)["revoked"])
        a3, agent3, token3 = self.visitor_with_pass()
        box = self.app.sandboxes.live[self.sid(a3)]
        box.store.run("UPDATE agents SET status='paused' WHERE id=?", (agent3["id"],))
        out = self.verify(token3)
        self.assertTrue(out["revoked"])
        self.assertIn("loop guard", out["revoked_reason"])

    def test_agent_fetched_pass_verifies_too(self):
        a, agent, _ = self.visitor_with_pass()
        s, p, _ = http("GET", self.url + "/v1/pass", headers={"Authorization": "Bearer " + agent["key"]})
        self.assertEqual(s, 200, p)
        self.assertTrue(self.verify(p["pass"])["valid"])

    def test_signature_checks_offline_with_published_keys(self):
        _, _, token = self.visitor_with_pass()
        s, jwks, _ = http("GET", self.url + "/.well-known/lowtides-pass-keys.json")
        self.assertEqual(s, 200)
        h, p, sig = token.split(".")
        kid = passport.json.loads(passport.b64d(h))["kid"]
        key = [k for k in jwks["keys"] if k["kid"] == kid]
        self.assertEqual(len(key), 1)
        self.assertTrue(ed25519.verify(passport.b64d(key[0]["x"]), (h + "." + p).encode(), passport.b64d(sig)))

    def test_evicted_or_reset_sandbox_reads_friendly_expired(self):
        a, _, token = self.visitor_with_pass()
        with self.app.sandboxes.lock:
            self.app.sandboxes._drop(self.sid(a))
        out = self.verify(token, amount=5)
        self.assertFalse(out["valid"])
        self.assertTrue(out["expired"] and out["gone"])
        self.assertTrue(out["signature_ok"])
        self.assertIn("demo sandbox", out["reasons"][0])
        self.assertEqual(out["would_allow"], "denied")
        # a reset forgets the old passes the same way
        b, _, token_b = self.visitor_with_pass()
        b("POST", "/api/demo/reset", {"empty": True})
        self.assertTrue(self.verify(token_b)["gone"])

    def test_shared_demo_passes_still_verify(self):
        # cookie-less callers (curl, the scenario CLI) use the shared root demo, as before
        r = http("POST", self.url + "/api/agents", {"name": "Root agent"})[1]["agent"]
        s, p, _ = http("POST", self.url + "/api/agents/%d/pass" % r["id"], {})
        self.assertEqual(s, 200, p)
        self.assertTrue(self.verify(p["pass"]["token"])["valid"])

    def test_junk_is_still_a_clean_400(self):
        s, out, _ = http("POST", self.url + "/v1/pass/verify", {"pass": "a.b.c"})
        self.assertEqual(s, 400)
        s, out, _ = http("POST", self.url + "/v1/pass/verify", {"pass": 12})
        self.assertEqual(s, 400)


if __name__ == "__main__":
    unittest.main()
