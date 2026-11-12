"""PayPal capture guard: check an AI agent's Lowtides Pass right before you capture its payment.

A merchant takes an AI agent's PayPal payment in two steps: the agent approves an order, then the
merchant captures it (POST /v2/checkout/orders/{id}/capture). This guard sits in the gap. Right
before the capture it asks the agent's Lowtides server (POST /v1/pass/verify) whether that agent is
still allowed to spend THIS order's amount at THIS merchant:

    allowed               -> capture the order (or authorize, then capture the authorization)
    needs_owner_approval  -> HOLD: don't capture; the agent's owner must approve in Lowtides first
    denied / revoked /
    expired / forged      -> REFUSE: don't capture, and void the authorization if the order is
                             an AUTHORIZE order that already holds the buyer's money

It fails closed: if Lowtides can't be reached, nothing is captured (HOLD).

The amount checked is read from the PayPal order itself, never taken from the agent. The verify
URL is the Lowtides server YOU trust, never the `status_url` written inside the pass.

Standard library only. Copy this file into your checkout code:

    from paypal_capture_guard import CaptureGuard, PayPalOrders
    guard = CaptureGuard(PayPalOrders(client_id, secret), merchant="Acme Data", category="data",
                         verify_url="https://lowtides.ai/v1/pass/verify")
    result = guard.capture(order_id, agent_pass)     # result["action"]: captured | held | refused

Command line:

    python paypal_capture_guard.py --pass PASS --amount 45 --merchant "Acme Data"   # dry run
    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python paypal_capture_guard.py --agent-key lt-...
        # sandbox demo: creates a sandbox order, you approve it, then the guard decides the capture

License: AGPL-3.0 (part of Lowtides, https://github.com/seniorhelper/lowtides).
"""
import argparse
import base64
import decimal
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"
DEFAULT_VERIFY_URL = "https://lowtides.ai/v1/pass/verify"

CAPTURE, HOLD, REFUSE = "capture", "hold", "refuse"


