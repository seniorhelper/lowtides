"""Merchant side of the Lowtides Pass: integrations/merchant-check/paypal_capture_guard.py
against a fake PayPal (Orders v2 + authorizations) and a real local Lowtides server issuing
passes; the JS checker (node --test, when Node is installed); and the /merchants/ page."""
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_http import ServerCase, http

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "integrations", "merchant-check")
sys.path.insert(0, KIT)

import paypal_capture_guard as pcg  # noqa: E402


class FakeOrders(BaseHTTPRequestHandler):
    """Just enough PayPal: OAuth, GET order (full), capture, authorize, auth capture, void."""

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = b"" if code == 204 else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _order(self, o):
        pu = {"amount": {"currency_code": "USD", "value": o["value"]}}
        if o.get("auth"):
            a = self.server.st["auths"][o["auth"]]
            pu["payments"] = {"authorizations": [{"id": o["auth"], "status": a["status"]}]}
        return {"id": o["id"], "intent": o["intent"], "status": o["status"], "purchase_units": [pu]}

    def do_GET(self):
        st = self.server.st
        st["calls"].append("GET " + self.path)
        o = st["orders"].get(self.path.rsplit("/", 1)[-1])
        return self._send(200, self._order(o)) if o else self._send(404, {"name": "RESOURCE_NOT_FOUND"})

    def do_POST(self):
        st = self.server.st
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}  # the OAuth call is form-encoded
        p = self.path
        st["calls"].append("POST " + p)
        if p == "/v1/oauth2/token":
            return self._send(200, {"access_token": "T", "expires_in": 3600})
        if self.headers.get("Authorization") != "Bearer T" or not self.headers.get("PayPal-Request-Id"):
            return self._send(401, {"name": "AUTHENTICATION_FAILURE"})
        parts = p.strip("/").split("/")
        if p.startswith("/v2/checkout/orders/") and p.endswith("/capture"):
            o = st["orders"][parts[3]]
            if o["intent"] != "CAPTURE" or o["status"] != "APPROVED":
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            o["status"] = "COMPLETED"
            st["captured"].append(o["id"])
            return self._send(201, {"id": o["id"], "status": "COMPLETED"})
        if p.startswith("/v2/checkout/orders/") and p.endswith("/authorize"):
            o = st["orders"][parts[3]]
            o["auth"] = "AUTH" + uuid.uuid4().hex[:8]
            st["auths"][o["auth"]] = {"status": "CREATED", "order": o["id"]}
            o["status"] = "COMPLETED"
            return self._send(201, self._order(o))
        if p.startswith("/v2/payments/authorizations/"):
            a = st["auths"][parts[3]]
            if a["status"] != "CREATED":
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "AUTHORIZATION_ALREADY_" + a["status"]}]})
            if p.endswith("/capture"):
                a["status"] = "CAPTURED"
                st["captured"].append(a["order"])
                return self._send(201, {"id": "CAP1", "status": "COMPLETED", "amount": body.get("amount")})
            if p.endswith("/void"):
                a["status"] = "VOIDED"
                st["voided"].append(parts[3])
                return self._send(200, {"id": parts[3], "status": "VOIDED"})
        return self._send(404, {"name": "NOT_FOUND"})


