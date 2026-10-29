"""Public demo mode: no password, simulated models only, sandbox only, resettable."""
import os
import threading
import time
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
        cls.app.limiter.burst = cls.app.limiter.rate = 10 ** 6  # this class fires requests faster than any visitor

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

    def test_reset_clears_webhooks(self):
        n = self.app.notify
        n.add("https://hooks.zapier.com/hooks/catch/1/reset/")
        n._log(1, {"id": "evt_x", "type": "test.ping"}, 1, False, None, "x", 0)
        for t in ("outbound_webhooks", "webhook_deliveries"):
            self.assertTrue(self.app.store.q("SELECT * FROM " + t), t)
        self.app.reset_demo(seed_story=False)
        for t in ("outbound_webhooks", "webhook_deliveries"):
            self.assertEqual(self.app.store.q("SELECT * FROM " + t), [], t)
        self.assertEqual(n.hooks(), [])

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

    def test_demo_seed_has_simulated_paypal_on_file_and_kill_revokes_it(self):
        s, _, _ = http("POST", self.url + "/api/demo/reset", {})
        self.assertEqual(s, 200)
        s, st, _ = http("GET", self.url + "/api/state")
        a = st["agents"][0]
        self.assertEqual((a["paypal_on_file"]["status"], a["paypal_on_file"]["simulated"]), ("active", True))
        self.assertTrue(st["vault_simulated"])  # the demo never sends strangers through a real vault approval
        self.assertFalse(self.pp.state.get("setup_tokens"))
        s, k, _ = http("POST", self.url + "/api/agents/%d/kill" % a["id"], {})
        self.assertEqual((k["tokens_revoked"], k["paypal_on_file"]["status"]), (1, "revoked"))
        self.assertIn("can't be charged again until you reconnect it", k["paypal_on_file"]["message"])
        s, st, _ = http("GET", self.url + "/api/state")
        self.assertEqual(st["agents"][0]["paypal_on_file"]["label"], "revoked")
        self.assertTrue(any("Revoked the saved PayPal payment method" in e["message"] for e in st["events"]))

    def test_story_rules_include_the_leash(self):
        from lowtides.scenario import RULES_TEXT, data_dump
        from lowtides.spend import parse_rules_offline
        rules, _ = parse_rules_offline(RULES_TEXT)
        self.assertEqual(rules["max_input_tokens"], 25000)
        self.assertAlmostEqual(rules["max_cost_per_call"], 0.25)
        self.assertTrue(rules["max_reply_tokens"])
        self.assertGreater(len(data_dump()) / 4, 79000)


def demo_server(**kw):
    os.environ["LOWTIDES_PUBLIC_DEMO"] = "1"
    try:
        app = App(config(), Store(":memory:"), paypal=PayPal("", ""))
    finally:
        os.environ.pop("LOWTIDES_PUBLIC_DEMO", None)
    for k, v in kw.items():
        setattr(app.sandboxes, k, v)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.self_url = "http://127.0.0.1:%d" % srv.server_address[1]
    return app, srv, app.self_url


class Browser:
    """A visitor: picks up the lt_demo cookie from the page and sends it back."""

    def __init__(self, url):
        self.url = url
        s, _, h = http("GET", url + "/")
        self.cookie = h["Set-Cookie"].split(";")[0]
        assert self.cookie.startswith("lt_demo=")

    def __call__(self, method, path, body=None, headers=None):
        return http(method, self.url + path, body, dict(headers or {}, Cookie=self.cookie))


