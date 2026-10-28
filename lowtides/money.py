"""Money flows on top of PayPal: wallet top-ups, quote-and-hold tasks, client invoices.

Rounding rule (stated in the dashboard too): task charges round to the nearest cent.
Anything under half a cent is not charged at all; the hold is simply released.
"""
import datetime
import time

from . import pricing
from .paypal import PayPalError, cents
from .quote import quote as make_quote


class MoneyError(Exception):
    pass


# PayPal issues that mean the hold can't be captured at all, so the
# capture-then-refund fallback must not be tried.
TERMINAL_AUTH_ISSUES = {"AUTHORIZATION_VOIDED", "AUTHORIZATION_EXPIRED", "AUTHORIZATION_ALREADY_CAPTURED",
                        "AUTHORIZATION_DENIED", "PAYER_CANNOT_PAY", "PAYEE_ACCOUNT_LOCKED_OR_CLOSED",
                        "PAYER_ACCOUNT_LOCKED_OR_CLOSED", "INVALID_RESOURCE_ID", "PERMISSION_DENIED"}


class Money:
    def __init__(self, config, store, paypal):
        self.config = config
        self.store = store
        self.pp = paypal

    @property
    def base_url(self):
        return self.config.paypal.get("public_base_url", "http://localhost:8787").rstrip("/")

    # ---- wallet top-ups ------------------------------------------------------
    def topup_start(self, agent_id, amount):
        amount = float(amount)
        if amount < 1 or amount > 1000:
            raise MoneyError("Top-ups must be between $1 and $1,000.")
        agent = self.store.agent(agent_id)
        if not agent:
            raise MoneyError("No such agent.")
        order = self.pp.create_order(
            amount, "CAPTURE", "AI allowance for %s" % agent["name"], "topup:%d" % agent_id,
            self.base_url + "/paypal/return?kind=topup", self.base_url + "/paypal/cancel")
        self.store.add_topup(order["id"], agent_id, amount)
        return {"order_id": order["id"], "approve_url": self.pp.approve_link(order)}

    def topup_finish(self, order_id):
        t = self.store.topup(order_id)
        if not t:
            raise MoneyError("Unknown top-up.")
        if t["status"] == "COMPLETED":
            return {"status": "COMPLETED", "already": True, "balance": self.store.balance(t["agent_id"])}
        try:
            res = self.pp.capture_order(order_id)
        except PayPalError as e:
            if e.issue == "ORDER_ALREADY_CAPTURED":
                res = self.pp.get_order(order_id)
            else:
                raise
        status = res.get("status", "")
        cap_status = self.pp.capture_status(res)
        if status == "COMPLETED" and cap_status and cap_status != "COMPLETED":
            # e.g. PENDING (an eCheck or a review) or DECLINED. Credit only when the money
            # actually lands; the PAYMENT.CAPTURE.COMPLETED webhook finishes a pending one.
            status = "CAPTURE_" + cap_status
        if status == "COMPLETED":
            self.store.wallet_move(t["agent_id"], t["amount"], "paypal top-up", ref="topup:" + order_id)
            self.store.set_topup_status(order_id, "COMPLETED")
            self.store.event(t["agent_id"], "wallet", "Wallet topped up with %s via PayPal." % pricing.money(t["amount"]))
        else:
            self.store.set_topup_status(order_id, status or "UNKNOWN")
        return {"status": status, "balance": self.store.balance(t["agent_id"])}

    # ---- quote & hold ------------------------------------------------------------
    def new_task(self, agent_id, label, description="", budget=None, model=None):
        agent = self.store.agent(agent_id)
        if not agent:
            raise MoneyError("No such agent.")
        q = make_quote(self.store, self.config, agent_id, label=label, description=description, model=model)
        if budget is None:
            budget = max(q["high"], 0.01)
        tid = self.store.add_task(agent_id, label, float(budget), quote=q)
        return {"task_id": tid, "quote": q, "budget": float(budget)}

    def hold_start(self, task_id):
        t = self.store.task(task_id)
        if not t:
            raise MoneyError("No such task.")
        if t["hold_auth_id"]:
            raise MoneyError("This task already has a hold.")
        if t["status"] != "open":
            raise MoneyError("This task is already closed.")
        # A hold that was started but never approved (or was canceled) can simply be
        # started again; the unapproved PayPal order expires on its own.
        amount = max(float(self.config.paypal.get("min_hold", 1.0)), float(t["budget"]))
        order = self.pp.create_order(
            amount, "AUTHORIZE", "Hold for AI task %s" % (t["label"] or t["id"]), "hold:" + task_id,
            self.base_url + "/paypal/return?kind=hold", self.base_url + "/paypal/cancel")
        self.store.update_task(task_id, hold_order_id=order["id"], hold_amount=amount, paypal_status="AWAITING_APPROVAL")
        return {"order_id": order["id"], "approve_url": self.pp.approve_link(order), "amount": amount}

    def hold_finish(self, order_id):
        rows = self.store.q("SELECT * FROM tasks WHERE hold_order_id=?", (order_id,))
        if not rows:
            raise MoneyError("Unknown hold.")
        t = rows[0]
        if t["hold_auth_id"]:
            return {"status": "AUTHORIZED", "already": True, "task_id": t["id"]}
        res = self.pp.authorize_order(order_id)
        auth_id = self.pp.authorization_id(res)
        if not auth_id:
            raise MoneyError("PayPal did not return an authorization.")
        self.store.update_task(t["id"], hold_auth_id=auth_id, paypal_status="AUTHORIZED")
        self.store.event(t["agent_id"], "hold", "Held %s on PayPal for task '%s'." % (pricing.money(t["hold_amount"]), t["label"] or t["id"]))
        return {"status": "AUTHORIZED", "task_id": t["id"], "authorization_id": auth_id}

    def settle(self, task_id):
        """Close a task: charge only what it really cost, release the rest."""
        t = self.store.task(task_id)
        if not t:
            raise MoneyError("No such task.")
        if t["status"] == "settled":
            return {"status": "settled", "already": True, "charged": t["captured_amount"]}
        spent = self.store.task_spend(task_id)
        charge = float(cents(spent))
        result = {"spent": round(spent, 6), "charged": 0.0, "released": 0.0, "method": "none"}
        if t["hold_auth_id"]:
            hold = float(t["hold_amount"])
            charge = min(charge, hold)
            if charge < 0.01:
                self.pp.void_authorization(t["hold_auth_id"])
                result.update(released=hold, method="void")
                status = "VOIDED"
            else:
                try:
                    self.pp.capture_authorization(t["hold_auth_id"], charge, final=True)
                    result.update(charged=charge, released=round(hold - charge, 2), method="partial capture")
                    status = "CAPTURED"
                except PayPalError as e:
                    # Some accounts don't have partial capture turned on. Fall back honestly:
                    # capture the full hold, then refund the difference right away.
                    # Never fall back when the hold itself is dead or already used.
                    if e.status not in (400, 422) or e.issue in TERMINAL_AUTH_ISSUES or charge >= hold:
                        self.store.update_task(task_id, paypal_status="CAPTURE_FAILED: %s" % (e.issue or e.status))
                        raise
                    cap = self.pp.capture_authorization(t["hold_auth_id"], hold, final=True)
                    diff = round(hold - charge, 2)
                    # Write down the full capture before refunding, so a failed refund is never lost.
                    self.store.update_task(task_id, captured_amount=hold, paypal_status="CAPTURED_FULL_REFUND_PENDING")
                    try:
                        self.pp.refund_capture(cap.get("id"), diff)
                    except PayPalError as re:
                        self.store.event(t["agent_id"], "settle", "Task '%s': captured the full hold %s but the refund of %s failed (%s). "
                                         "Refund capture %s in PayPal." % (t["label"] or t["id"], pricing.money(hold), pricing.money(diff), re, cap.get("id")))
                        raise MoneyError("Captured the full hold, but the refund of %s failed. Refund capture %s in PayPal."
                                         % (pricing.money(diff), cap.get("id")))
                    result.update(charged=charge, released=diff, method="capture then refund")
                    status = "CAPTURED_REFUNDED"
            self.store.update_task(task_id, captured_amount=result["charged"], paypal_status=status)
        self.store.update_task(task_id, status="settled", closed=time.time())
        self.store.event(t["agent_id"], "settle", "Task '%s' settled: cost %s, charged %s, released %s."
                         % (t["label"] or t["id"], pricing.money(spent), pricing.money(result["charged"]), pricing.money(result["released"])))
        return result

    def finish_without_charge(self, task_id):
        """Mark a task done. If it still has a hold, release it so the customer's money
        isn't left tied up."""
        t = self.store.task(task_id)
        if not t:
            raise MoneyError("No such task.")
        if t["hold_auth_id"] and t["paypal_status"] == "AUTHORIZED":
            return self.cancel_hold(task_id)
        self.store.update_task(task_id, status="done", closed=time.time())
        return {"ok": True}

    def invoice_period(self, client_id, days):
        """Start where the last invoice ended, so periods never overlap."""
        end = time.time()
        start = end - float(days) * 86400
        last = self.store.last_invoice(client_id)
        if last and last["period_end"] and last["period_end"] > start:
            start = float(last["period_end"])
        return start, end

    def cancel_hold(self, task_id):
        t = self.store.task(task_id)
        if not t or not t["hold_auth_id"]:
            raise MoneyError("No active hold on this task.")
        self.pp.void_authorization(t["hold_auth_id"])
        self.store.update_task(task_id, paypal_status="VOIDED", status="done", closed=time.time())
        return {"status": "VOIDED"}

    # ---- client invoices (business side) ------------------------------------------------
    def invoice_preview(self, client_id, start, end):
        c = self.store.client(client_id)
        if not c:
            raise MoneyError("No such client.")
        tot = self.store.totals(client_id=client_id, since=start, until=end)
        markup = float(c["markup_percent"])
        amount = round(tot["cost"] * (1 + markup / 100.0), 2)
        period = "%s to %s" % (datetime.date.fromtimestamp(start).isoformat(), datetime.date.fromtimestamp(end - 1).isoformat())
        receipt = ("Your AI agents made %d calls (%d jobs). Unmanaged they would have cost %s. Governed cost: %s. Saved: %s."
                   % (tot["calls"], tot["tasks"], pricing.money(tot["baseline"]), pricing.money(tot["cost"]), pricing.money(tot["saved"])))
        return {"client": c, "period": period, "totals": tot, "markup_percent": markup, "amount": amount, "receipt": receipt}

    def invoice_client(self, client_id, start, end, send=False):
        p = self.invoice_preview(client_id, start, end)
        c = p["client"]
        if not c["email"]:
            raise MoneyError("Add the client's email first.")
        if p["amount"] < 0.01:
            raise MoneyError("Nothing to bill for that period.")
        items = [{"name": "AI agent usage, %s" % p["period"], "description": p["receipt"], "amount": p["amount"]}]
        # Same client + same period start = same request id, so a double-click can't
        # create a second invoice (PayPal returns the first one).
        inv = self.pp.create_invoice(c["email"], items, self.config.business.get("invoice_note", ""), memo=p["receipt"],
                                     request_id="inv-%d-%d" % (client_id, int(start)))
        inv_id = inv.get("id")
        status = inv.get("status", "DRAFT")
        if send and inv_id:
            self.pp.send_invoice(inv_id)
            status = "SENT"
        self.store.add_invoice(client_id, inv_id, start, end, p["amount"], status)
        return {"invoice_id": inv_id, "status": status, "amount": p["amount"], "receipt": p["receipt"]}

    # ---- webhooks --------------------------------------------------------------------------
    def on_webhook(self, event):
        etype = event.get("event_type", "")
        res = event.get("resource") or {}
        order_id = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
        if etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.CAPTURE.REFUNDED") and order_id and self.store.topup(order_id):
            t = self.store.topup(order_id)
            credited = self.store.one("SELECT 1 AS x FROM wallet_tx WHERE ref=?", ("topup:" + order_id,))
            if not credited:
                self.store.set_topup_status(order_id, etype.rsplit(".", 1)[1])
                return "topup not credited, nothing to take back"
            try:
                amt = float(((res.get("amount") or {}).get("value")) or t["amount"])
            except (TypeError, ValueError):
                amt = t["amount"]
            amt = min(amt, t["amount"])
            self.store.wallet_move(t["agent_id"], -amt, "paypal " + etype.rsplit(".", 1)[1].lower(), ref="%s:%s:%s" % (etype, order_id, res.get("id", "")))
            self.store.set_topup_status(order_id, etype.rsplit(".", 1)[1])
            self.store.event(t["agent_id"], "wallet", "PayPal %s a top-up. Took %s back out of the wallet."
                             % (etype.rsplit(".", 1)[1].lower(), pricing.money(amt)))
            return "topup taken back"
        if etype == "PAYMENT.CAPTURE.COMPLETED" and order_id and self.store.topup(order_id):
            t = self.store.topup(order_id)
            self.store.wallet_move(t["agent_id"], t["amount"], "paypal top-up", ref="topup:" + order_id)
            self.store.set_topup_status(order_id, "COMPLETED")
            return "topup credited"
        if etype == "CHECKOUT.ORDER.APPROVED":
            oid = res.get("id")
            if oid and self.store.topup(oid):
                self.topup_finish(oid)
                return "topup captured"
            if oid and self.store.q("SELECT 1 FROM tasks WHERE hold_order_id=?", (oid,)):
                self.hold_finish(oid)
                return "hold authorized"
        return "ignored"
