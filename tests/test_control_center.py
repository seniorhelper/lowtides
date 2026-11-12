"""Phase 2 control centers: /shop/, /handshake/, /merchants/ and their shared /cc.css + /cc.js."""

import json
import re

from test_http import ServerCase, http

NAV = ('href="/#how"', 'href="/shop/"', 'href="/merchants/"', 'href="/handshake/"')
COMMON = ('href="/brand.css"', 'href="/cc.css"', '<script src="/cc.js" defer></script>', 'href="/ask.css"',
          '<script src="/ask.js" defer></script>', "data-ask=", 'rel="canonical"', "og:title", 'name="description"',
          "PayPal sandbox, test money, fictional demo stores", 'class="lt-header"', "Need a website?")


class TestControlCenterPages(ServerCase):
    def page(self, path):
        s, raw, h = http("GET", self.url + path)
        self.assertEqual(s, 200, path)
        self.assertIn("text/html", h.get("Content-Type", ""))
        for needle in NAV + COMMON:
            self.assertIn(needle, raw, "%s is missing %s" % (path, needle))
        self.assertNotIn("fonts.googleapis.com", raw)
        self.assertNotIn("Fraunces", raw)
        self.assertEqual(len(re.findall(r"<h1[\s>]", raw)), 1, path + " needs exactly one h1")
        self.assertIn('aria-current="page"', raw)
        block = re.search(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S)
        if block:
            json.loads(block.group(1))
        return raw

    def test_shop_page(self):
        raw = self.page("/shop/")
        for needle in ("Your AI shopper, your rules.", 'id="daily_cap"', 'id="per_item_cap"', 'id="ask_above"', 'id="deal_mode"',
                       'name="guard_strictness" value="relaxed"', 'value="balanced"', 'value="strict"', 'id="cats"', 'id="never"',
                       'id="results"', 'id="hs-panel"', 'id="trust"', 'id="activity"', 'class="estop"', 'id="kill-dock"', 'id="reset"',
                       'href="/pass/"', 'name="mode" value="autopilot"', 'value="assist"', 'name="mode" value="off"', 'id="agents"', 'data-preset="everyday"', 'data-preset="bargain"', 'data-preset="restock"', 'id="plain"', 'id="money"', 'data-q="tape measure"', 'data-q="printer ink"', 'data-q="dog food"', 'Lowtides never sees your card. PayPal holds it.', '<script src="/shop/shop.js" defer></script>', 'href="https://lowtides.ai/shop/"'):
            self.assertIn(needle, raw)
        s, js, h = http("GET", self.url + "/shop/shop.js")
        self.assertEqual(s, 200)
        s, shared, _ = http("GET", self.url + "/cc.js")
        for route in ("/api/shopper/agents", "/api/rules/compile", "/kill", "/update", "/api/shopper/prefs", "/api/catalog", "/api/handshake", "/api/handshakes", "/api/stop-all", "/api/demo/reset", "/trust", "/pass"):
            self.assertTrue(route in js or route in shared, route)

    def test_handshake_page(self):
        raw = self.page("/handshake/")
        for needle in ("See both sides", 'id="steps"', 'id="next"', 'id="back"', 'id="play"', 'id="restart"', 'id="sound" aria-pressed="false"',
                       'id="tv-live"', 'id="tv-count"', 'id="cap-big"', 'id="say" aria-live="polite"', 'id="stage"', 'id="fp-stage"', 'id="start"',
                       'id="end"', 'href="/shop/" id="end-shop"', 'href="/merchants/" id="end-store"', 'id="again"', 'id="reset"',
                       "That’s Lowtides: both say yes, or no money moves.", 'href="/handshake/handshake.css"', 'id="tiles"', 'id="hs-panel"',
                       'id="free"', 'id="tour-x"', 'name="mode" value="assist"', 'id="sync-status"', 'id="f-store"', 'id="f-item"', '<script src="/handshake/handshake.js" defer></script>'):
            self.assertIn(needle, raw)
        s, js, _ = http("GET", self.url + "/handshake/handshake.js")
        self.assertEqual(s, 200)
        for needle in ("harbor-anchors", "harbor-miter", "fakeharbor", "brightside", "/api/stop-all", "/api/demo/reset", "/resume", "/approve",
                       "tour=1", "/brand/tour/", "clip.pause()", "LT.RM", "lt_tour_voice",
                       "ArrowRight", "ArrowLeft", "Escape", "TOUR_LINES:start", 'class="estop"'):
            self.assertIn(needle, js)
        s, css, h = http("GET", self.url + "/handshake/handshake.css")
        self.assertEqual(s, 200)
        self.assertIn("prefers-reduced-motion: reduce", css)
        for prop in re.findall(r"transition:\s*([^;}]+)", css):                     # motion is transform/opacity only
            for part in re.split(r",(?![^(]*\))", prop):
                self.assertTrue(part.split()[0] in ("opacity", "transform", "none", "background-color", "border-color"), prop)

    def test_tour_entry_points_on_both_sides(self):
        for path in ("/", "/shop/", "/merchants/"):
            s, raw, _ = http("GET", self.url + path)
            self.assertIn('href="/handshake/?tour=1"', raw, path)
            self.assertIn("Play the 90-second tour", raw, path)
        for path in ("/shop/", "/merchants/"):
            s, raw, _ = http("GET", self.url + path)
            hero = raw[raw.index('<section class="cc-hero"'):raw.index("</section>", raw.index('<section class="cc-hero"'))]
            self.assertIn('class="lt-btn lt-btn--ghost" href="/handshake/?tour=1"', hero, path)   # secondary: the header CTA is the primary
            self.assertNotIn("lt-btn--primary", hero, path)

    def test_merchants_page_leads_with_the_store_control_center(self):
        raw = self.page("/merchants/")
        for needle in ("Know which AI shoppers to trust.", 'id="stores"', 'id="max_order_no_human"', 'id="min_trust"',
                       'id="require_evidence"', 'id="opp-list"', 'Rule-based suggestions from your own orders', 'name="smode" value="review"', 'value="closed"', 'id="blocked"', 'id="publish"', 'id="kpis"', 'id="chart"', 'id="grid-agents"', 'id="grid-hs"',
                       '<script src="/merchants/store.js" defer></script>',
                       # the checkout simulator and verifier stay
                       'id="try"', "/v1/pass/verify", "lowtides-pass-check.js"):
            self.assertIn(needle, raw)
        self.assertLess(raw.index('id="policy"'), raw.index('id="try"'), "the store control center comes first")
        self.assertNotIn("ag-grid-community", raw, "AG Grid is fetched late, by store.js, not by the page")
        # the developer half lives in one collapsed <details> at the very bottom, after the control center
        self.assertRegex(raw, r'<details class="dev-more" id="dev-more">\s*<summary>For developers: verify a Pass in your checkout</summary>')
        self.assertNotIn('<details class="dev-more" id="dev-more" open', raw)
        self.assertLess(raw.index('id="dev-more"'), raw.index('id="try"'))
        self.assertLess(raw.index('id="grid-hs"'), raw.index('id="dev-more"'))
        self.assertIn('id="scope-all" aria-pressed="true"', raw)
        self.assertIn('id="kpi-actions"', raw)
        s, js, _ = http("GET", self.url + "/merchants/store.js")
        self.assertEqual(s, 200)
        for needle in ("function plainCsv", "function drawPlain", "No AI orders yet", "/proof"):
            self.assertIn(needle, js)
        for needle in ("ag-grid-community@32.3.3", "IntersectionObserver", "/api/stores/", "/policy", "/api/merchant/kpis", "exportDataAsCsv", "quickFilterText", "returning_shoppers",
                       "kept_order_rate", "net_captured_usd", "bad_orders_stopped", "satisfaction proxy", "/api/merchant/opportunities",
                       "policy_patch", "satisfaction_rate", "no ratings yet"):
            self.assertIn(needle, js)

    def test_ag_grid_only_on_merchants(self):
        for path in ("/shop/", "/handshake/", "/"):
            s, raw, _ = http("GET", self.url + path)
            self.assertNotIn("ag-grid-community", raw, path)

    def test_no_stale_live_demo_links_to_root(self):
        for path in ("/merchants/", "/pass/"):
            s, raw, _ = http("GET", self.url + path)
            self.assertNotIn(">Live demo<", raw, path)
            # the only link to "/" left is the logo
            self.assertEqual(len(re.findall(r'href="/"', raw)), 1, path)

    def test_shared_assets(self):
        s, css, h = http("GET", self.url + "/cc.css")
        self.assertEqual(s, 200)
        self.assertIn("text/css", h.get("Content-Type", ""))
        self.assertIn("prefers-reduced-motion", css)
        s, js, h = http("GET", self.url + "/cc.js")
        self.assertEqual(s, 200)
        for needle in ("window.LT", "renderHandshake", "Both said yes", "Needs your yes", "No match",
                       "PayPal sandbox", "simulated", "/proof", "heuristic", "/rating", "Was this what you wanted?"):
            self.assertIn(needle, js)

    def test_approval_links_point_at_the_dashboard(self):
        with open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "lowtides", "notify.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/app/#approvals"', src)
        self.assertNotIn('rstrip("/") + "/#approvals"', src)
