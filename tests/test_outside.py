"""Outside spend guard: real Admin API clients against local fakes that speak the
Anthropic cost_report / api_keys and OpenAI costs / project api_keys wire formats.
No real admin API is ever called."""
import datetime
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import fakes
from helpers import config
from test_http import http

from lowtides.outside import AnthropicCosts, OpenAICosts, OutsideGuard, ProviderError
from lowtides.server import App, make_handler
from lowtides.store import Store

NOW = datetime.datetime(2026, 10, 29, 15, 0, tzinfo=datetime.timezone.utc)
ANT_KEY, OAI_KEY = "sk-ant-admin-fake", "sk-admin-fake"


class FakeAnthropicAdmin(fakes._Base):
    def _auth(self):
        st = self.server.state
        st["calls"].append((self.command, self.path))
        if self.headers.get("x-api-key") != ANT_KEY or not self.headers.get("anthropic-version"):
            self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            return False
        if st.get("fail"):
            code = st["fail"]
            body = json.dumps({"type": "error", "error": {"type": "rate_limit_error" if code == 429 else "api_error",
                                                          "message": "fake failure"}}).encode()
            self.send_response(code)
            if code == 429:
                self.send_header("Retry-After", "600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        st = self.server.state
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path != "/v1/organizations/cost_report":
            return self._send(404, {"type": "error"})
        buckets = []
        for day, cents in sorted(st["days"].items()):
            buckets.append({"starting_at": day + "T00:00:00Z", "ending_at": "x",
                            "results": [{"amount": "%.2f" % cents, "currency": "USD"}] if cents else []})
        # two pages, like a real paginated report
        half = len(buckets) // 2
        if q.get("page") == ["p2"]:
            return self._send(200, {"data": buckets[half:], "has_more": False, "next_page": None})
        return self._send(200, {"data": buckets[:half], "has_more": True, "next_page": "p2"})

    def do_POST(self):
        if not self._auth():
            return
        body = self._json()
        kid = self.path.rsplit("/", 1)[-1]
        if kid not in self.server.state["keys"]:
            return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "no key"}})
        self.server.state["keys"][kid] = body.get("status", "active")
        return self._send(200, {"type": "api_key", "id": kid, "status": self.server.state["keys"][kid]})


class FakeOpenAIAdmin(fakes._Base):
    def _auth(self):
        self.server.state["calls"].append((self.command, self.path))
        if self.headers.get("Authorization") != "Bearer " + OAI_KEY:
            self._send(401, {"error": {"message": "bad key"}})
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        st = self.server.state
        data = [{"object": "bucket", "start_time": int(datetime.datetime.strptime(d, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc).timestamp()), "end_time": 0,
            "results": [{"object": "organization.costs.result", "amount": {"value": v, "currency": "usd"}}]}
            for d, v in sorted(st["days"].items())]
        return self._send(200, {"object": "page", "data": data, "has_more": False, "next_page": None})

    def do_DELETE(self):
        if not self._auth():
            return
        parts = self.path.strip("/").split("/")  # v1 organization projects P api_keys K
        key = parts[3] + "/" + parts[5]
        if key not in self.server.state["keys"]:
            return self._send(404, {"error": {"message": "not found"}})
        del self.server.state["keys"][key]
        return self._send(200, {"id": parts[5], "deleted": True, "object": "organization.project.api_key.deleted"})


def ant_state(today_dollars=1.0, earlier_dollars=10.0):
    return {"calls": [], "keys": {"apikey_runaway": "active", "apikey_prod": "active"},
            "days": {"2026-10-01": earlier_dollars * 100, "2026-10-15": 0, "2026-10-29": today_dollars * 100}}


