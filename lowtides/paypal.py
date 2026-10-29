"""PayPal REST client (sandbox by default).

Used for these money flows:
  * Wallet top-ups       Orders API, intent CAPTURE
  * Quote & hold tasks   Orders API, intent AUTHORIZE, then capture only the real cost and void the rest
  * Agent purchases      Payouts API
  * PayPal on file       Vault v3 payment tokens per agent; the kill switch deletes them
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

    @property
    def name(self):
        return self.body.get("name") or self.body.get("error") or ""

    @property
    def debug_id(self):
        return self.body.get("debug_id", "")

    @property
    def retryable(self):
        """Network failure or a PayPal 5xx: the request may or may not have gone through."""
        return self.status == 0 or self.status >= 500

    def summary(self):
        """'NAME: message (issue) [debug_id X]' -- what PayPal actually said, for the UI and audit log."""
        parts = []
        if self.name:
            parts.append(self.name + ":")
        parts.append(str(self))
        details = self.body.get("details") or []
        if details:
            d = details[0]
            extra = d.get("issue", "")
            if d.get("description"):
                extra = (extra + " - " if extra else "") + d["description"]
            if extra and extra not in str(self):
                parts.append("(%s)" % extra)
        if self.debug_id:
            parts.append("[debug_id %s]" % self.debug_id)
        return " ".join(parts)


PAYOUTS_SCOPE = "https://uri.paypal.com/payments/payouts"

# Payout item transaction_status values (Payouts v1 schema). Money never reached the
# receiver and is back with (or never left) the sender:
PAYOUT_FAILED = ("FAILED", "BLOCKED", "RETURNED", "REFUNDED", "REVERSED", "DENIED")
# Cancel works only on UNCLAIMED items.
PAYOUT_CANCELLABLE = ("UNCLAIMED",)


def cents(amount):
    """Format dollars for PayPal: two decimals, rounded half-up."""
    d = decimal.Decimal(str(amount)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    return format(d, "f")


class PayPal:
    def __init__(self, client_id, client_secret, env="sandbox", base_url=None, currency="USD"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base = (base_url or BASES.get(env, BASES["sandbox"])).rstrip("/")
        self.env = "live" if self.base == BASES["live"] else "sandbox"
        self.currency = currency
        self._token = None
        self._token_exp = 0
        self.scopes = None  # set of scopes the token grants, once signed in

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
            try:
                b = json.loads(e.read().decode("utf-8", "replace"))
            except ValueError:
                b = {}
            why = b.get("error_description") or b.get("error") or ""
            raise PayPalError("PayPal sign-in failed (HTTP %d%s). Check the client ID and secret, and that they are %s keys."
                              % (e.code, ": " + why if why else "", self.env), e.code, b)
        except urllib.error.URLError as e:
            raise PayPalError("Could not reach PayPal: %s" % e)
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3000))
        if data.get("scope"):
            self.scopes = set(str(data["scope"]).split())
        return self._token

    def payouts_enabled(self):
        """True/False from the token's scopes, None if unknown (not signed in yet)."""
        return None if self.scopes is None else PAYOUTS_SCOPE in self.scopes

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
                "brand_name": "Lowtides",
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

    # ---- payouts (agent purchases) --------------------------------------------------
    def create_payout(self, receiver_email, amount, note, item_id):
        """Pay a merchant from the owner's PayPal balance (Payouts v1, POST /v1/payments/payouts).

        Idempotent twice over: sender_batch_id (PayPal rejects a reuse within 30 days) and
        PayPal-Request-Id (PayPal replays the original response for 30 days) are both the
        spend request's ID, so a retry can never pay the same purchase twice. The reply is
        201 with batch_header.batch_status PENDING; item IDs come later from get_payout_batch."""
        body = {
            "sender_batch_header": {"sender_batch_id": str(item_id)[:256], "email_subject": "Payment from an AI agent (Lowtides)",
                                    "email_message": note[:1000]},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email[:127], "note": note[:1000],
                       "sender_item_id": str(item_id)[:63], "amount": {"currency": self.currency, "value": cents(amount)}}],
        }
        return self._call("POST", "/v1/payments/payouts", body, request_id="payout-" + str(item_id))

    def get_payout_batch(self, batch_id):
        return self._call("GET", "/v1/payments/payouts/" + urllib.parse.quote(batch_id))

    def cancel_payout_item(self, item_id):
        """Only works while the payout is UNCLAIMED (the receiver hasn't accepted it yet).
        The spec defines no body, but the sandbox answered an empty JSON body with
        INTERNAL_SERVICE_ERROR, so send an empty object (also what PayPal's own SDKs send)."""
        return self._call("POST", "/v1/payments/payouts-item/%s/cancel" % urllib.parse.quote(item_id), body={},
                          request_id="cancel-" + item_id)

    @staticmethod
    def payout_item(batch):
        """(payout_item_id, transaction_status, item error summary) of a one-item batch."""
        items = (batch or {}).get("items") or []
        if not items:
            return None, "", ""
        it = items[0]
        err = it.get("errors") or {}
        why = ""
        if err:
            why = ("%s: %s" % (err.get("name", ""), err.get("message", ""))).strip(": ")
        return it.get("payout_item_id"), it.get("transaction_status", ""), why

    # ---- vault v3 (saved PayPal for an agent) -----------------------------------------
    def create_setup_token(self, return_url, cancel_url, description, request_id=None):
        """Step 1 of saving a payer's PayPal (POST /v3/vault/setup-tokens). The reply's
        'approve' link sends the payer to PayPal to agree to let this merchant charge them
        later without them present (usage_type MERCHANT)."""
        body = {"payment_source": {"paypal": {
            "description": description[:127],
            "usage_type": "MERCHANT",
            "usage_pattern": "IMMEDIATE",
            "customer_type": "CONSUMER",
            "permit_multiple_payment_tokens": False,
            "experience_context": {"return_url": return_url, "cancel_url": cancel_url, "brand_name": "Lowtides",
                                   "shipping_preference": "NO_SHIPPING"},
        }}}
        return self._call("POST", "/v3/vault/setup-tokens", body, request_id=request_id)

    def get_setup_token(self, setup_token_id):
        return self._call("GET", "/v3/vault/setup-tokens/" + urllib.parse.quote(setup_token_id))

    def create_payment_token(self, setup_token_id):
        """Step 2, after the payer approved: turn the setup token into a saved payment token
        (POST /v3/vault/payment-tokens). Same setup token = same request id, so a double
        return from PayPal can't save two tokens."""
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        return self._call("POST", "/v3/vault/payment-tokens", body, request_id="vault-" + setup_token_id)

    def get_payment_token(self, token_id):
        return self._call("GET", "/v3/vault/payment-tokens/" + urllib.parse.quote(token_id))

    def delete_payment_token(self, token_id):
        """Revoke the saved PayPal (DELETE /v3/vault/payment-tokens/{id}, 204). After this
        nobody can charge it, including anyone holding the token ID."""
        return self._call("DELETE", "/v3/vault/payment-tokens/" + urllib.parse.quote(token_id))

    @staticmethod
    def payer_email(token):
        return ((token.get("payment_source") or {}).get("paypal") or {}).get("email_address", "")

    def charge_vaulted(self, vault_id, amount, description, custom_id, request_id):
        """Charge a saved PayPal with no payer present: Orders v2, intent CAPTURE,
        payment_source.paypal.vault_id. PayPal completes it in one call (no approve step)."""
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": self.currency, "value": cents(amount)},
                                "description": description[:127], "custom_id": str(custom_id)[:127]}],
            "payment_source": {"paypal": {"vault_id": vault_id}},
        }
        return self._call("POST", "/v2/checkout/orders", body, request_id=request_id)

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
