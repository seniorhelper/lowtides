"""Autopilot ask, receipts with plain-English search, and Talk to a human (lowtides/extras.py)."""
import os
import time
import unittest
from unittest import mock

from test_demo import Browser, demo_server

from lowtides import extras


class TestUnderstand(unittest.TestCase):
    def test_ask(self):
        self.assertEqual(extras.understand("find me a cordless drill under $90"), ("cordless drill", 90.0))
        self.assertEqual(extras.understand("Dog food"), ("dog food", None))
        self.assertEqual(extras.understand("tape measure, $20 or less"), ("tape measure", 20.0))
        self.assertEqual(extras.understand("please"), ("", None))

    def test_matches(self):
        self.assertTrue(extras.matches("cordless drills", "18V Cordless Drill tools Harbor Hardware"))
        self.assertTrue(extras.matches("drill", "Cordless drill"))
        self.assertFalse(extras.matches("drill ladder", "Cordless drill"))

    def test_search_words(self):
        now = time.mktime((2026, 11, 11, 12, 0, 0, 0, 0, -1))
        f, said = extras.parse_search("what did I buy at harbor this week over $20 refunded", now)
        self.assertEqual((f["min"], f["status"], f["words"]), (20.0, "refunded", ["harbor"]))
        self.assertIn("this week", said)
        self.assertLess(f["since"], now)
        f, _ = extras.parse_search("between $5 and $15 last 30 days", now)
        self.assertEqual((f["min"], f["max"]), (5.0, 15.0))
        self.assertEqual(extras.parse_search("show me all my receipts", now)[0], {})

    def test_pasted_receipt(self):
        got = extras.from_text("Corner Hardware\n11/09/2026\nPaint brush 2in  $8.99\nTax $0.70\nTotal: $9.69")
        self.assertEqual(got, {"store": "Corner Hardware", "amount": 9.69, "bought_on": "2026-11-09", "title": "Paint brush 2in"})


class TestExtrasHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()
        cls.app.limiter.burst = cls.app.limiter.rate = 10 ** 6

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def shop(self):
        b = Browser(self.url)
        b("GET", "/api/stores")
        return b

    def test_autopilot_pays_a_green_match(self):
        b = self.shop()
        code, r = b("POST", "/api/shopper/autopilot", {"ask": "a tape measure under $30"})[:2]
        self.assertEqual(code, 200, r)
        self.assertEqual(r["understood"], {"q": "tape measure", "max_price": 30.0})
        self.assertEqual(r["mode"], "autopilot")
        self.assertEqual(r["handshake"]["signal"], "match")
        self.assertTrue(r["handshake"]["order"])
        self.assertIn("paid", r["said"])
        self.assertLessEqual(r["picked"]["price"], 30)

    def test_assist_holds_and_off_buys_nothing(self):
        b = self.shop()
        self.assertEqual(b("PUT", "/api/shopper/prefs", {"mode": "assist"})[0], 200)
        r = b("POST", "/api/shopper/autopilot", {"ask": "tape measure"})[1]
        self.assertEqual(r["handshake"]["signal"], "needs_yes")
        self.assertTrue(r["handshake"]["approval_id"])
        self.assertIsNone(r["handshake"]["order"])
        b("PUT", "/api/shopper/prefs", {"mode": "off"})
        r = b("POST", "/api/shopper/autopilot", {"ask": "tape measure"})[1]
        self.assertIsNone(r["handshake"])
        self.assertIn("Off", r["said"])
        paid = [x for x in b("GET", "/api/receipts")[1]["receipts"] if x["status"] == "paid" and x["title"].lower().startswith("tape")]
        self.assertEqual(paid, [])

    def test_autopilot_never_buys_a_red_and_handles_nothing_found(self):
        b = self.shop()
        r = b("POST", "/api/shopper/autopilot", {"ask": "gift card"})[1]
        self.assertIsNone(r["handshake"])
        self.assertIn("Nothing was bought", r["said"])
        r = b("POST", "/api/shopper/autopilot", {"ask": "submarine"})[1]
        self.assertEqual(r["considered"], 0)
        self.assertEqual(b("POST", "/api/shopper/autopilot", {"ask": "please"})[0], 400)
        self.assertEqual(b("POST", "/api/shopper/autopilot", {})[0], 400)

    def test_search_box_understands_a_price(self):
        b = self.shop()
        r = b("GET", "/api/catalog?q=drill%20under%20%2490")[1]
        self.assertEqual(r["understood"]["max_price"], 90.0)
        self.assertTrue(r["items"])
        self.assertTrue(all(i["price"] <= 90 and "drill" in i["title"].lower() for i in r["items"]))

    def test_receipts_track_and_search(self):
        b = self.shop()
        b("POST", "/api/shopper/autopilot", {"ask": "tape measure"})
        code, a = b("POST", "/api/receipts", {"store": "Corner Hardware", "title": "Paint brush", "amount": 12.5, "note": "cash"})[:2]
        self.assertEqual(code, 200, a)
        code, p = b("POST", "/api/receipts", {"text": "Mesa Feed Co\n2026-11-02\nChicken feed 40lb $31.00\nTotal $33.48"})[:2]
        self.assertEqual((code, p["amount"], p["store"]), (200, 33.48, "Mesa Feed Co"))
        everything = b("GET", "/api/receipts")[1]
        self.assertGreaterEqual(everything["count"], 3)
        kinds = {x["kind"] for x in everything["receipts"]}
        self.assertEqual(kinds, {"lowtides", "added"})
        r = b("GET", "/api/receipts?q=what%20did%20i%20add%20over%20%2420")[1]
        self.assertEqual([x["store"] for x in r["receipts"]], ["Mesa Feed Co"])
        self.assertEqual(r["spent_usd"], 33.48)
        r = b("GET", "/api/receipts?q=paid%20tape%20today")[1]
        self.assertTrue(r["receipts"])
        self.assertTrue(all(x["status"] == "paid" and x["proof_url"] for x in r["receipts"]))
        self.assertEqual(b("GET", "/api/receipts?q=submarine")[1]["count"], 0)
        self.assertEqual(b("POST", "/api/receipts/delete", {"id": a["id"]})[0], 200)
        self.assertEqual(b("POST", "/api/receipts/delete", {"id": a["id"]})[0], 404)
        self.assertEqual(b("POST", "/api/receipts", {"store": "", "amount": 5})[0], 400)
        self.assertEqual(b("POST", "/api/receipts", {"store": "X", "amount": -1})[0], 400)
        self.assertEqual(b("POST", "/api/receipts", {"store": "X", "amount": 5, "bought_on": "soon"})[0], 400)

    def test_receipts_stay_with_their_visitor(self):
        a, b = self.shop(), self.shop()
        a("POST", "/api/receipts", {"store": "Only Mine", "amount": 4})
        self.assertNotIn("Only Mine", [x["store"] for x in b("GET", "/api/receipts")[1]["receipts"]])

    def test_talk_to_a_human(self):
        b = self.shop()
        chat = [{"who": "you", "text": "is this real money?"}, {"who": "guide", "text": "No, it is PayPal sandbox test money."}, {"who": "x", "text": "dropped"}]
        with mock.patch.dict(os.environ, {"LOWTIDES_HELP_TOKEN": "tok-123"}, clear=False):
            code, r = b("POST", "/api/help", {"email": "pat@example.com", "message": "My store wants in.", "page": "/shop/", "chat": chat})[:2]
            self.assertEqual(code, 200, r)
            self.assertTrue(r["staffed"])
            self.assertEqual(r["chat_messages"], 2)
            self.assertRegex(r["id"], r"^H-[0-9A-F]{6}$")
            rows = extras.help_list(self.app, "tok-123")["requests"]
            mine = [x for x in rows if x["id"] == r["id"]][0]
            self.assertEqual((mine["email"], len(mine["chat"])), ("pat@example.com", 2))
            with self.assertRaises(extras.HandshakeError):
                extras.help_list(self.app, "wrong")
        self.assertEqual(b("GET", "/api/help")[0], 404)
        self.assertEqual(b("POST", "/api/help", {"email": "nope", "message": "hello there"})[0], 400)
        self.assertEqual(b("POST", "/api/help", {"email": "pat@example.com", "message": ""})[0], 400)

    def test_help_says_so_when_nobody_is_on_shift_and_limits_floods(self):
        b = self.shop()
        with mock.patch.dict(os.environ, {"LOWTIDES_HELP_TOKEN": "", "LOWTIDES_HELP_WEBHOOK": ""}, clear=False):
            extras._help_hits.clear()
            r = b("POST", "/api/help", {"email": "pat@example.com", "message": "anyone there?"})[1]
            self.assertFalse(r["staffed"])
            self.assertIn("no person on shift", r["said"])
            codes = [b("POST", "/api/help", {"email": "pat@example.com", "message": "again %d" % i})[0] for i in range(6)]
            self.assertIn(429, codes)
            extras._help_hits.clear()



class HelpBotGuards(unittest.TestCase):
    def test_honeypot_and_too_fast_are_refused(self):
        from lowtides import extras
        from lowtides.handshake import HandshakeError
        ok = {"email": "a@example.com", "message": "please help"}
        for bad in ({"_honey": "x"}, {"filled_ms": 400}):
            with self.assertRaises(HandshakeError):
                extras.help_request(None, dict(ok, **bad))

if __name__ == "__main__":
    unittest.main()
