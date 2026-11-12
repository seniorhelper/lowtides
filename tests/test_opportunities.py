"""Store opportunities (rule-based what-ifs), one-tap ratings, the shopper's agents, and Off in /v1/pass/verify."""
import json
import unittest

from helpers import agent
from test_handshake import fresh_app

from lowtides import handshake, trust

ISS = "https://lowtides.test"


class Base(unittest.TestCase):
    def setUp(self):
        self.app = fresh_app()
        self.svc = handshake.service(self.app)
        self.a = agent(self.app.store, "Shopper", daily=5.0)
        self.app.store.update_agent(self.a["id"], rules=json.dumps({"max_per_purchase": 200, "daily_purchase_limit": 400, "approval_over": 100}))
        self.app.store.add_merchant("Acme", "pay@example.com", "data")
        for i in range(4):      # 4 kept purchases: trust 64
            self.app.spend.request(self.app.store.agent(self.a["id"]), {"amount": 5 + i, "merchant": "Acme", "item": "earlier %d" % i, "category": "data"})

    def hs(self, store="harbor", item="harbor-hammer", **kw):
        return self.svc.handshake(self.app.store.agent(self.a["id"]), store, item=item, issuer=ISS, **kw)

    def find(self, store, fid):
        for f in self.svc.opportunities(store)["findings"]:
            if f["id"] == fid:
                return f
        return None


class TestOpportunities(Base):
    def test_no_data_and_scam_store(self):
        o = self.svc.opportunities("harbor")
        self.assertEqual(o["findings"], [])
        self.assertIn("Not enough", o["note"])
        self.assertEqual(o["label"], "Rule-based suggestions from your own orders")
        self.assertIn("no signed Store Policy", self.svc.opportunities("fakeharbor")["note"])
        with self.assertRaises(handshake.HandshakeError):
            self.svc.opportunities("nope")

    def test_lower_min_trust_what_if_and_apply(self):
        r = self.hs("brightside", "bright-lamp", execute=True)                 # trust 64 < 70
        self.assertEqual(r["signal"], "no_match")
        f = self.find("brightside", "min_trust_down")
        self.assertEqual(f["action"]["policy_patch"], {"min_trust": 60})
        self.assertEqual(f["impact_usd"], 36.0)
        self.assertIn("1 order from 1 AI shopper", f["detail"])
        self.svc.set_policy("brightside", f["action"]["policy_patch"])
        self.assertEqual(self.hs("brightside", "bright-lamp")["signal"], "match")
        self.assertIsNone(self.find("brightside", "min_trust_down"))            # already accepted now: nothing left to win

    def test_raise_no_person_limit(self):
        self.svc.set_policy("harbor", {"max_order_no_human": 10})
        self.assertEqual(self.hs(execute=True)["signal"], "needs_yes")           # $18.50 over the store's $10
        f = self.find("harbor", "max_order_up")
        self.assertEqual(f["action"]["policy_patch"], {"max_order_no_human": 60.0})
        self.assertEqual(f["impact_usd"], 18.5)

    def test_open_a_category(self):
        self.svc.set_policy("harbor", {"categories": ["tools"]})
        self.assertEqual(self.hs(item="harbor-anchors", execute=True)["signal"], "no_match")
        f = self.find("harbor", "open_hardware")
        self.assertEqual(f["action"]["policy_patch"], {"categories": ["hardware", "tools"]})

    def test_review_to_auto(self):
        self.svc.set_policy("harbor", {"mode": "review"})
        self.hs(execute=True)
        self.assertEqual(self.find("harbor", "mode_auto")["action"]["policy_patch"], {"mode": "auto"})

    def test_repeats_are_counted_once(self):
        self.svc.set_policy("harbor", {"mode": "review"})
        for _ in range(3):
            self.hs()
        self.assertIn("1 order from 1 AI shopper", self.find("harbor", "mode_auto")["detail"])

    def test_refunds_from_low_trust_shoppers_suggest_raising_the_minimum(self):
        a = self.hs(execute=True)
        b = self.hs(item="harbor-anchors", execute=True)
        self.assertIsNone(self.find("harbor", "min_trust_up"))                  # nothing pulled back yet
        for r in (a, b):
            self.app.store.update_spend(r["spend_id"], status="cancelled")
        f = self.find("harbor", "min_trust_up")
        self.assertEqual(f["kind"], "protect")
        self.assertEqual(f["action"]["policy_patch"], {"min_trust": 80})         # both were at trust 64 or 70
        self.assertEqual(f["impact_usd"], 32.75)

    def test_a_revoked_shopper_that_keeps_trying(self):
        self.app.stop_all()
        for item in ("harbor-hammer", "harbor-anchors", "harbor-socket"):
            self.assertEqual(self.hs(item=item, execute=True)["signal"], "no_match")
        f = self.find("harbor", "block_%d" % self.a["id"])
        self.assertEqual(f["action"]["policy_patch"], {"blocked_agents": ["shopper"]})
        self.svc.set_policy("harbor", f["action"]["policy_patch"])
        self.assertIsNone(self.find("harbor", "block_%d" % self.a["id"]))
        self.assertIsNotNone(self.find("harbor", "keeps_trying_%d" % self.a["id"]))


