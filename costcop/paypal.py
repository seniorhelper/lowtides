"""PayPal REST client (sandbox by default).

Used for three money flows:
  * Wallet top-ups       Orders API, intent CAPTURE
  * Quote & hold tasks   Orders API, intent AUTHORIZE, then capture only the real cost and void the rest
  * Client invoices      Invoicing API v2 (business side)
plus webhook signature verification.

Every write sends a PayPal-Request-Id so a retry can never double-charge.
"""
import base64
import decimal
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASES = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


class PayPalError(Exception):
    def __init__(self, message, status=0, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}

    @property
    def issue(self):
        details = self.body.get("details") or []
        if details:
            return details[0].get("issue", "")
        return self.body.get("name", "")


def cents(amount):
    """Format dollars for PayPal: two decimals, rounded half-up."""
    d = decimal.Decimal(str(amount)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    return format(d, "f")


class PayPal:
    def __init__(self, client_id, client_secret, env="sandbox", base_url=None, currency="USD"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base = (base_url or BASES.get(env, BASES["sandbox"])).rstrip("/")
        self.currency = currency
        self._token = None
        self._token_exp = 0

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    # ---- transport -------------------------------------------------------
    def _token_now(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        auth = base64.b64encode(("%s:%s" % (self.client_id, self.client_secret)).encode()).decode()
        req = urllib.request.Request(self.base + "/v1/oauth2/token", data=b"grant_type=client_credentials", method="POST")
        req.add_header("Authorization", "Basic " + auth)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise PayPalError("PayPal sign-in failed (HTTP %d). Check the client ID and secret." % e.code, e.code)
        except urllib.error.URLError as e:
            raise PayPalError("Could not reach PayPal: %s" % e)
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3000))
        return self._token

    def _call(self, method, path, body=None, request_id=None, prefer=None, raw_data=None):
        if not self.configured:
            raise PayPalError("PayPal is not set up yet. Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.", 400)
        data = raw_data if raw_data is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token_now())
        req.add_header("Content-Type", "application/json")
        if method == "POST":
            req.add_header("PayPal-Request-Id", request_id or str(uuid.uuid4()))
        if prefer:
            req.add_header("Prefer", prefer)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                b = json.loads(raw)
            except ValueError:
                b = {"message": raw[:300]}
            raise PayPalError(b.get("message") or ("PayPal error HTTP %d" % e.code), e.code, b)
        except urllib.error.URLError as e:
            raise PayPalError("Could not reach PayPal: %s" % e)

    @staticmethod
    def approve_link(order):
        for link in order.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                return link.get("href")
        return None

    # ---- orders ------------------------------------------------------------
    def create_order(self, amount, intent, description, custom_id, return_url, cancel_url, request_id=None):
        body = {
            "intent": intent,
            "purchase_units": [{
                "amount": {"currency_code": self.currency, "value": cents(amount)},
                "description": description[:127],
                "custom_id": str(custom_id)[:127],
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url,
                "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "brand_name": "CostCop",
            }}},
        }
        return self._call("POST", "/v2/checkout/orders", body, request_id=request_id)

    def get_order(self, order_id):
        return self._call("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id))

    def capture_order(self, order_id):
        return self._call("POST", "/v2/checkout/orders/%s/capture" % urllib.parse.quote(order_id), {},
                          request_id="cap-" + order_id)

    def authorize_order(self, order_id):
        return self._call("POST", "/v2/checkout/orders/%s/authorize" % urllib.parse.quote(order_id), {},
                          request_id="auth-" + order_id)

    @staticmethod
    def capture_status(order):
        """The status of the money itself. An order can say COMPLETED while its
        capture is still PENDING or was DECLINED, so credit only on this."""
        for pu in order.get("purchase_units", []):
            for c in (pu.get("payments") or {}).get("captures", []):
                return c.get("status", "")
        return ""

    @staticmethod
    def authorization_id(order):
        for pu in order.get("purchase_units", []):
            for a in (pu.get("payments") or {}).get("authorizations", []):
                return a.get("id")
        return None

    @staticmethod
    def capture_id(order):
        for pu in order.get("purchase_units", []):
            for c in (pu.get("payments") or {}).get("captures", []):
                return c.get("id")
        return None

    # ---- authorizations -------------------------------------------------------
    def capture_authorization(self, auth_id, amount, final=True):
        body = {"amount": {"currency_code": self.currency, "value": cents(amount)}, "final_capture": bool(final)}
        return self._call("POST", "/v2/payments/authorizations/%s/capture" % urllib.parse.quote(auth_id), body,
                          request_id="capauth-%s-%s" % (auth_id, cents(amount)))

    def void_authorization(self, auth_id):
        return self._call("POST", "/v2/payments/authorizations/%s/void" % urllib.parse.quote(auth_id), {},
                          request_id="void-" + auth_id)

    def refund_capture(self, capture_id, amount):
        body = {"amount": {"currency_code": self.currency, "value": cents(amount)}}
        return self._call("POST", "/v2/payments/captures/%s/refund" % urllib.parse.quote(capture_id), body,
                          request_id="refund-%s-%s" % (capture_id, cents(amount)))

    # ---- invoices ----------------------------------------------------------------
    def create_invoice(self, recipient_email, items, note, memo="", request_id=None):
        body = {
            "detail": {"currency_code": self.currency, "note": note[:4000], "memo": memo[:500],
                       "payment_term": {"term_type": "DUE_ON_RECEIPT"}},
            "primary_recipients": [{"billing_info": {"email_address": recipient_email}}],
            "items": [{"name": it["name"][:200], "description": it.get("description", "")[:1000], "quantity": "1",
                       "unit_amount": {"currency_code": self.currency, "value": cents(it["amount"])}} for it in items],
        }
        return self._call("POST", "/v2/invoicing/invoices", body, request_id=request_id, prefer="return=representation")

    def send_invoice(self, invoice_id):
        return self._call("POST", "/v2/invoicing/invoices/%s/send" % urllib.parse.quote(invoice_id),
                          {"send_to_invoicer": True}, request_id="send-" + invoice_id)

    # ---- webhooks -------------------------------------------------------------------
    def verify_webhook(self, headers, event, webhook_id, raw=None):
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo", ""),
            "cert_url": h.get("paypal-cert-url", ""),
            "transmission_id": h.get("paypal-transmission-id", ""),
            "transmission_sig": h.get("paypal-transmission-sig", ""),
            "transmission_time": h.get("paypal-transmission-time", ""),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        raw_data = None
        if raw:
            # Send the event exactly as PayPal sent it. Re-encoding the JSON can change
            # bytes (escaping, number format) and make a genuine signature fail.
            head = dict(body)
            head.pop("webhook_event")
            raw_data = json.dumps(head)[:-1].encode() + b', "webhook_event": ' + bytes(raw).strip() + b"}"
        r = self._call("POST", "/v1/notifications/verify-webhook-signature", body, raw_data=raw_data)
        return r.get("verification_status") == "SUCCESS"
