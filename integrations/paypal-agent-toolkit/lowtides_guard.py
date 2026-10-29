"""Lowtides guard for PayPal's official Agent Toolkit (Python package ``paypal-agent-toolkit``).

Every framework adapter in the toolkit (OpenAI Agents SDK, LangChain, CrewAI, Bedrock) ends in one
call: ``PayPalAPI.run(method, params)``. This module wraps that one call. Before a money-moving PayPal
tool runs, it asks Lowtides (``POST /v1/spend``). The tool runs only if Lowtides approves.

- approved        -> the PayPal tool runs and its result is returned.
- needs_approval  -> the tool does not run; the agent gets "waiting for owner approval" and the
                     Lowtides request id. Calling the same tool again with the same arguments
                     re-checks that request (GET /v1/spend/{id}) instead of filing a new one.
- denied / error  -> the tool does not run; the agent gets the reasons. The guard fails closed.

Read-only tools and writes that do not move money pass straight through.
Unknown tool names (for example, ones added in a newer toolkit release) are blocked until they are
classified here.

Standard library only. The toolkit itself is optional: it is only needed to talk to real PayPal.

    from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
    from lowtides_guard import guard_toolkit
    toolkit = guard_toolkit(PayPalToolkit(client_id=..., secret=..., configuration=...))
    tools = toolkit.get_tools()   # same tools, now governed by Lowtides

License: AGPL-3.0 (same as Lowtides).
"""

import json
import os
import urllib.error
import urllib.request

__all__ = ["LowtidesClient", "ToolkitGuard", "guard_api", "guard_toolkit",
           "MONEY_TOOLS", "PASS_THROUGH_TOOLS", "classify"]

# Tools where money actually moves, or a recurring charge is committed to.
# Method names come from python/paypal_agent_toolkit/shared/tools.py (toolkit 1.11.0).
# create_refund exists in the TypeScript toolkit; it is listed so the guard is ready if Python adds it.
MONEY_TOOLS = {
    "pay_order": "Captures payment for an order (money moves).",
    "create_refund": "Refunds a captured payment (money goes out).",
    "accept_dispute_claim": "Accepts a buyer's dispute claim (money goes back to the buyer).",
    "create_subscription": "Starts a recurring subscription charge.",
}

# Writes that do not move money by themselves. They pass through.
# create_order only creates an order; money moves when pay_order captures it, which is guarded.
# Invoices request money from a customer; record_* only book-keep payments made elsewhere.
PASS_THROUGH_TOOLS = {
    "create_order", "create_product", "create_subscription_plan", "cancel_subscription", "update_subscription",
    "create_invoice", "send_invoice", "send_invoice_reminder", "cancel_sent_invoice", "delete_invoice",
    "update_invoicing", "setup_invoice_auto_reminders", "update_invoice_auto_reminder",
    "cancel_invoice_auto_reminder", "record_payment_for_invoice", "record_refund_for_invoice",
    "create_conditional_rules_for_invoice", "create_recurring_series", "activate_recurring_series",
    "cancel_recurring_series", "delete_recurring_series",
    "create_shipment_tracking", "update_shipment_tracking",
}

READ_ONLY_PREFIXES = ("get_", "list_", "show_", "search_", "generate_")

APPROVED_STATUSES = ("approved", "paid")


def classify(method):
    """Return 'money', 'pass' or 'unknown' for a toolkit method name."""
    if method in MONEY_TOOLS:
        return "money"
    if method in PASS_THROUGH_TOOLS or method.startswith(READ_ONLY_PREFIXES):
        return "pass"
    return "unknown"


class LowtidesError(Exception):
    pass


class LowtidesClient:
    """Tiny client for the Lowtides spend API (docs/SPEND-API.md)."""

    def __init__(self, url=None, agent_key=None, timeout=10, opener=None):
        self.url = (url or os.environ.get("LOWTIDES_URL") or "http://127.0.0.1:8787").rstrip("/")
        self.agent_key = agent_key or os.environ.get("LOWTIDES_AGENT_KEY") or ""
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _call(self, method, path, body=None):
        if not self.agent_key:
            raise LowtidesError("LOWTIDES_AGENT_KEY is not set.")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.agent_key, "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("error") or e.reason
            except Exception:
                detail = e.reason
            finally:
                e.close()
            raise LowtidesError("Lowtides returned HTTP %s: %s" % (e.code, detail))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LowtidesError("Could not reach Lowtides at %s: %s" % (self.url, e))

    def request_spend(self, payload):
        return self._call("POST", "/v1/spend", payload)

    def get_spend(self, spend_id):
        return self._call("GET", "/v1/spend/" + spend_id)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _loads(raw):
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