class TestRating(Base):
    def test_rating_a_paid_purchase(self):
        r = self.hs(execute=True)
        before = trust.compute(self.app.store, self.a["id"])["score"]
        out = self.svc.rate(r["id"], "up")
        self.assertEqual((out["id"], out["rating"]), (r["id"], "up"))
        k = self.svc.kpis("harbor")
        self.assertEqual((k["ratings"], k["rated_up"], k["satisfaction_rate"]), (1, 1, 1.0))
        self.svc.rate(r["id"], "down")                                          # changeable
        t = trust.compute(self.app.store, self.a["id"])
        self.assertEqual(t["score"], before + trust.WEIGHTS["rated_down"])
        self.assertIn("Purchases you rated thumbs down", [f["label"] for f in t["factors"]])
        self.assertEqual(self.svc.kpis("harbor")["satisfaction_rate"], 0.0)
        self.assertEqual(self.svc.public(self.svc.get(r["id"]))["rating"], "down")
        self.svc.rate(r["id"], "up")
        self.assertEqual(trust.compute(self.app.store, self.a["id"])["score"], before)

    def test_only_paid_purchases_and_valid_values(self):
        self.assertEqual(self.svc.kpis("harbor")["satisfaction_rate"], 0.0)
        self.assertEqual(self.svc.kpis("harbor")["ratings"], 0)
        preview = self.hs()
        with self.assertRaises(handshake.HandshakeError) as e:
            self.svc.rate(preview["id"], "up")
        self.assertEqual(e.exception.http, 409)
        paid = self.hs(execute=True)
        with self.assertRaises(handshake.HandshakeError):
            self.svc.rate(paid["id"], "meh")
        with self.assertRaises(handshake.HandshakeError):
            self.svc.rate("hs_0000000000000000", "up")


class TestShoppers(Base):
    def test_presets_list_and_cap(self):
        self.assertEqual(self.svc.shoppers(ISS), [])
        self.svc.set_prefs(self.app.store.agent(self.a["id"]), {"mode": "autopilot"}, ISS)
        self.assertEqual([x["name"] for x in self.svc.shoppers(ISS)], ["Shopper"])
        b = self.svc.add_shopper("bargain", None, ISS)
        self.assertEqual((b["name"], b["mode"], b["pass_state"], b["per_item_cap"], b["trust"]), ("Bargain Scout", "autopilot", "VALID", 60, 40))
        self.assertEqual(self.svc.prefs(self.app.store.agent(b["id"]))["deal_mode"], 90)
        r = self.svc.add_shopper("restock", "Pantry", ISS)
        self.assertEqual((r["name"], r["mode"]), ("Pantry", "assist"))
        self.assertEqual(self.svc.prefs(self.app.store.agent(r["id"]))["categories_allowed"], ["hardware", "office", "pet supplies"])
        self.assertIn("simulated", r["paypal"])
        with self.assertRaises(handshake.HandshakeError):
            self.svc.add_shopper("yolo", None, ISS)
        for _ in range(3):
            self.svc.add_shopper("everyday", None, ISS)
        with self.assertRaises(handshake.HandshakeError) as e:
            self.svc.add_shopper("everyday", None, ISS)
        self.assertEqual(e.exception.http, 409)
        self.assertEqual(len(self.svc.shoppers(ISS)), handshake.Service.MAX_SHOPPERS)

    def test_renaming_scout_does_not_make_a_second_one(self):
        app = fresh_app(public_demo=True)
        svc = handshake.service(app)
        s1 = svc.ensure_scout(ISS)
        app.store.update_agent(s1["id"], name="Pal")
        self.assertEqual(svc.ensure_scout(ISS)["id"], s1["id"])
        self.assertEqual(svc.resolve_agent(None)["id"], s1["id"])
        self.assertEqual([a["name"] for a in svc.shoppers(ISS)], ["Pal"])

    def test_off_shows_in_pass_verify(self):
        from lowtides import passport
        a = self.app.store.agent(self.a["id"])
        book = passport.book(self.app)
        book.issue(a, ISS)
        tok = book.current(self.app.store.agent(self.a["id"]), ISS)["token"]
        self.assertEqual(book.check(tok, amount=10, merchant="Acme", category="data")["would_allow"], "allowed")
        self.svc.set_prefs(a, {"mode": "assist"}, ISS)
        tok = book.current(self.app.store.agent(self.a["id"]), ISS)["token"]
        self.assertEqual(book.check(tok, amount=10, merchant="Acme", category="data")["would_allow"], "needs_owner_approval")
        self.svc.set_prefs(a, {"mode": "off"}, ISS)
        tok = book.current(self.app.store.agent(self.a["id"]), ISS)["token"]
        out = book.check(tok, amount=10, merchant="Acme", category="data")
        self.assertEqual(out["would_allow"], "denied")
        self.assertTrue(out["reasons"][0].startswith("OFF"))


if __name__ == "__main__":
    unittest.main()
