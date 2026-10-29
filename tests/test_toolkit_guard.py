"""Tests for integrations/paypal-agent-toolkit/lowtides_guard.py.
No network and no keys: the toolkit is faked, Lowtides is a local HTTP server on 127.0.0.1."""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "integrations", "paypal-agent-toolkit"))

import lowtides_guard as lg  # noqa: E402

KEY = "lt-testkey"

# Every method in paypal_agent_toolkit/shared/tools.py as of toolkit 1.11.0.
TOOLKIT_METHODS = [
    "create_order", "pay_order", "get_order_details", "create_product", "list_products", "show_product_details",
    "create_subscription_plan", "list_subscription_plans", "show_subscription_plan_details", "create_subscription",
    "show_subscription_details", "cancel_subscription", "create_invoice", "create_recurring_series",
    "activate_recurring_series", "get_recurring_series", "cancel_recurring_series", "delete_recurring_series",
    "list_invoices", "get_invoice", "send_invoice", "send_invoice_reminder", "cancel_sent_invoice", "delete_invoice",
    "generate_invoice_qr_code", "generate_invoice_number", "setup_invoice_auto_reminders",
    "update_invoice_auto_reminder", "search_invoicing", "update_invoicing", "cancel_invoice_auto_reminder",
    "record_payment_for_invoice", "record_refund_for_invoice", "create_conditional_rules_for_invoice",
    "list_disputes", "get_dispute", "accept_dispute_claim", "create_shipment_tracking", "get_shipment_tracking",
    "update_shipment_tracking", "list_transactions", "get_merchant_insights",
]


class FakeLowtides(BaseHTTPRequestHandler):
    seen = []          # (method, path, body, auth)
    decisions = {}     # merchant -> (decision, status, reasons)
    status_of = {}     # spend id -> status for GET

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        return self.headers.get("Authorization") == "Bearer " + KEY

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeLowtides.seen.append(("POST", self.path, body))
        if not self._auth():
            return self._send(401, {"error": "bad agent key"})
        decision, status, reasons = FakeLowtides.decisions.get(body.get("merchant"), ("approved", "paid", ["ok"]))
        sid = "sp_%d" % len(FakeLowtides.seen)
        FakeLowtides.status_of[sid] = status
        self._send(200, {"id": sid, "decision": decision, "status": status, "amount": body.get("amount"),
                         "merchant": body.get("merchant"), "reasons": reasons})

    def do_GET(self):
        FakeLowtides.seen.append(("GET", self.path, None))
        if not self._auth():
            return self._send(401, {"error": "bad agent key"})
        sid = self.path.rsplit("/", 1)[-1]
        if sid not in FakeLowtides.status_of:
            return self._send(404, {"error": "not found"})
        self._send(200, {"id": sid, "status": FakeLowtides.status_of[sid], "reasons": ["checked again"]})


