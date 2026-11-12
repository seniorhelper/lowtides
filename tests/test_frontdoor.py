"""The consumer front door at /, the dashboard at /app/, and the shared /brand.css."""

import json
import re
import urllib.error
import urllib.request

from test_http import ServerCase, http


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def location(url):
    try:
        urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url), timeout=10)
    except urllib.error.HTTPError as e:
        return e.code, e.headers["Location"]
    return 200, None


class TestFrontDoor(ServerCase):
    def test_landing_is_the_consumer_page(self):
        s, raw, _ = http("GET", self.url + "/")
        self.assertEqual(s, 200)
        for needle in ("Let AI shop for you.", "Keep the keys.", 'id="hs-live"', "data-video-slot",
                       'href="/shop/"', 'href="/merchants/"', 'href="/handshake/"', 'href="/app/"',
                       "GuardPooch", "PayPal sandbox", "fictional demo stores", "/brand.css",
                       '<script src="/ask.js" defer></script>', 'href="/ask.css"', "Need a website?",
                       "https://github.com/seniorhelper/lowtides", "/openapi.json", 'rel="canonical"', "og:title"):
            self.assertIn(needle, raw)
        self.assertNotIn("lt-splash", raw)
        self.assertNotIn("Fraunces", raw)
        self.assertNotIn("fonts.googleapis.com", raw)

    def test_landing_json_ld_is_valid_and_matches_the_faq(self):
        s, raw, _ = http("GET", self.url + "/")
        block = re.search(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S).group(1)
        graph = json.loads(block)["@graph"]
        types = {g["@type"] for g in graph}
        self.assertTrue({"SoftwareApplication", "FAQPage"} <= types)
        faq = [g for g in graph if g["@type"] == "FAQPage"][0]["mainEntity"]
        self.assertGreaterEqual(len(faq), 5)
        for q in faq:  # every structured-data question is visible on the page
            visible = q["name"].replace("'", "’")
            self.assertTrue(q["name"] in raw or visible in raw, q["name"])

    def test_dashboard_moved_to_app_without_splash(self):
        s, raw, _ = http("GET", self.url + "/app/")
        self.assertEqual(s, 200)
        for needle in ("Agent bills &amp; controls", 'id="stop-all"', 'class="estop', "/app.js", "/tour.js",
                       "/tour.css", "/style.css", "/brand.css", "/ask.js", 'rel="canonical" href="https://lowtides.ai/app/"'):
            self.assertIn(needle, raw)
        self.assertNotIn("lt-splash", raw)
        self.assertNotIn("AI Cost Governor", raw)
        self.assertEqual(location(self.url + "/app"), (302, "/app/"))

    def test_shared_assets_are_served(self):
        for path, needle in (("/brand.css", ".estop"), ("/brand.css", ".sig--needs_yes"), ("/brand.css", ".lt-slider"),
                             ("/site.css", ".hs-sig"), ("/site.js", "data-lt-slider")):
            s, raw, h = http("GET", self.url + path)
            self.assertEqual(s, 200, path)
            self.assertIn(needle, raw)

    def test_paypal_returns_land_on_the_dashboard(self):
        self.assertEqual(location(self.url + "/paypal/cancel"), (302, "/app/#home?canceled=1"))
        code, where = location(self.url + "/paypal/return?kind=topup&token=NOPE")
        self.assertEqual(code, 302)
        self.assertTrue(where.startswith("/app/#"), where)

    def test_stop_all_wording_and_both_sides_tour(self):
        import re
        s, home, _ = http("GET", self.url + "/")
        for needle in ('id="both-sides"', 'data-spot="5"', "Stop all", "Watch the 3-minute story", 'class="hero-do"'):
            self.assertIn(needle, home)
        for path in ("/", "/shop/", "/handshake/", "/shop/shop.js", "/handshake/handshake.js", "/cc.js", "/ask.js"):
            s, raw, _ = http("GET", self.url + path)
            self.assertIsNone(re.search(r"\bKILL\b", raw), path)     # the red button is called Stop all

    def test_blog_site_map_and_patent_line(self):
        posts = ("/blog/is-ai-shopping-safe/", "/blog/agentic-commerce-consent/", "/blog/paypal-sandbox-how-it-works/",
                 "/blog/ai-agent-do-only-what-you-asked/", "/blog/ai-agent-off-switch-stop-word/", "/blog/ai-booking-deposits-payments/",
                 "/blog/why-ai-shopping-needs-consent-from-both-sides/", "/blog/ai-spending-limits-are-not-enough/")
        s, xml, _ = http("GET", self.url + "/sitemap.xml")
        s, site_map, _ = http("GET", self.url + "/sitemap/")
        for path in posts + ("/blog/",):
            self.assertIn("https://lowtides.ai" + path + "</loc>", xml)
            self.assertIn('href="%s"' % path, site_map)
            s, raw, _ = http("GET", self.url + path)
            for needle in ('rel="canonical" href="https://lowtides.ai%s"' % path, "application/ld+json", "Patent Pending", "PayPal sandbox", "/ask.js"):
                self.assertIn(needle, raw, (path, needle))
            self.assertNotIn("localhost", raw)
        for path in ("/", "/shop/", "/merchants/", "/handshake/", "/app/", "/why/", "/pass/"):
            s, raw, _ = http("GET", self.url + path)
            self.assertIn("Patent Pending", raw, path)
            self.assertNotIn("64/196", raw, path)          # never the application number

    def test_free_tools_page_runs_in_the_browser_only(self):
        s, raw, _ = http("GET", self.url + "/tools/")
        self.assertEqual(s, 200)
        for needle in ('rel="canonical" href="https://lowtides.ai/tools/"', "Patent Pending", "/tools/tools.js", "No AI reads what you type"):
            self.assertIn(needle, raw, needle)
        self.assertEqual(raw.count("<h1"), 1)
        s, js, _ = http("GET", self.url + "/tools/tools.js")
        self.assertEqual(s, 200)
        for banned in ("fetch(", "XMLHttpRequest", "innerHTML", "eval("):      # nothing typed leaves the page
            self.assertNotIn(banned, js, banned)
        s, xml, _ = http("GET", self.url + "/sitemap.xml")
        self.assertIn("https://lowtides.ai/tools/</loc>", xml)

    def test_long_posts_carry_sources_and_described_charts(self):
        for path in ("/blog/why-ai-shopping-needs-consent-from-both-sides/", "/blog/ai-spending-limits-are-not-enough/"):
            s, raw, _ = http("GET", self.url + path)
            self.assertEqual(s, 200, path)
            self.assertIn("<h2>Sources</h2>", raw, path)
            self.assertGreaterEqual(raw.count('<figure class="bl-fig">'), 2, path)
            self.assertGreaterEqual(raw.count('role="img" aria-label="'), raw.count('<figure class="bl-fig">'), path)   # every chart is described

    def test_every_tour_line_has_a_recorded_voice_clip(self):
        import re
        s, js, _ = http("GET", self.url + "/handshake/handshake.js")
        block = re.search(r"/\* TOUR_LINES:start \*/(.*?)/\* TOUR_LINES:end \*/", js, re.S).group(1)
        keys = re.findall(r"^\s*(\w+): \"", block, re.M)
        self.assertGreaterEqual(len(keys), 18)
        self.assertNotIn("LowtidesAsk.speak", js)                 # the tour never uses a synthetic browser voice
        for k in keys:
            name = {"killed": "stopped"}.get(k, k)
            with urllib.request.urlopen(self.url + "/brand/tour/%s.mp3" % name) as r:
                self.assertEqual(r.status, 200, name)
                self.assertEqual(r.headers.get("Content-Type"), "audio/mpeg", name)
                self.assertGreater(len(r.read()), 5000, name)

    def test_sitemap_and_llms_list_the_new_site_map(self):
        s, raw, _ = http("GET", self.url + "/sitemap.xml")
        for p in ("https://lowtides.ai/</loc>", "/shop/", "/handshake/", "/app/", "/merchants/", "/why/", "/pass/"):
            self.assertIn(p, raw)
        s, raw, _ = http("GET", self.url + "/llms.txt")
        for p in ("Consent Shopping", "/app/", "PayPal sandbox", "GuardPooch", "get_pass"):
            self.assertIn(p, raw)

    def test_hero_video_files_are_served_with_types_caching_and_ranges(self):
        for name, ctype in (("lowtides-hero-loop.mp4", "video/mp4"), ("lowtides-hero-loop.webm", "video/webm"),
                            ("lowtides-hero-poster.jpg", "image/jpeg")):
            req = urllib.request.Request(self.url + "/brand/" + name)
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.headers["Content-Type"], ctype)
                self.assertIn("max-age=86400", r.headers["Cache-Control"])
                self.assertGreater(len(r.read()), 10000)
        req = urllib.request.Request(self.url + "/brand/lowtides-hero-loop.mp4", headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req, timeout=10) as r:       # Safari asks for byte ranges
            self.assertEqual(r.status, 206)
            self.assertEqual(len(r.read()), 100)
            self.assertTrue(r.headers["Content-Range"].startswith("bytes 0-99/"))

    def test_every_meta_description_is_short_enough(self):
        for path in ("/", "/shop/", "/merchants/", "/handshake/", "/app/", "/why/", "/pass/"):
            s, raw, _ = http("GET", self.url + path)
            for kind in ('name="description"', 'property="og:description"'):
                m = re.search(r'<meta %s content="([^"]*)"' % kind, raw)
                self.assertTrue(m, path + kind)
                self.assertLessEqual(len(m.group(1)), 155, "%s %s is %d characters" % (path, kind, len(m.group(1))))

    def test_why_page_leads_with_consent_shopping(self):
        s, raw, _ = http("GET", self.url + "/why/")
        self.assertIn("signed Store Policy", raw)
        self.assertIn("AI cost governor", raw)            # still explained in the FAQ
        self.assertNotIn('<p class="eyebrow">The AI cost governor</p>', raw)

    def test_hero_is_the_animation_and_the_clip_lives_in_how_it_works(self):
        s, raw, _ = http("GET", self.url + "/")
        hero = raw[raw.index('<section class="hero"'):raw.index("<!-- PROBLEM -->")]
        self.assertNotIn("<video", hero)
        self.assertIn("hs-stage", hero)
        self.assertIn("Pays through PayPal (sandbox) &middot; Rules in plain English, read by AI when connected", hero)
        self.assertNotIn("<video", raw)          # the dark clip clashed with the bright page, so it is not on the page at all
        self.assertNotIn("hero.js", raw)

    def test_hero_leads_with_the_tour_and_keeps_both_doors(self):
        s, raw, _ = http("GET", self.url + "/")
        hero = raw[raw.index('<section class="hero"'):raw.index("<!-- VIDEO")]
        cta = hero[hero.index('class="hero-cta"'):hero.index("</div>", hero.index('class="hero-cta"'))]
        self.assertIn('class="lt-btn lt-btn--primary lt-btn--lg" href="/handshake/?tour=1"', cta)
        self.assertIn("Play the 90-second tour", cta)
        self.assertEqual(cta.count("lt-btn--primary"), 1)                           # one primary per section
        self.assertIn('<a class="hero-vid" href="#video" id="hero-video" hidden>', raw)            # the video is one press away, above the fold
        self.assertIn("<b>Watch the 3-minute story</b>", raw)
        doors = hero[hero.index('class="doors"'):]
        self.assertIn('class="door" href="/shop/"', doors)
        self.assertIn('class="door" href="/merchants/"', doors)
        self.assertIn("I'm a shopper", doors)
        self.assertIn("I'm a store", doors)

    def test_video_block_carries_the_real_video_id_and_loads_only_on_click(self):
        s, raw, _ = http("GET", self.url + "/")
        m = re.search(r'<section class="band video-band" id="video" data-video-id="([^"]*)"[^>]*>', raw)
        self.assertTrue(m)
        self.assertRegex(m.group(1), r"^[A-Za-z0-9_-]{11}$")                       # the uploaded video
        self.assertTrue(m.group(0).rstrip(">").endswith(" hidden"))               # hidden in the HTML itself
        self.assertNotIn("<iframe", raw)                                            # the player is injected only on click
        self.assertIn("youtube-nocookie.com/embed/", raw)
        self.assertNotIn("i.ytimg.com", raw)                                        # a local poster, not YouTube's thumbnail
        self.assertIn('src="/brand/video-poster.jpg" width="1280" height="720"', raw)
        self.assertIn('<source srcset="/brand/video-poster.webp" type="image/webp" width="1280" height="720">', raw)
        req = urllib.request.Request(self.url + "/brand/video-poster.jpg")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")

    def test_csp_allows_only_the_privacy_enhanced_player_in_frames(self):
        s, raw, h = http("GET", self.url + "/")
        csp = h.get("Content-Security-Policy", "")
        self.assertIn("frame-src https://www.youtube-nocookie.com;", csp)
        self.assertNotIn("www.youtube.com", csp)
        self.assertNotIn("ytimg", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;", csp)   # no new script origins
