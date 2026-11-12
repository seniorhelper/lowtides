"""Action Muzzle: the both-sides check for things an AI does besides buying. The job scope, levels, each
business's signed policy, the watchdog, the three outcomes, approvals, signed receipts, the stop word and
freeze, receipts search, the agent API, and one visitor never seeing another's actions."""
import json
import unittest
import urllib.error

from test_demo import Browser, demo_server, http

from lowtides import handshake, muzzle


def call(b, method, path, body=None, headers=None):
    try:
        return b(method, path, body, headers)[:2]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def act(b, typ, cp, **extra):
    s, r = call(b, "POST", "/api/muzzle/action", dict({"action_type": typ, "counterparty": cp}, **extra))
    assert s == 200, r
    return r


def mode(b, m):
    s, st = call(b, "GET", "/api/muzzle")
    s, r = call(b, "PUT", "/api/shopper/prefs", {"agent_id": st["agent"]["id"], "mode": m})
    assert s == 200, r


class Pure(unittest.TestCase):
    def test_job_becomes_a_scope(self):
        self.assertEqual(muzzle.scope_of("book a table for two Friday at 7 at Harbor Bistro"),
                         {"action_types": ["reserve"], "counterparty": "harbor-bistro", "summary": "Reserve at Harbor Bistro"})
        self.assertEqual(muzzle.scope_of("schedule a check-up at the clinic")["counterparty"], "tidewater-clinic")
        self.assertEqual(muzzle.scope_of("call the phone company about my bill")["action_types"], ["call"])
        with self.assertRaises(handshake.HandshakeError):
            muzzle.scope_of("hmm")

    def test_watchdog_refuses_the_lookalike_only(self):
        verdicts = {c: muzzle.legit(c)["verdict"] for c in muzzle.COUNTERPARTIES}
        self.assertEqual(verdicts.pop("coastline-moblie-help"), "danger")
        self.assertEqual(set(verdicts.values()), {"safe"})
        self.assertIn("Coastline Mobile", muzzle.legit("coastline-moblie-help")["reason"])