class FakeAPI:
    """Same shape as paypal_agent_toolkit.shared.api.PayPalAPI: run(method, params) -> str."""

    def __init__(self):
        self.calls = []
        self.orders = {"O-SMALL": ("12.50", "acme@example.com"), "O-BIG": ("900.00", "bad@example.com"),
                       "O-MID": ("35.00", "mid@example.com")}

    def run(self, method, params):
        self.calls.append(method)
        if method == "get_order_details":
            o = self.orders.get(params.get("order_id"))
            if not o:
                return "Error: order not found"
            return json.dumps({"purchase_units": [{"amount": {"value": o[0]}, "payee": {"email_address": o[1]},
                                                   "items": [{"name": "Credits"}]}]})
        if method == "show_subscription_plan_details":
            return json.dumps({"name": "Pro plan", "billing_cycles": [
                {"pricing_scheme": {"fixed_price": {"value": "0"}}},
                {"pricing_scheme": {"fixed_price": {"value": "9.99"}}}]})
        return json.dumps({"ran": method})


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeLowtides)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeLowtides.seen = []
        FakeLowtides.decisions = {}
        FakeLowtides.status_of = {}
        self.api = lg.guard_api(FakeAPI(), client=lg.LowtidesClient(self.url, KEY, timeout=5))

    def ran(self, method):
        return method in self.api.calls

    def test_every_toolkit_method_is_classified(self):
        unknown = [m for m in TOOLKIT_METHODS if lg.classify(m) == "unknown"]
        self.assertEqual(unknown, [])
        self.assertEqual(lg.classify("pay_order"), "money")
        self.assertEqual(lg.classify("get_order_details"), "pass")

    def test_read_only_passes_without_asking_lowtides(self):
        out = json.loads(self.api.run("list_transactions", {}))
        self.assertEqual(out, {"ran": "list_transactions"})
        self.assertEqual(FakeLowtides.seen, [])

    def test_approved_capture_runs_and_sends_real_amount(self):
        out = json.loads(self.api.run("pay_order", {"order_id": "O-SMALL"}))
        self.assertEqual(out, {"ran": "pay_order"})
        method, path, body = FakeLowtides.seen[0]
        self.assertEqual((method, path), ("POST", "/v1/spend"))
        self.assertEqual(body["amount"], 12.5)
        self.assertEqual(body["merchant"], "acme@example.com")
        self.assertEqual(body["item"], "Credits")
        self.assertEqual(body["settle"], "external")
        self.assertEqual(body["tool"], "pay_order")
        self.assertTrue(body["reason"])

    def test_denied_returns_reasons_and_does_not_run(self):
        FakeLowtides.decisions["bad@example.com"] = ("denied", "denied", ["Over the $50.00 per-purchase limit."])
        out = json.loads(self.api.run("pay_order", {"order_id": "O-BIG"}))["lowtides"]
        self.assertEqual(out["decision"], "denied")
        self.assertIn("Over the $50.00 per-purchase limit.", out["reasons"])
        self.assertFalse(self.ran("pay_order"))

    def test_needs_approval_waits_then_runs_after_owner_approves(self):
        FakeLowtides.decisions["mid@example.com"] = ("needs_approval", "pending", ["Over the $20.00 auto-approve limit."])
        out = json.loads(self.api.run("pay_order", {"order_id": "O-MID"}))["lowtides"]
        self.assertEqual(out["decision"], "needs_approval")
        self.assertIn("waiting for owner approval", out["message"].lower())
        self.assertIn(out["request_id"], out["message"])
        self.assertFalse(self.ran("pay_order"))
        # Retry while still pending: re-checks the same request, does not file a new one.
        json.loads(self.api.run("pay_order", {"order_id": "O-MID"}))
        self.assertEqual([s[0] for s in FakeLowtides.seen], ["POST", "GET"])
        self.assertFalse(self.ran("pay_order"))
        # Owner approves on the dashboard (Lowtides marks it paid/approved).
        FakeLowtides.status_of[out["request_id"]] = "approved"
        self.assertEqual(json.loads(self.api.run("pay_order", {"order_id": "O-MID"})), {"ran": "pay_order"})
        self.assertEqual(FakeLowtides.seen[-1][1], "/v1/spend/" + out["request_id"])

    def test_needs_approval_then_owner_denies(self):
        FakeLowtides.decisions["mid@example.com"] = ("needs_approval", "pending", ["ask"])
        sid = json.loads(self.api.run("pay_order", {"order_id": "O-MID"}))["lowtides"]["request_id"]
        FakeLowtides.status_of[sid] = "denied"
        out = json.loads(self.api.run("pay_order", {"order_id": "O-MID"}))["lowtides"]
        self.assertEqual(out["decision"], "denied")
        self.assertFalse(self.ran("pay_order"))

    def test_unknown_amount_fails_closed(self):
        out = json.loads(self.api.run("pay_order", {"order_id": "O-MISSING"}))["lowtides"]
        self.assertEqual(out["decision"], "error")
        self.assertEqual(FakeLowtides.seen, [])
        self.assertFalse(self.ran("pay_order"))

    def test_unknown_tool_is_blocked(self):
        out = json.loads(self.api.run("send_money_somewhere", {}))["lowtides"]
        self.assertEqual(out["decision"], "denied")
        self.assertFalse(self.ran("send_money_somewhere"))

    def test_bad_key_fails_closed(self):
        api = lg.guard_api(FakeAPI(), client=lg.LowtidesClient(self.url, "lt-wrong", timeout=5))
        out = json.loads(api.run("pay_order", {"order_id": "O-SMALL"}))["lowtides"]
        self.assertEqual(out["decision"], "error")
        self.assertIn("401", " ".join(out["reasons"]))
        self.assertNotIn("pay_order", api.calls)

    def test_lowtides_down_fails_closed(self):
        api = lg.guard_api(FakeAPI(), client=lg.LowtidesClient("http://127.0.0.1:1", KEY, timeout=2))
        out = json.loads(api.run("pay_order", {"order_id": "O-SMALL"}))["lowtides"]
        self.assertEqual(out["decision"], "error")
        self.assertNotIn("pay_order", api.calls)

    def test_missing_key_fails_closed(self):
        api = lg.guard_api(FakeAPI(), client=lg.LowtidesClient(self.url, "", timeout=2))
        api.run("pay_order", {"order_id": "O-SMALL"})
        self.assertNotIn("pay_order", api.calls)

    def test_subscription_and_refund_amounts(self):
        self.api.run("create_subscription", {"plan_id": "P-1", "quantity": 2})
        self.assertEqual(FakeLowtides.seen[-1][2]["amount"], 19.98)
        self.assertEqual(FakeLowtides.seen[-1][2]["merchant"], "Pro plan")
        self.api.run("create_refund", {"capture_id": "C-1", "amount": {"value": "4.00", "currency_code": "USD"}})
        self.assertEqual(FakeLowtides.seen[-1][2]["amount"], 4.0)
        self.assertTrue(self.ran("create_refund"))

    def test_guard_toolkit_covers_tools_built_before_and_after(self):
        """Mirrors the toolkit's adapters: OpenAI's PayPalTool closes over `api`; LangChain's
        tool keeps `self.paypal_api`. Both call .run at call time, so guarding in place covers them."""
        api = FakeAPI()

        class Toolkit:
            def __init__(self):
                self._paypal_api = api
                self.tools = [lambda params: api.run("pay_order", params)]  # built before guarding

            def get_paypal_api(self):
                return self._paypal_api

        class LangchainLikeTool:
            def __init__(self, paypal_api):
                self.paypal_api = paypal_api

            def _run(self, **kw):
                return self.paypal_api.run("pay_order", kw)

        tk = Toolkit()
        lc = LangchainLikeTool(api)
        FakeLowtides.decisions["bad@example.com"] = ("denied", "denied", ["no"])
        lg.guard_toolkit(tk, client=lg.LowtidesClient(self.url, KEY, timeout=5))
        lg.guard_toolkit(tk, client=lg.LowtidesClient(self.url, KEY, timeout=5))  # idempotent
        self.assertEqual(json.loads(tk.tools[0]({"order_id": "O-BIG"}))["lowtides"]["decision"], "denied")
        self.assertEqual(json.loads(lc._run(order_id="O-BIG"))["lowtides"]["decision"], "denied")
        self.assertNotIn("pay_order", api.calls)
        self.assertEqual(len([s for s in FakeLowtides.seen if s[0] == "POST"]), 2)


if __name__ == "__main__":
    unittest.main()