class MerchantGuardCase(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeOrders)
        cls.fake.st = {"orders": {}, "auths": {}, "calls": [], "captured": [], "voided": []}
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.fake_url = "http://127.0.0.1:%d" % cls.fake.server_address[1]
        # Acme Data is on the owner's merchant list, with a PayPal payee, so small buys go through.
        cls.api(cls, "POST", "/api/merchants", {"name": "Acme Data", "paypal_email": "acme@example.com", "category": "data"})

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        super().tearDownClass()

    def setUp(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Buyer %s" % uuid.uuid4().hex[:6], "daily_budget": 3})
        self.agent = r["agent"]
        s, r, _ = http("GET", self.url + "/v1/pass", headers={"Authorization": "Bearer " + self.agent["key"]})
        self.assertEqual(s, 200, r)
        self.token = r["pass"]
        self.guard = pcg.CaptureGuard(pcg.PayPalOrders("id", "secret", self.fake_url), merchant="Acme Data",
                                      category="data", verify_url=self.url + "/v1/pass/verify")

    def order(self, value, intent="CAPTURE", authorized=False):
        oid = "ORD" + uuid.uuid4().hex[:10].upper()
        o = {"id": oid, "intent": intent, "status": "APPROVED", "value": value}
        self.fake.st["orders"][oid] = o
        if authorized:
            o["auth"] = "AUTH" + uuid.uuid4().hex[:8]
            self.fake.st["auths"][o["auth"]] = {"status": "CREATED", "order": oid}
            o["status"] = "COMPLETED"
        return oid

    def test_valid_pass_captures(self):
        oid = self.order("12.00")
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "captured", r["reasons"])
        self.assertEqual(r["verdict"]["would_allow"], "allowed")
        self.assertIn(oid, self.fake.st["captured"])
        # The amount checked came from the PayPal order, not from the agent.
        self.assertEqual(r["order"]["amount"], 12.0)

    def test_valid_pass_captures_an_authorization(self):
        oid = self.order("9.50", "AUTHORIZE", authorized=True)
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "captured", r["reasons"])
        auth = self.fake.st["orders"][oid]["auth"]
        self.assertEqual(self.fake.st["auths"][auth]["status"], "CAPTURED")
        # And an approved AUTHORIZE order that wasn't authorized yet: authorize, then capture.
        oid2 = self.order("7.25", "AUTHORIZE")
        self.assertEqual(self.guard.capture(oid2, self.token)["action"], "captured")
        self.assertIn(oid2, self.fake.st["captured"])

    def test_kill_revokes_so_guard_refuses_and_voids(self):
        oid = self.order("12.00", "AUTHORIZE", authorized=True)
        s, _, _ = self.api("POST", "/api/agents/%d/kill" % self.agent["id"], {})
        self.assertEqual(s, 200)
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "refused")
        self.assertTrue(r["voided"])
        self.assertTrue(r["verdict"]["revoked"])
        self.assertTrue(any("REVOKED" in x for x in r["reasons"]), r["reasons"])
        auth = self.fake.st["orders"][oid]["auth"]
        self.assertEqual(self.fake.st["auths"][auth]["status"], "VOIDED")
        self.assertNotIn(oid, self.fake.st["captured"])

    def test_owner_revoke_refuses_a_capture_order_without_moving_money(self):
        oid = self.order("12.00")
        self.api("POST", "/api/agents/%d/pass/revoke" % self.agent["id"], {})
        r = self.guard.capture(oid, self.token)
        self.assertEqual((r["action"], r["voided"]), ("refused", False))
        self.assertEqual(self.fake.st["orders"][oid]["status"], "APPROVED")
        self.assertNotIn(oid, self.fake.st["captured"])

    def test_over_approval_line_holds(self):
        oid = self.order("45.00", "AUTHORIZE", authorized=True)
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "held", r["reasons"])
        self.assertEqual(r["verdict"]["would_allow"], "needs_owner_approval")
        self.assertFalse(r["voided"])
        auth = self.fake.st["orders"][oid]["auth"]
        self.assertEqual(self.fake.st["auths"][auth]["status"], "CREATED")  # money still held, not captured

    def test_over_per_purchase_limit_refuses(self):
        oid = self.order("480.00", "AUTHORIZE", authorized=True)
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "refused")
        self.assertTrue(r["voided"])
        self.assertTrue(any("per-purchase limit" in x for x in r["reasons"]), r["reasons"])

    def test_tampered_pass_refuses(self):
        h, p, sig = self.token.split(".")
        payload = json.loads(pcg.base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        payload["limits"]["max_per_purchase"] = 99999
        forged = h + "." + pcg.base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode() + "." + sig
        oid = self.order("12.00", "AUTHORIZE", authorized=True)
        r = self.guard.capture(oid, forged)
        self.assertEqual(r["action"], "refused")
        self.assertFalse(r["verdict"]["signature_ok"])
        self.assertTrue(r["voided"])
        r = self.guard.capture(self.order("12.00"), "not-a-pass")
        self.assertEqual(r["action"], "refused")

    def test_lowtides_down_holds_and_untrusted_issuer_refuses(self):
        oid = self.order("12.00")
        down = pcg.CaptureGuard(self.guard.paypal, "Acme Data", "data", verify_url="http://127.0.0.1:9/v1/pass/verify")
        r = down.capture(oid, self.token)
        self.assertEqual(r["action"], "held")
        self.assertNotIn(oid, self.fake.st["captured"])
        picky = pcg.CaptureGuard(self.guard.paypal, "Acme Data", "data", verify_url=self.url + "/v1/pass/verify",
                                 trusted_issuer="https://lowtides.ai")
        self.assertEqual(picky.capture(oid, self.token)["action"], "refused")

    def test_already_captured_order_is_left_alone(self):
        oid = self.order("12.00")
        self.fake.st["orders"][oid]["status"] = "COMPLETED"
        r = self.guard.capture(oid, self.token)
        self.assertEqual(r["action"], "refused")
        self.assertIsNone(r["verdict"])  # didn't even ask Lowtides

    def test_cli_dry_run(self):
        env = dict(os.environ, PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="")
        run = lambda amt: subprocess.run([sys.executable, os.path.join(KIT, "paypal_capture_guard.py"), "--pass", self.token,
                                          "--lowtides", self.url, "--amount", amt], capture_output=True, text=True, env=env,
                                         timeout=60)
        r = run("12")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("WOULD CAPTURE", r.stdout)
        r = run("45")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("WOULD HOLD", r.stdout)


class TestMerchantPage(ServerCase):
    def test_page_and_crawler_files(self):
        s, raw, _ = http("GET", self.url + "/merchants/")
        self.assertEqual(s, 200)
        for needle in ("For merchants", "/v1/pass/verify", 'rel="canonical"', "application/ld+json", "/pass/", "/why/",
                       "lowtides-pass-check.js", "Need a website?"):
            self.assertIn(needle, raw)
        s, js, h = http("GET", self.url + "/merchants/lowtides-pass-check.js")
        self.assertEqual(s, 200)
        self.assertIn("javascript", h.get("Content-Type", ""))
        s, raw, _ = http("GET", self.url + "/sitemap.xml")
        self.assertIn("https://lowtides.ai/merchants/", raw)
        s, raw, _ = http("GET", self.url + "/llms.txt")
        self.assertIn("(https://lowtides.ai/merchants/)", raw)

    def test_page_copy_of_the_checker_matches_the_integration(self):
        with open(os.path.join(KIT, "lowtides-pass-check.js"), "rb") as a, \
                open(os.path.join(ROOT, "ui", "merchants", "lowtides-pass-check.js"), "rb") as b:
            self.assertEqual(a.read(), b.read(), "ui/merchants/lowtides-pass-check.js must be a copy of the integration file")


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class TestJsChecker(unittest.TestCase):
    def test_node_tests(self):
        r = subprocess.run(["node", "--test", os.path.join(KIT, "lowtides-pass-check.test.mjs")], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