class ToolkitGuard:
    """Decides whether one toolkit call may run. ``lookup(method, params)`` runs an unguarded
    read-only toolkit call; it is used to find the real amount (for example the order total)."""

    def __init__(self, client=None, lookup=None, task_id=None):
        self.client = client or LowtidesClient()
        self.lookup = lookup
        self.task_id = task_id
        self.pending = {}  # call signature -> Lowtides spend id

    # -- describing a call as a purchase -------------------------------------------------
    def _read(self, method, params):
        if not self.lookup:
            return {}
        try:
            return _loads(self.lookup(method, params))
        except Exception:
            return {}

    def describe(self, method, params):
        """Turn a money-moving call into a Lowtides spend request, or raise LowtidesError if the
        amount cannot be found (fail closed)."""
        p = params or {}
        if method == "pay_order":
            oid = str(p.get("order_id") or "")
            order = self._read("get_order_details", {"order_id": oid})
            unit = (order.get("purchase_units") or [{}])[0]
            amount = _num((unit.get("amount") or {}).get("value"))
            payee = unit.get("payee") or {}
            merchant = payee.get("email_address") or payee.get("merchant_id") or "PayPal order " + oid
            names = [i.get("name") for i in unit.get("items") or [] if i.get("name")]
            item = ", ".join(names)[:300] or "PayPal order " + oid
            category = "paypal order"
        elif method == "create_refund":
            amt = p.get("amount")
            amount = _num(amt.get("value") if isinstance(amt, dict) else amt)
            ref = p.get("capture_id") or p.get("captureId") or ""
            merchant, item, category = "PayPal refund", "Refund of capture %s" % ref, "paypal refund"
        elif method == "accept_dispute_claim":
            did = str(p.get("dispute_id") or p.get("disputeId") or "")
            dispute = self._read("get_dispute", {"dispute_id": did})
            amount = _num((dispute.get("dispute_amount") or {}).get("value"))
            merchant, item, category = "PayPal dispute", "Accept dispute claim %s" % did, "paypal dispute"
        elif method == "create_subscription":
            plan_id = str(p.get("plan_id") or "")
            plan = self._read("show_subscription_plan_details", {"plan_id": plan_id})
            prices = [_num(((c.get("pricing_scheme") or {}).get("fixed_price") or {}).get("value"))
                      for c in plan.get("billing_cycles") or []]
            prices = [x for x in prices if x]
            qty = _num(p.get("quantity")) or 1
            amount = max(prices) * qty if prices else None
            merchant = plan.get("name") or "PayPal plan " + plan_id
            item, category = "Subscription to plan %s" % plan_id, "paypal subscription"
        else:
            raise LowtidesError("No spend description for %s." % method)
        if not amount or amount <= 0:
            raise LowtidesError("Could not work out how much %s would move, so it was not run." % method)
        req = {"amount": round(amount, 2), "merchant": str(merchant)[:120], "item": item, "category": category,
               "reason": str(p.get("reason") or p.get("note") or "Agent called PayPal Agent Toolkit tool %s." % method)[:1000],
               # Tells Lowtides PayPal itself moves the money, so it should record, not pay out.
               "settle": "external", "source": "paypal-agent-toolkit", "tool": method}
        if self.task_id:
            req["task_id"] = self.task_id
        return req

    # -- the decision ---------------------------------------------------------------------
    def check(self, method, params):
        """Return (allowed, blocked_message_or_None, spend_or_None)."""
        kind = classify(method)
        if kind == "pass":
            return True, None, None
        if kind == "unknown":
            return False, _blocked("denied", None, ["%s is not a PayPal tool Lowtides knows about yet, so it was "
                                                    "blocked. Add it to MONEY_TOOLS or PASS_THROUGH_TOOLS." % method]), None
        sig = method + ":" + json.dumps(params or {}, sort_keys=True, default=str)
        try:
            if sig in self.pending:
                spend = self.client.get_spend(self.pending[sig])
            else:
                spend = self.client.request_spend(self.describe(method, params))
        except LowtidesError as e:
            return False, _blocked("error", None, [str(e), "Fail-closed: the PayPal tool did not run."]), None
        status = spend.get("status") or {"approved": "approved", "needs_approval": "pending"}.get(spend.get("decision"))
        if status in APPROVED_STATUSES:
            self.pending.pop(sig, None)
            return True, None, spend
        if status == "pending":
            self.pending[sig] = spend.get("id")
            return False, _blocked("needs_approval", spend, spend.get("reasons") or []), spend
        self.pending.pop(sig, None)
        return False, _blocked("denied", spend, spend.get("reasons") or ["Lowtides did not approve this."]), spend


def _blocked(decision, spend, reasons):
    sid = (spend or {}).get("id")
    if decision == "needs_approval":
        msg = ("Waiting for owner approval in Lowtides (request %s). The PayPal tool did not run. "
               "Call it again with the same arguments after the owner approves." % sid)
    elif decision == "error":
        msg = "Lowtides could not check this payment, so the PayPal tool did not run."
    else:
        msg = "Lowtides denied this payment. The PayPal tool did not run."
    return json.dumps({"lowtides": {"decision": decision, "request_id": sid, "message": msg, "reasons": list(reasons)}})


def guard_api(api, client=None, task_id=None):
    """Wrap a toolkit ``PayPalAPI`` in place: every tool built on it now goes through Lowtides.
    Works because the toolkit's tools call ``api.run(method, params)`` at call time."""
    if getattr(api, "_lowtides_guard", None):
        return api
    original = api.run
    guard = ToolkitGuard(client, lookup=original, task_id=task_id)

    def run(method, params):
        allowed, blocked, _ = guard.check(method, params)
        if not allowed:
            return blocked
        return original(method, params)

    api.run = run
    api._lowtides_guard = guard
    return api


def guard_toolkit(toolkit, client=None, task_id=None):
    """Guard a toolkit object from any adapter (openai, langchain, crewai, bedrock). Returns it."""
    api = None
    if hasattr(toolkit, "get_paypal_api"):
        api = toolkit.get_paypal_api()
    for name in ("_paypal_api", "paypal_api", "api"):
        if api is None:
            api = getattr(toolkit, name, None)
    if api is None or not hasattr(api, "run"):
        raise TypeError("Could not find the toolkit's PayPalAPI (expected get_paypal_api() or _paypal_api).")
    guard_api(api, client=client, task_id=task_id)
    return toolkit