class Base(unittest.TestCase):
    def setUp(self):
        self.ant, self.ant_url = fakes.start(FakeAnthropicAdmin, ant_state())
        self.oai, self.oai_url = fakes.start(FakeOpenAIAdmin, {"calls": [], "keys": {"proj_a/key_1": 1, "proj_a/key_2": 1},
                                                              "days": {"2026-10-02": 3.0, "2026-10-29": 0.5}})
        self.store = Store(":memory:")
        self.stops = []
        self.cfg = config()
        self.cfg.data["outside"] = {"enabled": True, "providers": {
            "anthropic": {"enabled": True, "daily_cap": 20, "monthly_cap": 100, "protect_keys": ["apikey_runaway"]}}}
        self.guard = OutsideGuard(self.cfg, self.store, stop_all=self.stops.append, now=lambda: NOW, clients={
            "anthropic": AnthropicCosts("anthropic", self.ant_url, ANT_KEY),
            "openai": OpenAICosts("openai", self.oai_url, OAI_KEY)})

    def tearDown(self):
        self.ant.shutdown()
        self.oai.shutdown()

    def anth(self):
        return self.guard.status()["providers"][0]


class TestOutsideGuard(Base):
    def test_under_cap_reads_real_spend_and_does_nothing(self):
        self.guard.check()
        p = self.anth()
        self.assertEqual((p["today"], p["month"]), (1.0, 11.0))  # cents -> dollars, both pages summed
        self.assertEqual(p["level"], "ok")
        self.assertEqual(self.ant.state["keys"]["apikey_runaway"], "active")
        self.assertEqual(self.store.audit_entries(), [])
        self.assertEqual(self.stops, [])
        self.assertIsNone(self.guard.status()["tripped"])

    def test_warn_records_one_alert_and_leaves_keys(self):
        self.ant.state["days"]["2026-10-29"] = 17 * 100  # 85% of $20/day
        self.guard.check()
        self.guard.check()
        self.assertEqual(self.anth()["level"], "warn")
        alerts = [e for e in self.store.events(50) if e["kind"] == "outside"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("past 80%", alerts[0]["message"])
        self.assertEqual(self.ant.state["keys"]["apikey_runaway"], "active")
        self.assertEqual(self.stops, [])

    def test_over_cap_disables_only_listed_keys_audits_and_stops_agents(self):
        self.ant.state["days"]["2026-10-29"] = 21 * 100
        st = self.guard.check()
        self.assertEqual(self.ant.state["keys"], {"apikey_runaway": "inactive", "apikey_prod": "active"})
        self.assertEqual(st["tripped"]["keys"][0]["result"], "disabled")
        self.assertEqual(self.stops, ["OUTSIDE CAP"])
        audit = self.store.audit_entries()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["kind"], "outside.cap")
        self.assertTrue(self.store.audit_verify()["ok"])
        # Banner up: later polls do not re-trip or spam the audit log.
        self.guard.check()
        self.assertEqual(len(self.store.audit_entries()), 1)
        # Owner clears the banner while still over: it trips again (raise the cap to resume).
        self.guard.clear_banner()
        self.guard.check()
        self.assertEqual(len(self.store.audit_entries()), 2)

    def test_monthly_and_combined_caps(self):
        self.cfg.data["outside"]["providers"]["openai"] = {"enabled": True, "daily_cap": 0, "monthly_cap": 0,
                                                           "protect_keys": ["proj_a/key_1"]}
        self.cfg.data["outside"]["combined"] = {"daily_cap": 0, "monthly_cap": 14}
        st = self.guard.check()  # anthropic $11 + openai $3.50 = $14.50 this month
        self.assertEqual(st["combined"]["month"], 14.5)
        self.assertEqual(st["tripped"]["scope"], "combined")
        self.assertEqual(self.ant.state["keys"]["apikey_runaway"], "inactive")
        self.assertEqual(self.oai.state["keys"], {"proj_a/key_2": 1})  # only the listed key deleted

    def test_openai_404_is_failed_not_deleted(self):
        c = OpenAICosts("openai", self.oai_url, OAI_KEY)
        with self.assertRaises(ProviderError) as cm:
            c.disable("proj_a/typo")
        self.assertIn("not found", str(cm.exception))
        self.assertEqual(c.spend(NOW), (0.5, 3.5))
        # Through the guard: the banner and panel say the key is NOT protected.
        self.cfg.data["outside"]["providers"]["openai"] = {"enabled": True, "daily_cap": 0.1, "monthly_cap": 0,
                                                           "protect_keys": ["proj_a/typo"]}
        self.cfg.data["outside"]["providers"]["anthropic"]["enabled"] = False
        st = self.guard.check()
        k = st["tripped"]["keys"][0]
        self.assertEqual(k["result"], "failed")
        self.assertIn("check the project/key ID", k["error"])
        self.assertEqual(st["tripped"]["problems"], [k])
        oai = [p for p in st["providers"] if p["name"] == "openai"][0]
        self.assertFalse(oai["keys"][0]["protected"])
        self.assertIn("NOT protected", oai["error"])
        self.assertIn("NOT protected", st["alerts"][0]["message"])
        self.assertEqual(self.oai.state["keys"], {"proj_a/key_1": 1, "proj_a/key_2": 1})

    def test_non_finite_and_out_of_range_settings_are_refused(self):
        bad = [{"combined": {"daily_cap": float("inf")}}, {"combined": {"monthly_cap": float("nan")}},
               {"providers": {"anthropic": {"daily_cap": 1e999}}}, {"providers": {"anthropic": {"daily_cap": -1}}},
               {"providers": {"anthropic": {"monthly_cap": 1000001}}}, {"providers": {"anthropic": {"daily_cap": "lots"}}},
               {"poll_minutes": 0}, {"poll_minutes": 1441}, {"poll_minutes": float("inf")},
               {"warn_percent": 0}, {"warn_percent": 101}, {"warn_percent": float("nan")}]
        before = json.dumps(self.cfg.data["outside"], sort_keys=True)
        for d in bad:
            with self.assertRaises(ValueError, msg=d):
                self.guard.update_settings(d)
        self.assertEqual(json.dumps(self.cfg.data["outside"], sort_keys=True), before)
        s = self.guard.update_settings({"poll_minutes": 1440, "warn_percent": 100, "combined": {"daily_cap": 1000000},
                                        "providers": {"anthropic": {"daily_cap": 0}}})
        self.assertEqual((s["poll_minutes"], s["warn_percent"], s["combined"]["daily_cap"]), (1440, 100, 1000000))
        json.dumps(self.guard.status(), allow_nan=False)

    def test_provider_non_finite_amount_is_an_error(self):
        self.ant.state["days"]["2026-10-29"] = float("inf")
        self.guard.check()
        self.assertIn("not a real amount", self.anth()["error"])
        self.assertIsNone(self.guard.status()["tripped"])

    def test_key_ids_must_be_safe_segments(self):
        for ref in ("../apikey_prod", "a/b", "apikey_x?y", "..", "key id"):
            with self.assertRaises(ValueError, msg=ref):
                self.guard.update_settings({"providers": {"anthropic": {"protect_keys": [ref]}}})
        for ref in ("proj_a", "proj_a/key_1/x", "../key_1", "proj_a/..", "proj_a//key_1", "/key_1"):
            with self.assertRaises(ValueError, msg=ref):
                self.guard.update_settings({"providers": {"openai": {"protect_keys": [ref]}}})
        s = self.guard.update_settings({"providers": {"openai": {"protect_keys": ["proj_a/key_1"]},
                                                      "anthropic": {"protect_keys": ["apikey_01-AbC"]}}})
        self.assertEqual(s["providers"]["openai"]["protect_keys"], ["proj_a/key_1"])
        # The clients refuse bad IDs too, before any request.
        n = len(self.ant.state["calls"])
        with self.assertRaises(ProviderError):
            AnthropicCosts("anthropic", self.ant_url, ANT_KEY).disable("../apikey_prod")
        with self.assertRaises(ProviderError):
            OpenAICosts("openai", self.oai_url, OAI_KEY).disable("proj_a/../x")
        self.assertEqual(len(self.ant.state["calls"]), n)
        self.assertEqual(self.oai.state["calls"], [])

    def test_next_page_token_is_quoted(self):
        seen = []

        def fake_call(self_, method, path, body=None):
            seen.append(path)
            if len(seen) == 1:
                return {"data": [], "has_more": True, "next_page": "p2&limit=1/../x"}
            return {"data": [], "has_more": False}
        for cls, name in ((AnthropicCosts, "anthropic"), (OpenAICosts, "openai")):
            seen.clear()
            with mock.patch.object(cls, "_call", fake_call):
                cls(name, "http://x", "k").spend(NOW)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[1]).query)
            self.assertEqual(q["page"], ["p2&limit=1/../x"])
            self.assertEqual(q["limit"], ["31"])

    def test_status_does_not_wait_on_a_slow_provider(self):
        gate, entered = threading.Event(), threading.Event()

        class Slow:
            def spend(self, now):
                entered.set()
                gate.wait(5)
                return 1.0, 2.0
        g = OutsideGuard(self.cfg, self.store, now=lambda: NOW, clients={"anthropic": Slow()})
        t = threading.Thread(target=g.check)
        t.start()
        self.assertTrue(entered.wait(5))
        done = []
        r = threading.Thread(target=lambda: done.append(g.status()))
        r.start()
        r.join(1)
        self.assertTrue(done, "status() blocked while a provider call was in flight")
        gate.set()
        t.join(5)
        self.assertEqual(g.status()["providers"][0]["today"], 1.0)

    def test_trip_stops_agents_before_disabling_keys_and_not_under_lock(self):
        order = []
        guard = None

        class Keys:
            def spend(self, now):
                return 50.0, 50.0

            def disable(self, key_id):
                order.append("disable")
                got = []
                th = threading.Thread(target=lambda: got.append(guard.status()))
                th.start()
                th.join(1)
                order.append("status ok" if got else "status blocked")
                return "disabled"
        guard = OutsideGuard(self.cfg, self.store, stop_all=lambda by: order.append("stop_all"), now=lambda: NOW,
                             clients={"anthropic": Keys()})
        guard.check()
        self.assertEqual(order, ["stop_all", "disable", "status ok"])
        self.assertEqual(guard.status()["tripped"]["keys"][0]["result"], "disabled")

    def test_error_alert_repeats_after_clearing_or_an_hour(self):
        def errors():
            return [e for e in self.store.events(50) if e["kind"] == "outside" and "500" in e["message"]]
        self.ant.state["fail"] = 500
        self.guard.check()
        self.guard.check()
        self.assertEqual(len(errors()), 1)
        self.ant.state["fail"] = None
        self.guard.check()  # cleared
        self.ant.state["fail"] = 500
        self.guard.check()
        self.assertEqual(len(errors()), 2)
        real = time.time()
        with mock.patch("lowtides.outside.time.time", return_value=real + 3601):
            self.guard.check()
        self.assertEqual(len(errors()), 3)

    def test_yesterdays_today_figure_is_ignored_after_utc_midnight(self):
        self.ant.state["days"]["2026-10-29"] = 19 * 100
        self.guard.check()
        self.assertEqual(self.anth()["today"], 19.0)
        later = [NOW + datetime.timedelta(hours=10)]  # 01:00 UTC next day
        self.guard.now = lambda: later[0]
        self.ant.state["fail"] = 500  # no fresh read today
        self.guard.state["anthropic"]["today"] = 25.0  # would trip if still counted as today
        st = self.guard.check()
        self.assertEqual(self.anth()["today"], 0.0)
        self.assertIsNone(st["tripped"])

    def test_no_keys_listed_means_none_touched(self):
        self.cfg.data["outside"]["providers"]["anthropic"]["protect_keys"] = []
        self.ant.state["days"]["2026-10-29"] = 50 * 100
        st = self.guard.check()
        self.assertEqual(st["tripped"]["keys"], [])
        self.assertEqual(self.ant.state["keys"], {"apikey_runaway": "active", "apikey_prod": "active"})
        self.assertNotIn("POST", [m for m, _ in self.ant.state["calls"]])

    def test_api_error_keeps_last_numbers_and_never_trips(self):
        self.guard.check()
        self.ant.state["fail"] = 500
        self.guard.check()
        p = self.anth()
        self.assertIn("500", p["error"])
        self.assertEqual(p["today"], 1.0)
        self.assertIsNone(self.guard.status()["tripped"])

    def test_rate_limit_waits_for_retry_after(self):
        self.ant.state["fail"] = 429
        self.guard.check()
        self.assertIn("rate-limited", self.anth()["error"])
        n = len(self.ant.state["calls"])
        self.guard.check()  # within Retry-After: no new request
        self.assertEqual(len(self.ant.state["calls"]), n)
        self.assertEqual(self.anth()["level"], "unknown")

    def test_disable_failure_is_reported_not_hidden(self):
        self.ant.state["fail"] = 500
        t = self.guard.disable_now()
        self.assertEqual(t["keys"][0]["result"], "failed")
        self.assertIn("by hand", self.store.events(5)[0]["message"] + self.guard.status()["alerts"][0]["message"])

    def test_lag_and_stale_are_shown(self):
        self.guard.check()
        p = self.anth()
        self.assertEqual(p["lag_minutes"], 5)
        self.assertFalse(p["stale"])
        self.guard.state["anthropic"]["last_ok"] -= 3600
        self.assertTrue(self.anth()["stale"])

    def test_missing_admin_key_makes_no_calls(self):
        g = OutsideGuard(self.cfg, self.store, now=lambda: NOW, env={})
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            g.check()
        self.assertIn("ANTHROPIC_ADMIN_KEY", g.status()["providers"][0]["error"])

    def test_settings_refuse_secrets_and_never_show_admin_key(self):
        with self.assertRaises(ValueError):
            self.guard.update_settings({"providers": {"anthropic": {"protect_keys": "sk-ant-api03-secret"}}})
        self.guard.update_settings({"warn_percent": 50, "providers": {"anthropic": {"protect_keys": "apikey_a, apikey_b"}}})
        self.assertEqual(self.cfg.data["outside"]["providers"]["anthropic"]["protect_keys"], ["apikey_a", "apikey_b"])
        g = OutsideGuard(self.cfg, self.store, env={"ANTHROPIC_ADMIN_KEY": ANT_KEY})
        self.assertNotIn(ANT_KEY, json.dumps(g.status()))
        self.assertTrue(g.status()["providers"][0]["admin_key_set"])


