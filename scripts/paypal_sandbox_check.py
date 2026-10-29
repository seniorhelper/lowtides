"""Manual check that the PayPal SANDBOX keys can do everything Lowtides needs for payouts.

    set PAYPAL_CLIENT_ID=...   set PAYPAL_CLIENT_SECRET=...      (sandbox app keys)
    python scripts/paypal_sandbox_check.py [receiver-email]

Steps: get an OAuth token -> create a $1.00 payout -> show the batch -> cancel the
UNCLAIMED item. Sandbox only, so no real money moves. Keys come from the environment only.
"""
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from lowtides.paypal import BASES, PayPal, PayPalError  # noqa: E402


def step(name, ok, detail=""):
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, (": " + detail) if detail else ""))


def main():
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not (cid and secret):
        print("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app keys) first.")
        return 2
    receiver = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LOWTIDES_DEMO_PAYEE", "merchant-demo@example.com")
    pp = PayPal(cid, secret, base_url=BASES["sandbox"])
    print("PayPal base URL:", pp.base)

    try:
        pp._token_now()
    except PayPalError as e:
        step("1. OAuth token", False, e.summary())
        return 1
    enabled = pp.payouts_enabled()
    step("1. OAuth token", True, "payouts scope %s" % ("present" if enabled else "MISSING (enable Payouts on the app)"))

    sid = "LTCHECK" + secrets.token_hex(6)
    try:
        out = pp.create_payout(receiver, 1.00, "Lowtides sandbox check", sid)
    except PayPalError as e:
        step("2. Create $1.00 payout to %s" % receiver, False, "HTTP %s %s" % (e.status, e.summary()))
        return 1
    hdr = out.get("batch_header") or {}
    bid = hdr.get("payout_batch_id")
    step("2. Create $1.00 payout to %s" % receiver, bool(bid), "batch %s, batch_status %s" % (bid, hdr.get("batch_status")))
    if not bid:
        return 1

    item_id, status, why = None, "", ""
    for _ in range(10):
        try:
            batch = pp.get_payout_batch(bid)
        except PayPalError as e:
            step("3. Show batch", False, e.summary())
            time.sleep(1)
            continue
        item_id, status, why = pp.payout_item(batch)
        bstatus = (batch.get("batch_header") or {}).get("batch_status")
        print("     batch_status=%s item=%s transaction_status=%s %s" % (bstatus, item_id, status, why))
        if item_id and status not in ("PENDING", ""):
            break
        time.sleep(1)
    step("3. Show batch", bool(item_id), "item %s is %s" % (item_id, status or "?"))
    if status != "UNCLAIMED":
        print("     Cancel needs UNCLAIMED. Use an email that is NOT a sandbox account to get UNCLAIMED.")
        return 0 if item_id else 1

    try:
        c = pp.cancel_payout_item(item_id)
        step("4. Cancel unclaimed item", c.get("transaction_status") == "RETURNED",
             "transaction_status %s" % c.get("transaction_status"))
    except PayPalError as e:
        step("4. Cancel unclaimed item", False, "HTTP %s %s" % (e.status, e.summary()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
