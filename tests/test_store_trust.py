"""Store trust: what the shopper sees about a store (a heuristic from GuardPooch plus the shopper's own orders)."""
import json
import os
import unittest

from helpers import agent
from test_handshake import fresh_app

from lowtides import handshake, trust

ISS = "https://lowtides.test"
W = trust.STORE_WEIGHTS


class TestStoreTrustFormula(unittest.TestCase):
    def test_safe_store_with_no_history_is_trusted(self):
        t = trust.store_trust("safe", 100)
        self.assertEqual((t["badge"], t["score"], t["kind"]), ("trusted", 75, "heuristic"))
        self.assertEqual(sum(f["delta"] for f in t["factors"]), t["score"])
        self.assertIn("Not a public rating", t["note"])

    def test_danger_is_always_danger(self):
        for kw in ({}, {"kept": 9, "up": 9}, {"up": 3, "kept": 3}):
            t = trust.store_trust("danger", 20, **kw)
            self.assertEqual(t["badge"], "danger", kw)
            self.assertLessEqual(t["score"], W["danger_max"])
        self.assertEqual(trust.store_trust("danger", 100, kept=9, up=9)["badge"], "danger")   # a high score can't buy its way out

    def test_caution_caps_at_careful(self):
        t = trust.store_trust("caution", 89, kept=3, up=2)
        self.assertEqual(t["badge"], "careful")
        self.assertLessEqual(t["score"], W["caution_max"])
        self.assertEqual(sum(f["delta"] for f in t["factors"]), t["score"])

    def test_thumbs_down_and_refunds_lower_it_and_kept_and_thumbs_up_raise_it(self):
        base = trust.store_trust("safe", 100)["score"]
        self.assertEqual(trust.store_trust("safe", 100, kept=1)["score"], base + W["kept"])
        self.assertEqual(trust.store_trust("safe", 100, kept=1, up=1)["score"], base + W["kept"] + W["up"])
        self.assertEqual(trust.store_trust("safe", 100, kept=1, down=1)["score"], base + W["kept"] + W["down"])
        self.assertEqual(trust.store_trust("safe", 100, refunds=1)["score"], base + W["refund"])
        self.assertEqual(trust.store_trust("safe", 100, kept=1, down=1)["badge"], "careful")        # one unhappy order tips it
        self.assertEqual(trust.store_trust("safe", 100, kept=50)["score"], base + W["kept_cap"])    # capped

    def test_limits_and_factors_always_add_up(self):
        for args in (("safe", 100, 9, 9, 0, 0), ("safe", 100, 0, 0, 9, 9), ("caution", 70, 2, 0, 3, 1), ("safe", 95, 1, 1, 1, 1)):
            t = trust.store_trust(*args)
            self.assertTrue(0 <= t["score"] <= 100)
            self.assertEqual(sum(f["delta"] for f in t["factors"]), t["score"], args)
        self.assertEqual(trust.store_trust("safe", 100, 0, 0, 9, 9)["badge"], "danger")             # lots of unhappy orders: below 40

    def test_pure(self):
        self.assertEqual(trust.store_trust("safe", 95, 2, 1, 0, 0), trust.store_trust("safe", 95, 2, 1, 0, 0))

    def test_the_documented_formula_matches_the_code(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "TRUST-SCORE.md"), encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("Store trust (what the shopper sees)", doc)
        for text in ("%d%% of GuardPooch" % W["gp_share_pct"], "+%d for each order kept" % W["kept"], "up to +%d" % W["kept_cap"],
                     "+%d for each thumbs up" % W["up"], "-%d for each thumbs down" % abs(W["down"]),
                     "-%d for each order pulled back" % abs(W["refund"]), "never above %d" % W["danger_max"],
                     "never above %d" % W["caution_max"], "%d or more" % W["trusted_min"]):
            self.assertIn(text, doc, text)