class PayPalError(Exception):
    def __init__(self, message, status=0, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


def cents(amount):
    d = decimal.Decimal(str(amount)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    return format(d, "f")


# ---- a minimal PayPal Orders v2 / Payments v2 client ------------------------------------------
class PayPalOrders:
    """Just the PayPal calls a capture needs. Every POST sends a PayPal-Request-Id so a retry
    can never capture twice."""

    def __init__(self, client_id, client_secret, base_url=SANDBOX, timeout=30):
        self.client_id, self.client_secret = client_id, client_secret
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._token, self._exp = None, 0

    def _access_token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        auth = base64.b64encode(("%s:%s" % (self.client_id, self.client_secret)).encode()).decode()
        req = urllib.request.Request(self.base + "/v1/oauth2/token", data=b"grant_type=client_credentials", method="POST")
        req.add_header("Authorization", "Basic " + auth)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise PayPalError("PayPal sign-in failed (HTTP %d). Check the client ID and secret." % e.code, e.code)
        except urllib.error.URLError as e:
            raise PayPalError("Could not reach PayPal: %s" % e)
        self._token = data["access_token"]
        self._exp = time.time() + int(data.get("expires_in", 3000))
        return self._token

    def call(self, method, path, body=None, request_id=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._access_token())
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "return=representation")
        if method == "POST":
            req.add_header("PayPal-Request-Id", request_id or str(uuid.uuid4()))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                b = json.loads(raw)
            except ValueError:
                b = {"message": raw[:300]}
            issue = ((b.get("details") or [{}])[0]).get("issue", "")
            raise PayPalError("%s%s" % (b.get("message") or b.get("name") or "PayPal error HTTP %d" % e.code,
                                         " (%s)" % issue if issue else ""), e.code, b)
        except urllib.error.URLError as e:
            raise PayPalError("Could not reach PayPal: %s" % e)

    def get_order(self, order_id):
        return self.call("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id))

    def create_order(self, amount, intent="CAPTURE", description="Purchase by an AI agent", currency="USD"):
        body = {"intent": intent,
                "purchase_units": [{"amount": {"currency_code": currency, "value": cents(amount)},
                                    "description": description[:127]}],
                "payment_source": {"paypal": {"experience_context": {
                    "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                    "return_url": "https://example.com/return", "cancel_url": "https://example.com/cancel"}}}}
        return self.call("POST", "/v2/checkout/orders", body)

    def capture_order(self, order_id):
        return self.call("POST", "/v2/checkout/orders/%s/capture" % urllib.parse.quote(order_id), {},
                         request_id="ltguard-cap-" + order_id)

    def authorize_order(self, order_id):
        return self.call("POST", "/v2/checkout/orders/%s/authorize" % urllib.parse.quote(order_id), {},
                         request_id="ltguard-auth-" + order_id)

    def capture_authorization(self, auth_id, amount, currency="USD"):
        body = {"amount": {"currency_code": currency, "value": cents(amount)}, "final_capture": True}
        return self.call("POST", "/v2/payments/authorizations/%s/capture" % urllib.parse.quote(auth_id), body,
                         request_id="ltguard-capauth-" + auth_id)

    def void_authorization(self, auth_id):
        return self.call("POST", "/v2/payments/authorizations/%s/void" % urllib.parse.quote(auth_id), {},
                         request_id="ltguard-void-" + auth_id)

    @staticmethod
    def approve_link(order):
        for link in order.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                return link.get("href")
        return None


def order_facts(order):
    """intent, status, amount, currency and any authorization of a PayPal order."""
    pu = (order.get("purchase_units") or [{}])[0]
    amt = pu.get("amount") or {}
    auths = (pu.get("payments") or {}).get("authorizations") or []
    a = auths[0] if auths else {}
    try:
        amount = float(decimal.Decimal(str(amt.get("value"))))
    except (decimal.InvalidOperation, TypeError, ValueError):
        amount = None
    return {"id": order.get("id"), "intent": order.get("intent", "CAPTURE"), "status": order.get("status", ""),
            "amount": amount, "currency": amt.get("currency_code", "USD"),
            "authorization_id": a.get("id"), "authorization_status": a.get("status")}


# ---- the Lowtides check ---------------------------------------------------------------------------
def verify_pass(pass_token, amount=None, merchant=None, category=None, verify_url=DEFAULT_VERIFY_URL, timeout=10):
    """POST /v1/pass/verify. Returns (http_status, body); (0, None) if Lowtides can't be reached."""
    body = {"pass": (pass_token or "").strip()}
    if amount is not None:
        body["amount"] = amount
    if merchant:
        body["merchant"] = merchant
    if category:
        body["category"] = category
    req = urllib.request.Request(verify_url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            return e.code, {"error": "HTTP %d" % e.code}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def decide(status, v, trusted_issuer=None):
    """Turn a /v1/pass/verify answer into (capture | hold | refuse, reasons)."""
    if status == 0 or status >= 500 or not isinstance(v, dict):
        return HOLD, ["Couldn't reach Lowtides to check the pass, so nothing was captured. Try again."]
    if status != 200:
        return REFUSE, [v.get("error") or "Lowtides refused this pass (HTTP %d)." % status]
    reasons = list(v.get("reasons") or [])
    if not v.get("signature_ok"):
        return REFUSE, reasons or ["The signature doesn't match. This pass was changed or forged."]
    if trusted_issuer and (v.get("issuer") or "").rstrip("/") != trusted_issuer.rstrip("/"):
        return REFUSE, ["This pass was issued by %s, not the Lowtides server you trust." % v.get("issuer")]
    if v.get("revoked"):
        return REFUSE, reasons or ["REVOKED: %s" % (v.get("revoked_reason") or "the owner revoked this pass.")]
    if v.get("expired") or not v.get("valid"):
        return REFUSE, reasons or ["The pass is not valid."]
    w = v.get("would_allow")
    if w == "allowed":
        return CAPTURE, reasons
    if w == "needs_owner_approval":
        return HOLD, reasons
    if w == "denied":
        return REFUSE, reasons
    return HOLD, ["The pass is valid, but no purchase was checked."]


class CaptureGuard:
    """Checks the agent's Lowtides Pass for the order's real amount right before capture."""

    def __init__(self, paypal, merchant, category=None, verify_url=DEFAULT_VERIFY_URL, trusted_issuer=None, log=None):
        self.paypal = paypal
        self.merchant = merchant
        self.category = category
        self.verify_url = verify_url
        self.trusted_issuer = trusted_issuer
        self.log = log or (lambda msg: None)

    def check(self, pass_token, amount, currency="USD"):
        """Just the decision: {"decision", "reasons", "verdict"} for a purchase of `amount`."""
        if currency != "USD":
            return {"decision": HOLD, "reasons": ["Lowtides Pass limits are in USD; this order is in %s." % currency],
                    "verdict": None}
        status, v = verify_pass(pass_token, amount, self.merchant, self.category, self.verify_url)
        decision, reasons = decide(status, v, self.trusted_issuer)
        return {"decision": decision, "reasons": reasons, "verdict": v}

    def capture(self, order_id, pass_token):
        """Capture `order_id` only if the agent's pass allows it right now.

        Returns {"action": "captured" | "held" | "refused", "decision", "reasons", "order",
        "voided": bool, "capture": PayPal reply or None, "verdict": Lowtides reply}."""
        order = order_facts(self.paypal.get_order(order_id))
        out = {"action": "refused", "decision": REFUSE, "reasons": [], "order": order, "voided": False,
               "capture": None, "verdict": None}
        intent, st = order["intent"], order["status"]
        authorized = intent == "AUTHORIZE" and order["authorization_id"] and order["authorization_status"] in ("CREATED", "PENDING")
        if not (st == "APPROVED" or authorized):
            out["reasons"] = ["PayPal order %s is %s, so there is nothing to capture." % (order_id, st or "unknown")]
            return out
        if order["amount"] is None:
            out["reasons"] = ["PayPal order %s has no readable amount." % order_id]
            return out
        c = self.check(pass_token, order["amount"], order["currency"])
        out.update(decision=c["decision"], reasons=c["reasons"], verdict=c["verdict"])
        self.log("Lowtides says %s for $%s at %s: %s" % (c["decision"].upper(), cents(order["amount"]), self.merchant,
                                                        " ".join(c["reasons"])))
        if c["decision"] == HOLD:
            out["action"] = "held"
            out["reasons"].append("Not captured. The order stays open%s until the agent's owner approves in Lowtides; "
                                  "then run the guard again." % (" (the authorization still holds the money)" if authorized else ""))
            return out
        if c["decision"] == REFUSE:
            if authorized:
                self.paypal.void_authorization(order["authorization_id"])
                out["voided"] = True
                out["reasons"].append("Voided PayPal authorization %s, so the buyer's money is released." % order["authorization_id"])
            else:
                out["reasons"].append("Not captured. No money moved.")
            return out
        if intent == "AUTHORIZE":
            auth_id = order["authorization_id"]
            if not authorized:
                auth_id = order_facts(self.paypal.authorize_order(order_id))["authorization_id"]
            out["capture"] = self.paypal.capture_authorization(auth_id, order["amount"], order["currency"])
        else:
            out["capture"] = self.paypal.capture_order(order_id)
        out["action"] = "captured"
        return out


# ---- command line: dry run, or a live demo against the PayPal sandbox ----------------------------
def _get_pass(lowtides, agent_key):
    req = urllib.request.Request(lowtides.rstrip("/") + "/v1/pass", headers={"Authorization": "Bearer " + agent_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())["pass"]
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode() or "{}")
        if body.get("pass"):
            return body["pass"]  # a revoked pass: still worth showing the guard refuse it
        sys.exit("Lowtides wouldn't give a pass: %s" % body.get("error", e.code))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check an AI agent's Lowtides Pass right before a PayPal capture.")
    ap.add_argument("--pass", dest="pass_token", default=os.environ.get("LOWTIDES_PASS"), help="the agent's pass")
    ap.add_argument("--agent-key", default=os.environ.get("LOWTIDES_AGENT_KEY"), help="or fetch the pass with this agent key")
    ap.add_argument("--lowtides", default=os.environ.get("LOWTIDES_URL", "http://127.0.0.1:8787"), help="the Lowtides server you trust")
    ap.add_argument("--amount", type=float, default=45.0)
    ap.add_argument("--merchant", default="Acme Data")
    ap.add_argument("--category", default="data")
    ap.add_argument("--intent", choices=("CAPTURE", "AUTHORIZE"), default="AUTHORIZE")
    ap.add_argument("--order", help="guard an existing, already-approved sandbox order instead of creating one")
    a = ap.parse_args(argv)
    if not a.pass_token and a.agent_key:
        a.pass_token = _get_pass(a.lowtides, a.agent_key)
    if not a.pass_token:
        ap.error("give --pass (copy it from the agent's Pass button in the dashboard) or --agent-key")
    verify_url = a.lowtides.rstrip("/") + "/v1/pass/verify"
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    base = os.environ.get("PAYPAL_BASE_URL", SANDBOX)

    if not (cid and secret):
        print("DRY RUN (no PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET): no PayPal calls are made.")
        print("Order: %s intent, $%s at %s (%s)" % (a.intent, cents(a.amount), a.merchant, a.category))
        g = CaptureGuard(None, a.merchant, a.category, verify_url)
        c = g.check(a.pass_token, a.amount)
        v = c["verdict"] or {}
        if v.get("agent"):
            print("Agent: %s (agent %s, %s), governed by %s" % (v["agent"].get("name"), v["agent"].get("id"),
                                                              v["agent"].get("status"), v.get("issuer")))
        what = {CAPTURE: "WOULD CAPTURE the order.",
                HOLD: "WOULD HOLD: not captured until the owner approves in Lowtides.",
                REFUSE: "WOULD REFUSE: not captured%s." % (", and the authorization would be voided" if a.intent == "AUTHORIZE" else "")}
        print(what[c["decision"]])
        for r in c["reasons"]:
            print("  - " + r)
        return {CAPTURE: 0, HOLD: 2, REFUSE: 3}[c["decision"]]

    if base.rstrip("/") == LIVE:
        sys.exit("This demo only runs against the PayPal sandbox.")
    pp = PayPalOrders(cid, secret, base)
    order_id = a.order
    if not order_id:
        o = pp.create_order(a.amount, a.intent, "AI agent purchase from %s" % a.merchant)
        order_id = o["id"]
        print("Created sandbox order %s (%s, $%s)." % (order_id, a.intent, cents(a.amount)))
        print("Approve it as the sandbox buyer: %s" % pp.approve_link(o))
        input("Press Enter once approved... ")
        if a.intent == "AUTHORIZE":
            pp.authorize_order(order_id)
            print("Authorized: PayPal now holds the buyer's $%s." % cents(a.amount))
    guard = CaptureGuard(pp, a.merchant, a.category, verify_url, log=print)
    r = guard.capture(order_id, a.pass_token)
    print("RESULT: %s%s" % (r["action"].upper(), " (authorization voided)" if r["voided"] else ""))
    for x in r["reasons"]:
        print("  - " + x)
    return {"captured": 0, "held": 2, "refused": 3}[r["action"]]


if __name__ == "__main__":
    sys.exit(main())
