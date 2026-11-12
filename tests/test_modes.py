"""Shopper modes (autopilot / assist / off), store modes (auto / review / closed) and the extra
store KPIs (returning shoppers, kept-order rate, net captured, bad orders stopped)."""
import json
import unittest

from helpers import agent
from test_handshake import check, fresh_app, run

from lowtides import handshake, trust
from lowtides.handshake import verify_doc

ISS = "https://lowtides.test"


class TestModesInTheEngine(unittest.TestCase):
    def test_shopper_modes(self):
        self.assertEqual(run(prefs={"mode": "autopilot"})["signal"], "match")
        r = run(prefs={"mode": "assist"})
        self.assertEqual(r["signal"], "needs_yes")
        self.assertEqual(check(r, "shopper_mode")["level"], "ask")
        self.assertIn("Assist", r["headline"])
        r = run(prefs={"mode": "off"})
        self.assertEqual(r["signal"], "no_match")
        self.assertEqual(check(r, "shopper_mode")["level"], "fail")
        self.assertIn("switched Off", r["headline"])

    def test_store_modes(self):
        self.assertEqual(run(policy={"mode": "auto"})["signal"], "match")
        r = run(policy={"mode": "review"})
        self.assertEqual(r["signal"], "needs_yes")
        self.assertEqual(check(r, "store_mode")["level"], "ask")
        self.assertIn(r["store_checks"][0]["id"], ("accepts_pass", "store_mode"))
        r = run(policy={"mode": "closed"})
        self.assertEqual(r["signal"], "no_match")
        self.assertIn("closed to AI shoppers", r["headline"])

    def test_defaults_add_no_checks(self):
        r = run()
        self.assertEqual(len(r["shopper_checks"]), 8)
        self.assertEqual(len(r["store_checks"]), 6)


class TestModesInTheService(unittest.TestCase):
    def setUp(self):
        self.app = fresh_app()
        self.svc = handshake.service(self.app)
        self.a = agent(self.app.store, "Shopper", daily=5.0)
        self.app.store.update_agent(self.a["id"], rules=json.dumps({"max_per_purchase": 200, "daily_purchase_limit": 400, "approval_over": 100}))
        self.app.store.add_merchant("Acme", "pay@example.com", "data")
        self.a = self.app.store.agent(self.a["id"])
        for i in range(4):
            self.app.spend.request(self.a, {"amount": 5 + i, "merchant": "Acme", "item": "earlier %d" % i, "category": "data"})

    def hs(self, store="harbor", item="harbor-hammer", **kw):
        return self.svc.handshake(self.app.store.agent(self.a["id"]), store, item=item, issuer=ISS, **kw)

    def test_shopper_mode_is_saved_and_enforced(self):
        self.assertEqual(self.svc.prefs(self.a)["mode"], "autopilot")
        out = self.svc.set_prefs(self.a, {"mode": "assist"}, ISS)
        self.assertEqual(out["mode"], "assist")
        r = self.hs(execute=True)                      # a green item now waits for a person
        self.assertEqual(r["signal"], "needs_yes", r["headline"])
        self.assertIsNone(r["order"])
        self.assertEqual(self.app.store.spend(r["approval_id"])["status"], "pending")
        self.svc.set_prefs(self.a, {"mode": "off"}, ISS)
        before = trust.compute(self.app.store, self.a["id"])["score"]
        r = self.hs(execute=True)
        self.assertEqual(r["signal"], "no_match")
        self.assertIsNone(r["approval_id"])
        self.assertEqual(trust.compute(self.app.store, self.a["id"])["score"], before, "Off is the owner's choice: no trust penalty")
        self.assertEqual(self.svc.prefs(self.a)["mode"], "off")
        self.svc.set_prefs(self.a, {"mode": "autopilot"}, ISS)         # reversible, unlike KILL
        self.assertEqual(self.hs()["signal"], "match")
        with self.assertRaises(handshake.HandshakeError):
            self.svc.set_prefs(self.a, {"mode": "turbo"}, ISS)

    def test_store_mode_is_signed_and_enforced(self):
        keys = self.svc.book.jwks()
        self.assertEqual(self.svc.store_view("harbor")["policy"]["mode"], "auto")
        self.assertEqual(self.svc.policy_doc("harbor")["policy"]["mode"], "auto")
        self.svc.set_policy("harbor", {"mode": "review"})
        doc = self.svc.policy_doc("harbor")
        self.assertEqual(doc["policy"]["mode"], "review")
        self.assertTrue(verify_doc(doc, keys))
        r = self.hs(execute=True)
        self.assertEqual(r["signal"], "needs_yes")
        self.assertEqual(check(r, "store_mode")["level"], "ask")
        self.assertEqual(self.app.store.spend(r["approval_id"])["status"], "pending")
        self.svc.set_policy("harbor", {"mode": "closed"})
        r = self.hs(execute=True)
        self.assertEqual(r["signal"], "no_match")
        self.assertEqual(check(r, "store_mode")["level"], "fail")
        self.svc.set_policy("harbor", {"mode": "auto"})
        self.assertEqual(self.hs()["signal"], "match")
        with self.assertRaises(handshake.HandshakeError):
            self.svc.set_policy("harbor", {"mode": "sometimes"})

    def test_extra_kpis_come_from_real_rows(self):
        k = self.svc.kpis("harbor")
        for f in ("returning_shoppers", "bad_orders_stopped", "net_captured_usd", "bad_orders_stopped_usd", "refunded_usd"):
            self.assertEqual(k[f], 0, f)
        self.assertEqual(k["kept_order_rate"], 0.0)
        self.hs(execute=True)                                  # $18.50 kept
        self.assertEqual(self.svc.kpis("harbor")["returning_shoppers"], 0)
        self.hs(item="harbor-anchors", execute=True)           # $14.25 kept: now a returning shopper
        self.hs(item="harbor-tablesaw", execute=True)          # $449 refused: a bad order stopped
        self.hs(item="harbor-tablesaw", execute=False)         # a look-only check is not an order
        k = self.svc.kpis("harbor")
        self.assertEqual((k["returning_shoppers"], k["shoppers_with_orders"], k["retention_rate"]), (1, 1, 1.0))
        self.assertEqual((k["kept_order_rate"], k["net_captured_usd"], k["gross_usd"]), (1.0, 32.75, 32.75))
        self.assertEqual((k["bad_orders_stopped"], k["bad_orders_stopped_usd"]), (1, 449.0))
        sid = [h for h in self.svc.list(10) if h["item"]["title"].startswith("Claw")][0]["spend_id"]
        self.app.store.update_spend(sid, status="cancelled")   # pulled back = refunded
        k = self.svc.kpis("harbor")
        self.assertEqual((k["kept_order_rate"], k["net_captured_usd"], k["refunded_usd"], k["returning_shoppers"]), (0.5, 14.25, 18.5, 0))


if __name__ == "__main__":
    unittest.main()