class TestStoreTrustOnTheServer(unittest.TestCase):
    def setUp(self):
        self.app = fresh_app()
        self.svc = handshake.service(self.app)
        self.a = agent(self.app.store, "Shopper", daily=5.0)
        self.app.store.update_agent(self.a["id"], rules=json.dumps({"max_per_purchase": 200, "daily_purchase_limit": 400, "approval_over": 100}))
        self.app.store.add_merchant("Acme", "pay@example.com", "data")
        for i in range(4):      # 4 kept purchases elsewhere: trust 64, enough for Harbor
            self.app.spend.request(self.app.store.agent(self.a["id"]), {"amount": 5 + i, "merchant": "Acme", "item": "earlier %d" % i, "category": "data"})

    def ag(self):
        return self.app.store.agent(self.a["id"])

    def by_id(self):
        return {s["id"]: s["store_trust"] for s in self.svc.stores(self.a["id"])}

    def test_stores_carry_store_trust_and_the_lookalike_is_danger(self):
        st = self.by_id()
        self.assertEqual(st["harbor"]["badge"], "trusted")
        self.assertEqual(st["fakeharbor"]["badge"], "danger")
        for t in st.values():
            self.assertEqual(t["kind"], "heuristic")
            self.assertEqual(sum(f["delta"] for f in t["factors"]), t["score"])

    def test_catalog_items_and_handshake_carry_it(self):
        items = self.svc.catalog(self.ag(), ISS)
        self.assertTrue(all(set(i["store_trust"]) == {"badge", "score"} for i in items))
        self.assertEqual({i["store_trust"]["badge"] for i in items if i["store_id"] == "fakeharbor"}, {"danger"})
        h = self.svc.handshake(self.ag(), "harbor", item="harbor-hammer", issuer=ISS)
        self.assertEqual(h["store_trust"]["badge"], "trusted")
        self.assertEqual(self.svc.handshake(self.ag(), "fakeharbor", item="fake-drill", issuer=ISS)["store_trust"]["badge"], "danger")

    def test_a_kept_order_and_a_rating_move_it_live(self):
        before = self.by_id()["harbor"]["score"]
        r = self.svc.handshake(self.ag(), "harbor", item="harbor-hammer", execute=True, issuer=ISS)
        self.assertEqual(r["signal"], "match")
        self.assertEqual(self.by_id()["harbor"]["score"], before + W["kept"])
        up = self.svc.rate(r["id"], "up")
        self.assertEqual(up["store_trust"]["score"], before + W["kept"] + W["up"])
        down = self.svc.rate(r["id"], "down")
        self.assertEqual(down["store_trust"]["score"], before + W["kept"] + W["down"])
        self.assertEqual(down["store_trust"]["badge"], "careful")
        self.assertEqual(down["agent_trust"]["score"], trust.compute(self.app.store, self.a["id"])["score"])
        self.assertEqual(self.by_id()["harbor"]["badge"], "careful")
        self.assertEqual(self.by_id()["pinepost"]["badge"], "trusted")          # other stores are untouched

    def test_kill_does_not_lower_it(self):
        before = self.by_id()["harbor"]["score"]
        self.svc.handshake(self.ag(), "harbor", item="harbor-hammer", execute=True, issuer=ISS)
        after_buy = self.by_id()["harbor"]["score"]
        self.app.spend.kill(self.a["id"])
        self.assertEqual(self.by_id()["harbor"]["score"], after_buy)
        self.assertEqual(after_buy, before + W["kept"])

    def test_history_is_per_shopper(self):
        self.svc.handshake(self.ag(), "harbor", item="harbor-hammer", execute=True, issuer=ISS)
        other = agent(self.app.store, "Other", daily=5.0)
        self.assertEqual(self.svc.store_trust(other["id"], "harbor")["score"], trust.store_trust("safe", self.svc.guard("harbor")["score"])["score"])


from test_http import ServerCase, http  # noqa: E402


class TestSecurityHeaders(ServerCase):
    NAMES = ("Content-Security-Policy", "Referrer-Policy", "Permissions-Policy", "X-Frame-Options")

    def test_every_response_carries_them(self):
        for path in ("/", "/shop/", "/api/state", "/nope", "/brand.css"):
            st, _, h = http("GET", self.url + path)
            for n in self.NAMES:
                self.assertIn(n, h, (path, n))
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
            self.assertIn("https://cdn.jsdelivr.net", h["Content-Security-Policy"])
            self.assertEqual(h["X-Frame-Options"], "DENY")
            self.assertEqual(h["Permissions-Policy"], "microphone=(self), camera=(), geolocation=()")
            self.assertNotIn("Strict-Transport-Security", h)           # plain http: never pin HTTPS

    def test_hsts_only_behind_https(self):
        st, _, h = http("GET", self.url + "/", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Strict-Transport-Security", h)

    def test_head_matches_get_without_a_body(self):
        import urllib.request
        for path, want in (("/shop/", 200), ("/nope", 404)):
            req = urllib.request.Request(self.url + path, method="HEAD")
            try:
                r = urllib.request.urlopen(req, timeout=10)
                code, hdr, body = r.status, r.headers, r.read()
            except Exception as e:
                code, hdr, body = e.code, e.headers, e.read()
            self.assertEqual(code, want)
            self.assertEqual(body, b"")
            self.assertIn("Content-Security-Policy", hdr)


class TestPlainWords(unittest.TestCase):
    def test_under_no_and_ask_first(self):
        from lowtides.spend import compile_rules
        r = compile_rules("under $80, no gift cards, ask me first above $50")
        self.assertEqual(r["rules"]["max_per_purchase"], 80)
        self.assertIn("gift cards", r["rules"]["blocked_categories"])
        self.assertEqual(r["rules"]["approval_over"], 50)
        self.assertEqual(r["notes"][0], "Read by the built-in rule reader.")
        self.assertFalse(any("didn't understand" in n for n in r["notes"]))

    def test_no_more_than_is_not_a_category_and_leftovers_are_listed(self):
        from lowtides.spend import compile_rules
        r = compile_rules("No more than $60 per purchase. Banana wizard.")
        self.assertEqual(r["rules"]["max_per_purchase"], 60)
        self.assertNotIn("more than", r["rules"]["blocked_categories"])
        self.assertTrue(any(n.startswith("I didn't understand:") and "banana wizard" in n for n in r["notes"]))