class Muzzle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_state_lists_types_businesses_and_signed_policies(self):
        b = Browser(self.url)
        s, st = call(b, "GET", "/api/muzzle")
        self.assertEqual(s, 200, st)
        self.assertEqual([t["type"] for t in st["types"]], list(muzzle.TYPES))
        self.assertEqual((st["frozen"], st["stop_word"], st["task"]), (False, "red light", None))
        cps = {c["id"]: c for c in st["counterparties"]}
        self.assertIsNone(cps["coastline-moblie-help"]["policy"])
        self.assertEqual(len(cps["harbor-bistro"]["policy"]["digest"]), 16)
        self.assertGreaterEqual(len(st["samples"]), 5)

    def test_do_only_what_was_asked(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        # no job yet: even an allowed kind of action has to ask
        a = act(b, "reserve", "harbor-bistro", summary="Table for two")
        self.assertEqual((a["signal"], a["status"]), ("needs_yes", "waiting"))
        s, r = call(b, "POST", "/api/muzzle/task", {"text": "book a table for two Friday at 7 at Harbor Bistro"})
        self.assertEqual(r["task"]["scope"]["counterparty"], "harbor-bistro")
        # inside the job, allowed, the business accepts it: green, with a confirmation and nothing real contacted
        a = act(b, "reserve", "harbor-bistro", summary="Table for two, Friday 7:00 pm", params={"party": 2})
        self.assertEqual((a["signal"], a["status"]), ("match", "done"))
        self.assertRegex(a["result"]["confirmation"], r"^HB-\d{4}$")
        self.assertTrue(a["simulated"])
        self.assertEqual({c["side"] for c in a["checks"]}, {"you", "them", "guard"})
        # outside the job: asks, even though the business would accept it
        a = act(b, "sign_up", "harbor-bistro")
        self.assertEqual(a["signal"], "needs_yes")
        self.assertTrue(any("Outside the job" in c["detail"] for c in a["checks"]))
        # allowed kind, but a different business than the job named: asks
        a = act(b, "schedule", "tidewater-clinic")
        self.assertEqual(a["signal"], "needs_yes")

    def test_red_for_never_private_details_lookalikes_and_unaccepted_requests(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        a = act(b, "share_data", "harbor-bistro", params={"data": ["ssn"]})
        self.assertEqual((a["signal"], a["status"]), ("no_match", "blocked"))
        self.assertTrue(any("Social Security" in c["detail"] for c in a["checks"]))
        a = act(b, "call", "coastline-moblie-help")
        self.assertEqual(a["signal"], "no_match")
        self.assertTrue(any(c["side"] == "guard" and c["level"] == "fail" for c in a["checks"]))
        a = act(b, "book_travel", "harbor-bistro")             # a restaurant does not take travel bookings
        self.assertEqual(a["signal"], "no_match")
        call(b, "POST", "/api/muzzle/rules", {"levels": {"reserve": "never"}})
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "no_match")

    def test_modes_apply_to_actions(self):
        b = Browser(self.url)
        call(b, "POST", "/api/muzzle/task", {"text": "book a table at Harbor Bistro"})
        mode(b, "off")
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "no_match")
        mode(b, "assist")
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "needs_yes")
        mode(b, "autopilot")
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "match")

    def test_yes_and_no_on_a_waiting_action_and_the_signed_receipt(self):
        b = Browser(self.url)
        a = act(b, "sign_up", "harbor-bistro")
        self.assertEqual(a["status"], "waiting")
        s, done = call(b, "POST", "/api/muzzle/decide", {"id": a["id"], "approve": True})
        self.assertEqual((s, done["status"], done["signal"]), (200, "done", "match"))
        self.assertIn("confirmation", done["result"])
        s, again = call(b, "POST", "/api/muzzle/decide", {"id": a["id"], "approve": True})
        self.assertEqual(s, 409)
        a2 = act(b, "message", "harbor-bistro")
        s, no = call(b, "POST", "/api/muzzle/decide", {"id": a2["id"], "approve": False})
        self.assertEqual(no["status"], "denied")
        s, rec = call(b, "GET", done["receipt_url"])
        self.assertEqual((rec["type"], rec["status"], rec["simulated"]), ("lowtides.action_receipt", "done", True))
        s, keys = call(b, "GET", "/.well-known/lowtides-pass-keys.json")
        self.assertTrue(handshake.verify_doc(rec, keys), "the action receipt verifies against the published key")
        rec["status"] = "blocked"
        self.assertFalse(handshake.verify_doc(rec, keys), "an edited receipt no longer verifies")
        s, bad = call(b, "POST", "/api/muzzle/decide", {"id": a2["id"]})
        self.assertEqual(s, 400)

    def test_stop_word_freezes_everything_and_release_brings_it_back(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        call(b, "POST", "/api/muzzle/task", {"text": "book a table at Harbor Bistro"})
        waiting = act(b, "sign_up", "harbor-bistro")
        s, r = call(b, "POST", "/api/ask", {"question": "Red light!", "page": "/shop/"})      # typed (or spoken) to the guide
        self.assertEqual(s, 200, r)
        self.assertTrue(r.get("frozen"))
        self.assertIn("Frozen", r["answer"])
        s, st = call(b, "GET", "/api/muzzle")
        self.assertTrue(st["frozen"])
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "no_match")            # actions are refused
        s, hs = call(b, "POST", "/api/handshake", {"store_id": "harbor", "item_id": "harbor-hammer", "execute": True})
        self.assertEqual(hs["signal"], "no_match", "a frozen shopper cannot buy either")
        s, r = call(b, "POST", "/api/muzzle/decide", {"id": waiting["id"], "approve": True})
        self.assertEqual(s, 409)
        s, r = call(b, "POST", "/api/muzzle/release", {})
        self.assertFalse(r["frozen"])
        self.assertEqual(act(b, "reserve", "harbor-bistro")["signal"], "match")
        s, hs = call(b, "POST", "/api/handshake", {"store_id": "harbor", "item_id": "harbor-hammer", "execute": False})
        self.assertNotEqual(hs["signal"], "no_match", "after release the shopper can buy again")
        s, r = call(b, "POST", "/api/muzzle/decide", {"id": waiting["id"], "approve": True})
        self.assertEqual((s, r["status"]), (200, "done"))                                       # the held job picks up

    def test_stop_word_rules(self):
        b = Browser(self.url)
        for bad in ("no", "x", "stop", "one two three four five six"):
            s, r = call(b, "POST", "/api/muzzle/rules", {"stop_word": bad})
            self.assertEqual(s, 400, bad)
        s, st = call(b, "POST", "/api/muzzle/rules", {"stop_word": "Blue Heron"})
        self.assertEqual(st["stop_word"], "blue heron")
        s, r = call(b, "POST", "/api/ask", {"question": "what is a pass"})
        self.assertFalse(r.get("frozen"))
        s, r = call(b, "POST", "/api/ask", {"question": "blue heron"})
        self.assertTrue(r.get("frozen"))
        s, r = call(b, "POST", "/api/muzzle/rules", {"levels": {"reserve": "sometimes"}})
        self.assertEqual(s, 400)

    def test_actions_show_in_receipts_and_search(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        call(b, "POST", "/api/muzzle/task", {"text": "book a table at Harbor Bistro"})
        act(b, "reserve", "harbor-bistro", summary="Table for two, Friday 7:00 pm")
        act(b, "share_data", "harbor-bistro", params={"data": ["ssn"]})
        s, r = call(b, "GET", "/api/receipts?q=bistro")
        kinds = [(x["kind"], x["status"]) for x in r["receipts"]]
        self.assertIn(("action", "done"), kinds)
        self.assertIn(("action", "blocked"), kinds)
        s, r = call(b, "GET", "/api/receipts?q=done")
        self.assertTrue(r["receipts"] and all(x["status"] == "done" for x in r["receipts"]))
        self.assertEqual(r["spent_usd"], 0)

    def test_a_deposit_is_paid_through_paypal_only_after_both_say_yes(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        call(b, "POST", "/api/muzzle/task", {"text": "book a table for two Friday at 7 at Harbor Bistro"})
        a = act(b, "reserve", "harbor-bistro", summary="Table for two, $20 deposit", params={"party": 2, "amount": 20, "amount_for": "deposit"})
        self.assertEqual((a["signal"], a["status"]), ("match", "done"), a["headline"])
        pay = a["result"]["payment"]
        self.assertEqual((pay["kind"], pay["amount"]), ("payout", 20.0))
        self.assertIn("deposit was paid through", a["headline"])
        self.assertTrue(any(c["name"] == "Your money limits" and c["level"] == "pass" for c in a["checks"]))
        s, r = call(b, "GET", "/api/receipts?q=bistro")
        row = [x for x in r["receipts"] if x["id"] == a["id"]][0]
        self.assertEqual((row["amount"], bool(row["payment"])), (20.0, True))
        # a fare over the ask-me line waits; nothing is paid until the person says yes
        s, st = call(b, "GET", "/api/muzzle")
        call(b, "PUT", "/api/shopper/prefs", {"agent_id": st["agent"]["id"], "ask_above": 100, "per_item_cap": 200, "daily_cap": 400})
        call(b, "POST", "/api/muzzle/rules", {"levels": {"book_travel": "allow"}})
        call(b, "POST", "/api/muzzle/task", {"text": "book a flight with Skyline Air"})
        f = act(b, "book_travel", "skyline-air", summary="One seat to Denver, $189", params={"amount": 189, "amount_for": "fare"})
        self.assertEqual((f["signal"], f["status"]), ("needs_yes", "waiting"), f["headline"])
        self.assertNotIn("payment", f["result"])
        s, done = call(b, "POST", "/api/muzzle/decide", {"id": f["id"], "approve": True})
        self.assertEqual((s, done["status"]), (200, "done"), done)
        self.assertEqual(done["result"]["payment"]["amount"], 189.0)
        # over the per-item limit: red, and no money moves
        big = act(b, "book_travel", "skyline-air", params={"amount": 950})
        self.assertEqual(big["signal"], "no_match")
        self.assertNotIn("payment", big["result"])
        s, bad = call(b, "POST", "/api/muzzle/action", {"action_type": "reserve", "counterparty": "harbor-bistro", "params": {"amount": -5}})
        self.assertEqual(s, 400)

    def test_a_freeze_survives_a_rules_change_and_release_keeps_a_deliberate_revoke(self):
        b = Browser(self.url)
        mode(b, "autopilot")
        s, st = call(b, "GET", "/api/muzzle")
        aid = st["agent"]["id"]
        call(b, "POST", "/api/muzzle/freeze", {})
        s, r = call(b, "PUT", "/api/shopper/prefs", {"agent_id": aid, "daily_cap": 350})        # moving a slider must not thaw it
        self.assertEqual(s, 200, r)
        s, hs = call(b, "POST", "/api/handshake", {"store_id": "harbor", "item_id": "harbor-hammer", "execute": False})
        self.assertEqual(hs["signal"], "no_match", "still frozen after a rules change")
        s, r = call(b, "POST", "/api/muzzle/release", {})
        s, hs = call(b, "POST", "/api/handshake", {"store_id": "harbor", "item_id": "harbor-hammer", "execute": False})
        self.assertNotEqual(hs["signal"], "no_match")

    def test_bad_input_is_a_400_and_a_yes_is_counted_once(self):
        b = Browser(self.url)
        for bad in ({"action_type": [], "counterparty": "harbor-bistro"}, {"action_type": "reserve", "counterparty": {"a": 1}}):
            s, r = call(b, "POST", "/api/muzzle/action", bad)
            self.assertEqual(s, 400, bad)
        a = act(b, "sign_up", "harbor-bistro", params={"amount": 5, "amount_for": "fee"})
        self.assertEqual(a["status"], "waiting")
        import threading
        codes = []
        def go():
            codes.append(call(b, "POST", "/api/muzzle/decide", {"id": a["id"], "approve": True})[0])
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(codes), [200, 409, 409, 409], "four yeses at once: one goes through")
        s, r = call(b, "POST", "/api/muzzle/decide", {"id": 7, "approve": True})
        self.assertEqual(s, 400)

    def test_agent_api_and_isolation(self):
        b = Browser(self.url)
        s, conn = call(b, "GET", "/api/shopper/connect")
        key = conn.get("key") or conn.get("agent_key") or (conn.get("agent") or {}).get("key")
        self.assertTrue(key, conn)
        try:
            s, r, _ = http("POST", self.url + "/v1/action", {"action_type": "reserve", "counterparty": "harbor-bistro"}, {"Authorization": "Bearer " + key})
        except urllib.error.HTTPError as e:
            s, r = e.code, json.loads(e.read() or b"{}")
        self.assertEqual(s, 200, r)
        self.assertIn(r["signal"], ("match", "needs_yes"))
        self.assertIn("next", r)
        try:
            s, _, _ = http("POST", self.url + "/v1/action", {"action_type": "reserve", "counterparty": "harbor-bistro"}, {"Authorization": "Bearer lt-nope"})
        except urllib.error.HTTPError as e:
            s = e.code
        self.assertEqual(s, 401)
        other = Browser(self.url)
        s, st = call(other, "GET", "/api/muzzle")
        self.assertEqual(st["actions"], [], "another visitor sees none of these actions")
        s, r = call(b, "POST", "/api/muzzle/action", {"action_type": "fly_to_moon", "counterparty": "harbor-bistro"})
        self.assertEqual(s, 400)

    def test_reset_clears_actions_and_the_freeze(self):
        b = Browser(self.url)
        act(b, "sign_up", "harbor-bistro")
        call(b, "POST", "/api/muzzle/freeze", {})
        s, r = call(b, "POST", "/api/demo/reset", {})
        self.assertEqual(s, 200, r)
        s, st = call(b, "GET", "/api/muzzle")
        self.assertEqual((st["frozen"], st["actions"], st["task"]), (False, [], None))


if __name__ == "__main__":
    unittest.main()
