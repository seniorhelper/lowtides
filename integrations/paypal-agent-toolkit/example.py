"""Show Lowtides in front of PayPal's Agent Toolkit.

    python example.py --offline   # no network, no keys: fake PayPal + fake Lowtides
    python example.py             # real: paypal-agent-toolkit (sandbox) + a running Lowtides

Live mode needs PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET (sandbox app), LOWTIDES_URL, LOWTIDES_AGENT_KEY.
It creates a sandbox order and tries to capture it. A sandbox order must be approved by a buyer
before capture succeeds, so PayPal may refuse the capture; the point is that Lowtides decides first.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lowtides_guard import LowtidesClient, guard_api, guard_toolkit  # noqa: E402


class FakePayPalAPI:
    """Stands in for paypal_agent_toolkit.shared.api.PayPalAPI (same run(method, params) shape)."""

    orders = {"ORDER-SMALL": 12.50, "ORDER-MEDIUM": 35.00, "ORDER-BIG": 900.00}

    def run(self, method, params):
        if method == "get_order_details":
            total = self.orders.get(params["order_id"], 0)
            return json.dumps({"id": params["order_id"], "purchase_units": [{
                "amount": {"currency_code": "USD", "value": "%.2f" % total},
                "payee": {"email_address": "acme-data@example.com"},
                "items": [{"name": "Data credits"}]}]})
        if method == "pay_order":
            return json.dumps({"id": params["order_id"], "status": "COMPLETED"})
        if method == "list_transactions":
            return json.dumps({"transaction_details": []})
        return json.dumps({"ok": True, "method": method})


class FakeLowtides(LowtidesClient):
    """Same rules a default Lowtides agent might have: auto-approve up to $20, max $50 per purchase."""

    def __init__(self):
        super().__init__(url="http://fake", agent_key="lt-demo")
        self.n = 0

    def request_spend(self, p):
        self.n += 1
        sid = "sp_demo%d" % self.n
        if p["amount"] > 50:
            return {"id": sid, "decision": "denied", "status": "denied",
                    "reasons": ["$%.2f is over the $50.00 per-purchase limit." % p["amount"]]}
        if p["amount"] > 20:
            return {"id": sid, "decision": "needs_approval", "status": "pending",
                    "reasons": ["$%.2f is over the $20.00 auto-approve limit." % p["amount"]]}
        return {"id": sid, "decision": "approved", "status": "paid", "reasons": ["Within the $20.00 auto-approve limit."]}

    def get_spend(self, sid):
        return {"id": sid, "decision": "needs_approval", "status": "pending", "reasons": ["Still waiting."]}


def offline():
    api = guard_api(FakePayPalAPI(), client=FakeLowtides())
    for method, params in [("list_transactions", {}),
                           ("pay_order", {"order_id": "ORDER-SMALL"}),
                           ("pay_order", {"order_id": "ORDER-MEDIUM"}),
                           ("pay_order", {"order_id": "ORDER-BIG"}),
                           ("brand_new_tool", {})]:
        print("%-18s %-28s -> %s" % (method, json.dumps(params), api.run(method, params)))


def live():
    try:
        from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
        from paypal_agent_toolkit.shared.configuration import Configuration, Context
    except ImportError:
        sys.exit("Install the toolkit first: pip install -r requirements.txt (or run with --offline).")
    for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "LOWTIDES_AGENT_KEY"):
        if not os.environ.get(name):
            sys.exit("Set %s first (sandbox values only), or run with --offline." % name)
    config = Configuration(actions={"orders": {"create": True, "get": True, "capture": True}},
                           context=Context(sandbox=True))
    toolkit = guard_toolkit(PayPalToolkit(client_id=os.environ["PAYPAL_CLIENT_ID"],
                                          secret=os.environ["PAYPAL_CLIENT_SECRET"], configuration=config))
    api = toolkit.get_paypal_api()
    order = json.loads(api.run("create_order", {"currency_code": "USD", "items": [
        {"name": "Data credits", "item_cost": 12.5, "item_total": 12.5, "quantity": 1}]}))
    print("create_order ->", json.dumps(order)[:200])
    print("pay_order    ->", api.run("pay_order", {"order_id": order.get("id", "")}))
    print("Every tool from toolkit.get_tools() goes through the same guard.")


if __name__ == "__main__":
    offline() if "--offline" in sys.argv else live()
