"""Report this store: a signed fraud report for a store or business the watchdog refused. Only a refusal for a
fake, lookalike or unsigned store can be reported, the same decision is reported once, the signed document
verifies with the published key, nothing is sent anywhere, and Reset demo clears it."""
import json
import unittest
import urllib.error

from test_demo import Browser, demo_server

from lowtides import handshake, reports


def call(b, method, path, body=None, headers=None):
    try:
        return b(method, path, body, headers)[:2]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def shake(b, store, item):
    s, r = call(b, "POST", "/api/handshake", {"store_id": store, "item_id": item})
    assert s == 200, r
    return r


class Reports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_report_a_refused_fake_store(self):
        b = Browser(self.url)
        h = shake(b, "fakeharbor", "fake-hammer")
        self.assertEqual(h["signal"], "no_match")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"], "note": "  Found it in  an ad. "})
        self.assertEqual(s, 200, r)
        self.assertRegex(r["id"], r"^rep_[0-9a-f]{16}$")
        self.assertIsInstance(r["ts"], float)
        self.assertEqual(r["store"], {"id": "fakeharbor", "name": "Harbor-Hardwarre Outlet", "domain": "harbor-hardwarre-deals.example"})
        self.assertEqual(r["decision"], {"kind": "handshake", "id": h["id"]})
        self.assertEqual(r["reasons"], h["guardpooch"]["reasons"])
        self.assertTrue(r["reasons"] and all(isinstance(x, str) and x for x in r["reasons"]))
        self.assertEqual(r["note"], "Found it in an ad.")
        self.assertEqual(r["evidence_url"], "/api/handshakes/%s/proof" % h["id"])
        self.assertEqual(r["report_url"], "/api/reports/" + r["id"])
        self.assertEqual(r["official"], [
            {"name": "FTC", "label": "Report fraud to the FTC", "url": "https://reportfraud.ftc.gov/"},
            {"name": "IC3", "label": "Report internet crime to the FBI's IC3", "url": "https://www.ic3.gov/"}])
        self.assertEqual(r["demo_note"], "Demo stores are fictional. Do not file a real report about them. "
                                         "Lowtides does not send anything to the authorities for you.")
        s, ev = call(b, "GET", r["evidence_url"])
        self.assertEqual(s, 200, ev)
        # the store is still refused afterwards, and the list lets the page show "Reported by you"
        self.assertEqual(shake(b, "fakeharbor", "fake-hammer")["signal"], "no_match")
        s, lst = call(b, "GET", "/api/reports")
        self.assertEqual([x["id"] for x in lst["reports"]], [r["id"]])
        self.assertEqual(lst["reports"][0], r)

    def test_signed_document_verifies_and_tampering_is_caught(self):
        b = Browser(self.url)
        h = shake(b, "fakeharbor", "fake-drill")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"]})
        s, doc = call(b, "GET", r["report_url"])
        self.assertEqual(s, 200, doc)
        self.assertEqual((doc["type"], doc["id"], doc["decision"], doc["sent_to_authorities"]),
                         ("lowtides.fraud_report", r["id"], r["decision"], False))
        self.assertEqual((doc["store"], doc["reasons"], doc["demo_note"]), (r["store"], r["reasons"], r["demo_note"]))
        s, keys = call(b, "GET", "/.well-known/lowtides-pass-keys.json")
        self.assertTrue(handshake.verify_doc(doc, keys), "the report verifies against the published key")
        self.assertFalse(handshake.verify_doc(dict(doc, reasons=["Looks fine."]), keys), "an edited report no longer verifies")
        s, r = call(b, "GET", "/api/reports/rep_0000000000000000")
        self.assertEqual(s, 404)

    def test_same_decision_twice_is_one_report(self):
        b = Browser(self.url)
        h = shake(b, "fakeharbor", "fake-socket")
        s, one = call(b, "POST", "/api/reports", {"handshake_id": h["id"]})
        s, two = call(b, "POST", "/api/reports", {"handshake_id": h["id"], "note": "again"})
        self.assertEqual(s, 200, two)
        self.assertEqual(two, one)
        s, lst = call(b, "GET", "/api/reports")
        self.assertEqual(len(lst["reports"]), 1)

    def test_only_a_watchdog_refusal_can_be_reported(self):
        b = Browser(self.url)
        call(b, "PUT", "/api/shopper/prefs", {"mode": "autopilot"})
        h = shake(b, "harbor", "harbor-hammer")
        self.assertEqual(h["signal"], "match")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"]})
        self.assertEqual((s, r["error"]), (409, "Only a store that GuardPooch refused can be reported."))
        # a refusal for some other reason (a restaurant does not take travel bookings) is not a fake store either
        s, a = call(b, "POST", "/api/muzzle/action", {"action_type": "book_travel", "counterparty": "harbor-bistro"})
        self.assertEqual(a["signal"], "no_match")
        s, r = call(b, "POST", "/api/reports", {"action_id": a["id"]})
        self.assertEqual(s, 409, r)
        s, lst = call(b, "GET", "/api/reports")
        self.assertEqual(lst["reports"], [])

    def test_bad_input(self):
        b = Browser(self.url)
        for body in ({"handshake_id": 7}, {"handshake_id": ["hs_1"]}, {"action_id": {"a": 1}}, {"action_id": True}, {}, {"note": "hi"},
                     {"handshake_id": "hs_00", "action_id": "act_00"}, [1, 2]):
            s, r = call(b, "POST", "/api/reports", body)
            self.assertEqual(s, 400, (body, r))
            self.assertTrue(r["error"])
        for body in ({"handshake_id": "hs_0000000000000000"}, {"action_id": "act_0000000000000000"}, {"handshake_id": "nope"},
                     {"handshake_id": "' OR 1=1 --"}):
            s, r = call(b, "POST", "/api/reports", body)
            self.assertEqual(s, 404, (body, r))
        h = shake(b, "fakeharbor", "fake-hammer")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"], "note": 5})
        self.assertEqual(s, 400, r)
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"], "note": "x" * 281})
        self.assertEqual(s, 400, r)
        self.assertIn("280", r["error"])
        s, lst = call(b, "GET", "/api/reports")
        self.assertEqual(lst["reports"], [], "a rejected note saves nothing")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"], "note": "x" * 280})
        self.assertEqual((s, len(r["note"])), (200, 280))

    def test_action_muzzle_lookalike_can_be_reported(self):
        b = Browser(self.url)
        s, a = call(b, "POST", "/api/muzzle/action", {"action_type": "call", "counterparty": "coastline-moblie-help"})
        self.assertEqual((s, a["signal"]), (200, "no_match"))
        s, r = call(b, "POST", "/api/reports", {"action_id": a["id"]})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["decision"], {"kind": "action", "id": a["id"]})
        self.assertEqual(r["store"], {"id": "coastline-moblie-help", "name": "Coastline Moblie Support", "domain": "coastline-moblie-help.example"})
        self.assertTrue(any("Coastline Mobile" in x for x in r["reasons"]))
        self.assertEqual(r["evidence_url"], "/api/muzzle/receipt/" + a["id"])
        s, rec = call(b, "GET", r["evidence_url"])
        s, keys = call(b, "GET", "/.well-known/lowtides-pass-keys.json")
        self.assertTrue(handshake.verify_doc(rec, keys))
        s, doc = call(b, "GET", r["report_url"])
        self.assertTrue(handshake.verify_doc(doc, keys))

    def test_one_visitor_never_sees_anothers_reports(self):
        b, other = Browser(self.url), Browser(self.url)
        h = shake(b, "fakeharbor", "fake-hammer")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"]})
        s, lst = call(other, "GET", "/api/reports")
        self.assertEqual(lst["reports"], [])
        s, x = call(other, "GET", r["report_url"])
        self.assertEqual(s, 404)
        s, x = call(other, "POST", "/api/reports", {"handshake_id": h["id"]})
        self.assertEqual(s, 404)

    def test_cap_per_sandbox(self):
        b = Browser(self.url)
        old, reports.MAX_REPORTS = reports.MAX_REPORTS, 2
        try:
            ids = [shake(b, "fakeharbor", "fake-hammer")["id"] for _ in range(3)]
            self.assertEqual([call(b, "POST", "/api/reports", {"handshake_id": i})[0] for i in ids], [200, 200, 409])
            self.assertEqual(call(b, "POST", "/api/reports", {"handshake_id": ids[0]})[0], 200, "an existing report still comes back")
        finally:
            reports.MAX_REPORTS = old

    def test_reset_clears_reports(self):
        b = Browser(self.url)
        h = shake(b, "fakeharbor", "fake-hammer")
        s, r = call(b, "POST", "/api/reports", {"handshake_id": h["id"]})
        self.assertEqual(s, 200, r)
        s, x = call(b, "POST", "/api/demo/reset", {})
        self.assertEqual(s, 200, x)
        s, lst = call(b, "GET", "/api/reports")
        self.assertEqual(lst["reports"], [])
        s, x = call(b, "GET", r["report_url"])
        self.assertEqual(s, 404)

    def test_no_network_code_in_the_module(self):
        with open(reports.__file__, encoding="utf-8") as f:
            src = f.read()
        for word in ("urllib", "http.client", "socket", "requests", "smtplib"):
            self.assertNotIn("import " + word, src)


if __name__ == "__main__":
    unittest.main()