class TestDemoNeverCallsRealApis(unittest.TestCase):
    def test_demo_guard_is_simulated_only(self):
        g = OutsideGuard(config(), Store(":memory:"), demo=True,
                         env={"ANTHROPIC_ADMIN_KEY": "x", "OPENAI_ADMIN_KEY": "y"})
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual([p["name"] for p in g.check()["providers"]], ["simulated"])
            st = g.simulate_runaway(25)
        self.assertEqual(st["tripped"]["keys"][0]["result"], "disabled")


class TestOutsideHttp(unittest.TestCase):
    def serve(self, demo):
        os.environ["LOWTIDES_PUBLIC_DEMO"] = "1" if demo else ""
        try:
            app = App(config(), Store(":memory:"), config_path=os.path.join(os.path.dirname(__file__), "_tmp_config_outside.json"))
        finally:
            os.environ.pop("LOWTIDES_PUBLIC_DEMO", None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return app, "http://127.0.0.1:%d" % srv.server_address[1]

    def tearDown(self):
        p = os.path.join(os.path.dirname(__file__), "_tmp_config_outside.json")
        if os.path.exists(p):
            os.remove(p)

    def test_demo_runaway_trips_banner_and_stop_all(self):
        app, url = self.serve(demo=True)
        app.store.add_agent("Bot", 5, 1)
        with mock.patch("lowtides.outside._Http._call", side_effect=AssertionError("network")):
            s, st, _ = http("POST", url + "/api/outside/simulate", {"amount": 30})
            self.assertEqual(s, 200)
            self.assertTrue(st["tripped"])
            s, state, _ = http("GET", url + "/api/state")
        self.assertTrue(state["outside"]["demo"])
        self.assertEqual(app.store.agents()[0]["status"], "stopped")

    def test_simulate_refused_outside_demo_and_settings_saved(self):
        app, url = self.serve(demo=False)
        s, _, _ = http("POST", url + "/api/outside/simulate", {})
        self.assertEqual(s, 403)
        s, st, _ = http("POST", url + "/api/outside/settings", {"enabled": True, "combined": {"daily_cap": 30}})
        self.assertEqual(s, 200)
        self.assertEqual(st["combined"]["daily_cap"], 30)
        s, st, _ = http("POST", url + "/api/outside/settings", {"providers": {"nope": {}}})
        self.assertEqual(s, 400)

    def test_non_finite_numbers_get_400_and_state_stays_strict_json(self):
        app, url = self.serve(demo=True)
        for body in ({"combined": {"daily_cap": float("inf")}}, {"providers": {"simulated": {"daily_cap": float("nan")}}},
                     {"poll_minutes": 0}):
            s, r, _ = http("POST", url + "/api/outside/settings", body)
            self.assertEqual(s, 400, body)
            self.assertIn("error", r)
        req = urllib.request.Request(url + "/api/outside/settings", data=b'{"combined": {"daily_cap": 1e999}}', method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        s, r, _ = http("POST", url + "/api/outside/simulate", {"amount": float("inf")})
        self.assertEqual(s, 400)
        with urllib.request.urlopen(url + "/api/state", timeout=10) as r:
            raw = r.read().decode()

        def refuse(name):
            raise AssertionError("non-finite number in /api/state: " + name)
        json.loads(raw, parse_constant=refuse)
        self.assertEqual(json.loads(raw)["outside"]["combined"]["daily_cap"], 0)

    def test_strict_sender_never_emits_infinity(self):
        from lowtides.server import _dumps_strict
        out = _dumps_strict({"a": float("inf"), "b": [float("nan"), 1.5]})
        self.assertEqual(json.loads(out), {"a": None, "b": [None, 1.5]})

    def test_demo_visitors_can_only_touch_the_simulated_provider(self):
        app, url = self.serve(demo=True)
        os.environ["ANTHROPIC_ADMIN_KEY"] = "sk-ant-admin-should-never-be-used"
        try:
            with mock.patch("lowtides.outside._Http._call", side_effect=AssertionError("network")):
                self.assertIsNone(app.outside.client("anthropic"))
                self.assertIsNone(app.outside.client("openai"))
                for name in ("anthropic", "openai"):
                    s, _, _ = http("POST", url + "/api/outside/settings", {"providers": {name: {"enabled": True}}})
                    self.assertEqual(s, 400)
                s, st, _ = http("POST", url + "/api/outside/settings", {"providers": {"simulated": {"daily_cap": 50}}})
                self.assertEqual(s, 200)
                self.assertEqual([p["name"] for p in st["providers"]], ["simulated"])
        finally:
            os.environ.pop("ANTHROPIC_ADMIN_KEY", None)
        self.assertNotIn("outside", app.config.data)

    def test_outside_api_needs_admin_password(self):
        os.environ["LOWTIDES_ADMIN_TOKEN"] = "pw"
        try:
            app, url = self.serve(demo=False)
        finally:
            os.environ["LOWTIDES_ADMIN_TOKEN"] = ""
        s, _, _ = http("POST", url + "/api/outside/disable", {})
        self.assertEqual(s, 401)
        s, _, _ = http("GET", url + "/api/outside")
        self.assertEqual(s, 401)
        s, st, _ = http("GET", url + "/api/outside", headers={"X-Admin-Token": "pw"})
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