class TestSandboxes(unittest.TestCase):
    """Every browser gets its own demo; nobody can wreck another visitor's."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_cookie_flags(self):
        s, _, h = http("GET", self.url + "/", headers={"X-Forwarded-Proto": "https"})
        c = h["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(flag, c)
        s, _, h = http("GET", self.url + "/")
        self.assertNotIn("Secure", h["Set-Cookie"])
        # a page load that already has a valid cookie gets no new one
        b = Browser(self.url)
        s, _, h = b("GET", "/")
        self.assertNotIn("Set-Cookie", h)

    def test_each_browser_gets_its_own_seeded_story(self):
        a, b = Browser(self.url), Browser(self.url)
        sa = a("GET", "/api/state")[1]
        sb = b("GET", "/api/state")[1]
        self.assertEqual([x["name"] for x in sa["agents"]], ["Night-shift research agent"])
        self.assertEqual(len(sb["agents"]), 1)
        self.assertNotEqual(sa["agents"][0]["key"], sb["agents"][0]["key"])
        pending = a("GET", "/api/spend?status=pending")[1]["requests"]
        self.assertEqual(len(pending), 2)  # the story stops before the KILL, in each sandbox
        a("POST", "/api/agents", {"name": "Only in A"})
        names_a = [x["name"] for x in a("GET", "/api/state")[1]["agents"]]
        names_b = [x["name"] for x in b("GET", "/api/state")[1]["agents"]]
        self.assertIn("Only in A", names_a)
        self.assertNotIn("Only in A", names_b)
        # the shared cookie-less demo is untouched too
        self.assertNotIn("Only in A", [x["name"] for x in self.app.store.agents()])

    def test_stop_all_kill_reset_stay_in_their_sandbox(self):
        a, b = Browser(self.url), Browser(self.url)
        a("GET", "/api/state")
        b("GET", "/api/state")
        s, r, _ = a("POST", "/api/stop-all", {})
        self.assertEqual(s, 200)
        self.assertEqual(r["denied"], 2)
        self.assertTrue(all(x["status"] == "stopped" for x in a("GET", "/api/state")[1]["agents"]))
        st_b = b("GET", "/api/state")[1]
        self.assertTrue(all(x["status"] != "stopped" for x in st_b["agents"]))
        self.assertEqual(len(b("GET", "/api/spend?status=pending")[1]["requests"]), 2)
        aid = st_b["agents"][0]["id"]
        b("POST", "/api/outside/simulate", {"amount": 30})
        self.assertFalse(a("GET", "/api/outside")[1]["tripped"])
        self.assertTrue(b("GET", "/api/outside")[1]["tripped"])
        # the guided replay starts with an empty reset: only the caller's sandbox is wiped
        s, _, _ = a("POST", "/api/demo/reset", {"empty": True})
        self.assertEqual(s, 200)
        self.assertEqual(a("GET", "/api/state")[1]["agents"], [])
        self.assertEqual([x["id"] for x in b("GET", "/api/state")[1]["agents"]], [aid])
        # and a full reset brings the story back, still only for the caller
        a("POST", "/api/demo/reset", {})
        self.assertEqual(len(a("GET", "/api/state")[1]["agents"]), 1)
        self.assertEqual(len(a("GET", "/api/spend?status=pending")[1]["requests"]), 2)
        self.assertEqual([x["id"] for x in b("GET", "/api/state")[1]["agents"]], [aid])
        self.assertTrue(b("GET", "/api/outside")[1]["tripped"])  # b's simulated runaway banner is still b's

    def test_agent_key_routes_to_its_sandbox(self):
        a, b = Browser(self.url), Browser(self.url)
        key = a("POST", "/api/agents", {"name": "Keyed"})[1]["agent"]["key"]
        b("GET", "/api/state")
        s, r, _ = http("POST", self.url + "/v1/spend", {"amount": 3, "merchant": "Acme Data", "item": "x"},
                       {"Authorization": "Bearer " + key})
        self.assertEqual(s, 200)
        sid = r["id"]
        self.assertIn(sid, [x["id"] for x in a("GET", "/api/spend")[1]["requests"]])
        self.assertNotIn(sid, [x["id"] for x in b("GET", "/api/spend")[1]["requests"]])
        s, _, _ = http("POST", self.url + "/v1/chat/completions",
                       {"model": "sim-small", "messages": [{"role": "user", "content": "hi"}]}, {"x-api-key": key})
        self.assertEqual(s, 200)
        s, budget, _ = http("GET", self.url + "/v1/budget", headers={"Authorization": "Bearer " + key})
        self.assertEqual((s, budget["agent"]), (200, "Keyed"))
        # after the owner's reset the old key is gone everywhere
        a("POST", "/api/demo/reset", {"empty": True})
        s, _, _ = http("GET", self.url + "/v1/budget", headers={"Authorization": "Bearer " + key})
        self.assertEqual(s, 401)

    def test_forged_cookie_gets_no_sandbox(self):
        n = self.app.sandboxes.count()
        s, st, _ = http("GET", self.url + "/api/state", headers={"Cookie": "lt_demo=made-up.0000"})
        self.assertEqual(s, 200)
        self.assertEqual(self.app.sandboxes.count(), n)  # served by the shared demo instead

    def test_cross_site_does_not_build_a_sandbox(self):
        b = Browser(self.url)
        n = self.app.sandboxes.count()
        s, _, _ = b("POST", "/api/stop-all", {}, {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(self.app.sandboxes.count(), n)


class TestReviewFixes(unittest.TestCase):
    """Fixes from the independent review before release."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_stale_cookie_gets_its_own_sandbox_not_the_shared_demo(self):
        before = self.app.sandboxes.count()
        s, st, h = http("GET", self.url + "/api/state",
                        headers={"Cookie": "lt_demo=old.forged", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(s, 200)
        self.assertIn("lt_demo=", h.get("Set-Cookie", ""))
        self.assertEqual(self.app.sandboxes.count(), before + 1)
        # a script without browser fetch headers still gets the shared demo, no sandbox
        s, _, h = http("GET", self.url + "/api/state")
        self.assertNotIn("Set-Cookie", h)
        self.assertEqual(self.app.sandboxes.count(), before + 1)

    def test_dropped_sandbox_stops_its_webhook_threads(self):
        import time as _t
        b = Browser(self.url)
        b("GET", "/api/state")
        sid = self.app.sandboxes.parse(b.cookie.split("=", 1)[1])
        sapp = self.app.sandboxes.live[sid]
        n = sapp.notify
        n._start()
        self.assertTrue(any(t.is_alive() for t in n._threads))
        threads = list(n._threads)
        with self.app.sandboxes.lock:
            self.app.sandboxes._drop(sid)
        deadline = _t.time() + 5
        while any(t.is_alive() for t in threads) and _t.time() < deadline:
            _t.sleep(0.05)
        self.assertFalse(any(t.is_alive() for t in threads))

    def test_reset_restores_mode_and_bad_wallet_id_is_400(self):
        b = Browser(self.url)
        b("GET", "/api/state")
        self.assertEqual(b("POST", "/api/policy", {"mode": "business"})[0], 200)
        b("POST", "/api/demo/reset", {})
        self.assertEqual(b("GET", "/api/state")[1]["mode"], "home")
        self.assertEqual(b("GET", "/api/wallet/abc")[0], 400)

    def test_deleting_an_agent_revokes_its_saved_paypal(self):
        b = Browser(self.url)
        a = b("GET", "/api/state")[1]["agents"][0]
        self.assertEqual(a["paypal_on_file"]["status"], "active")
        self.assertEqual(b("DELETE", "/api/agents/%d" % a["id"])[0], 200)
        sid = self.app.sandboxes.parse(b.cookie.split("=", 1)[1])
        row = self.app.sandboxes.live[sid].store.vault_current(a["id"])
        self.assertTrue(row is None or row.get("status") == "revoked", row)


class TestSandboxLimits(unittest.TestCase):
    def setUp(self):
        self.app, self.srv, self.url = demo_server(seed=False)
        self.sb = self.app.sandboxes

    def tearDown(self):
        self.srv.shutdown()

    def test_lru_cap(self):
        self.sb.cap = 3
        bs = [Browser(self.url) for _ in range(4)]
        for b in bs[:3]:
            b("POST", "/api/agents", {"name": "mine"})
        bs[0]("GET", "/api/state")  # 0 is now the most recently used; 1 is the oldest
        bs[3]("GET", "/api/state")
        self.assertEqual(self.sb.count(), 3)
        self.assertEqual(len(bs[0]("GET", "/api/state")[1]["agents"]), 1)
        self.assertEqual(len(bs[2]("GET", "/api/state")[1]["agents"]), 1)
        self.assertEqual(bs[1]("GET", "/api/state")[1]["agents"], [])  # evicted: a fresh sandbox

    def test_idle_expiry(self):
        b = Browser(self.url)
        key = b("POST", "/api/agents", {"name": "mine"})[1]["agent"]["key"]
        self.assertEqual(self.sb.count(), 1)
        self.sb.sweep(now=time.time() + self.sb.idle + 1)
        self.assertEqual(self.sb.count(), 0)
        self.assertIsNone(self.sb.by_key(key))
        self.assertEqual(b("GET", "/api/state")[1]["agents"], [])  # comes back fresh
        # an idle sandbox is also replaced on access, between sweeps
        b("POST", "/api/agents", {"name": "again"})
        sid = next(iter(self.sb.seen))
        self.sb.seen[sid] -= self.sb.idle + 1
        self.assertEqual(b("GET", "/api/state")[1]["agents"], [])

    def test_memory_budget_evicts_oldest(self):
        a, b = Browser(self.url), Browser(self.url)
        a("POST", "/api/agents", {"name": "mine"})
        self.assertGreater(self.sb.db_bytes(), 0)
        self.sb.budget = 1  # anything over one byte: only the newest sandbox may stay
        b("GET", "/api/state")
        self.assertEqual(self.sb.count(), 1)
        self.assertEqual(a("GET", "/api/state")[1]["agents"], [])

    def test_sandbox_database_is_capped(self):
        from lowtides.sandbox import SANDBOX_DB_MB
        b = Browser(self.url)
        b("GET", "/api/state")
        store = next(iter(self.sb.live.values())).store
        self.assertEqual(store.db.execute("PRAGMA max_page_count").fetchone()[0], SANDBOX_DB_MB * 256)

    def test_odd_cookie_values_are_ignored(self):
        for v in ("lt_demo=%C3%A9.%C3%A9", "lt_demo=.", "lt_demo=" + "a" * 500 + ".b"):
            s, _, _ = http("GET", self.url + "/api/state", headers={"Cookie": v})
            self.assertEqual(s, 200)
        self.assertEqual(self.sb.count(), 0)

    def test_new_sandboxes_per_ip_capped(self):
        self.sb.per_ip_hour = 2
        bs = [Browser(self.url) for _ in range(3)]
        self.assertEqual(bs[0]("GET", "/api/state")[0], 200)
        self.assertEqual(bs[1]("GET", "/api/state")[0], 200)
        s, r, _ = bs[2]("GET", "/api/state")
        self.assertEqual(s, 200)  # over the cap: shares the common demo instead of being turned away
        self.assertEqual(self.sb.count(), 2)
        self.assertEqual(bs[0]("GET", "/api/state")[0], 200)  # existing ones keep working

    def test_rate_limit_429(self):
        self.app.limiter.rate, self.app.limiter.burst = 0.001, 5
        self.app.limiter.buckets.clear()
        b = Browser(self.url)
        codes = [b("POST", "/api/merchants", {"name": "M%d" % i})[0] for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])
        s, r, h = b("POST", "/api/merchants", {"name": "late"})
        self.assertIn("Wait a few seconds", r["error"])
        self.assertEqual(h.get("Retry-After"), "5")
        s, _, _ = http("POST", self.url + "/v1/spend", {"amount": 1, "merchant": "x"}, {"Authorization": "Bearer nope"})
        self.assertEqual(s, 429)  # agent API too
        self.assertEqual(b("GET", "/api/state")[0], 200)  # reads are not limited

    def test_limiter_refills(self):
        from lowtides.sandbox import RateLimiter
        lim = RateLimiter(rate=10, burst=2)
        self.assertTrue(lim.allow("ip", now=100.0))
        self.assertTrue(lim.allow("ip", now=100.0))
        self.assertFalse(lim.allow("ip", now=100.0))
        self.assertTrue(lim.allow("ip", now=100.2))
        lim.prune(now=1000.0)
        self.assertEqual(lim.buckets, {})


class TestPrivateModeUnchanged(unittest.TestCase):
    def test_no_sandboxes_or_cookie(self):
        app = App(config(), Store(":memory:"), paypal=PayPal("", ""))
        self.assertIsNone(app.sandboxes)
        self.assertIsNone(app.limiter)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            s, _, h = http("GET", url + "/")
            self.assertNotIn("Set-Cookie", h)
            http("POST", url + "/api/agents", {"name": "One"})
            s, st, _ = http("GET", url + "/api/state", headers={"Cookie": "lt_demo=anything"})
            self.assertEqual([a["name"] for a in st["agents"]], ["One"])
            codes = {http("POST", url + "/api/merchants", {"name": "M%d" % i})[0] for i in range(60)}
            self.assertEqual(codes, {200})  # no rate limit outside the demo
        finally:
            srv.shutdown()


class TestDemoResetGuard(unittest.TestCase):
    def test_reset_refused_outside_demo(self):
        app = App(config(), Store(":memory:"), paypal=PayPal("", ""))
        with self.assertRaises(ValueError):
            app.reset_demo()


if __name__ == "__main__":
    unittest.main()
