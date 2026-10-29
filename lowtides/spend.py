"""Spend requests: agents ask before they buy anything.

Every purchase an agent wants to make goes through here first. Rules decide
approved / needs_approval / denied with plain-English reasons. Approved purchases are
paid to the merchant with PayPal Payouts (or simulated, clearly labeled, when PayPal
isn't set up). Each decision lands in the hash-chained audit log.

The LLM never decides a payment. Claude only translates plain-English rules into the
JSON below, a person saves them, and every check here is plain code.
"""
import datetime
import json
import re
import secrets
import statistics
import time
import urllib.error
import urllib.request

from .guard import start_of_today
from .paypal import PayPalError

DEFAULT_RULES = {
    "max_per_purchase": 50.0,
    "daily_purchase_limit": 200.0,
    "approval_over": 20.0,
    "daily_total_limit": None,      # AI usage + purchases together; None = off
    "allowed_categories": [],
    "blocked_categories": ["gift cards", "crypto"],
    "blocked_merchants": [],
    "new_merchant_needs_approval": True,
    "hours": None,
    "max_purchases_per_hour": 10,
}

MONEY_FIELDS = ("max_per_purchase", "daily_purchase_limit", "approval_over", "daily_total_limit")
LIST_FIELDS = ("allowed_categories", "blocked_categories", "blocked_merchants")

# Text in a purchase that reads like orders aimed at the agent itself. Agents that
# read web pages or emails can be steered this way into paying the wrong party.
INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules)|disregard (your|the|all) (rules|instructions)"
    r"|system prompt|you are now|new instructions|developer mode|jailbreak"
    r"|(send|wire|transfer) (the )?(money|funds|payment|it) to|pay (this|the following) (account|address|email) instead"
    r"|act as (an? )?(admin|administrator|owner)|override (the )?(budget|limit|rules)", re.I)

DUP_WINDOW = 600          # seconds: same agent, merchant and amount = possible double charge
UNUSUAL_MULTIPLE = 3.0    # times the agent's typical purchase
UNUSUAL_MIN_HISTORY = 5


class SpendError(Exception):
    def __init__(self, message, http=400):
        super().__init__(message)
        self.http = http


def money(x):
    return "$%.2f" % float(x)


# ---- rules -------------------------------------------------------------------------------
def validate_rules(raw):
    """Return a clean, complete rules dict, or raise SpendError explaining what's wrong."""
    if not isinstance(raw, dict):
        raise SpendError("Rules must be a JSON object.")
    out = dict(DEFAULT_RULES)
    for k, v in raw.items():
        if k not in DEFAULT_RULES:
            raise SpendError("Unknown rule '%s'." % k)
        if k in MONEY_FIELDS:
            if v is None and k == "daily_total_limit":
                out[k] = None
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise SpendError("'%s' must be a dollar amount." % k)
            if v < 0 or v > 1_000_000:
                raise SpendError("'%s' must be between $0 and $1,000,000." % k)
            out[k] = round(v, 2)
        elif k in LIST_FIELDS:
            if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
                raise SpendError("'%s' must be a list of words." % k)
            out[k] = sorted({s.strip().lower() for s in v if s.strip()})
        elif k == "new_merchant_needs_approval":
            out[k] = bool(v)
        elif k == "max_purchases_per_hour":
            try:
                out[k] = max(0, int(v))
            except (TypeError, ValueError):
                raise SpendError("'max_purchases_per_hour' must be a whole number.")
        elif k == "hours":
            if v is None:
                out[k] = None
            elif isinstance(v, dict) and all(isinstance(v.get(x), (int, float)) for x in ("start", "end")) \
                    and 0 <= v["start"] < 24 and 0 < v["end"] <= 24 and v["start"] < v["end"]:
                out[k] = {"start": int(v["start"]), "end": int(v["end"])}
            else:
                raise SpendError("'hours' must be null or {\"start\": 9, \"end\": 18}.")
    return out


def agent_rules(agent):
    try:
        raw = json.loads(agent.get("rules") or "{}")
    except ValueError:
        raw = {}
    try:
        return validate_rules(raw)
    except SpendError:
        return dict(DEFAULT_RULES)


def describe_rules(r):
    """The rules in plain English, for previews and the evidence pack."""
    lines = ["Up to %s per purchase." % money(r["max_per_purchase"]),
             "Up to %s of purchases per day." % money(r["daily_purchase_limit"]),
             "Purchases over %s need a person to approve them." % money(r["approval_over"])]
    if r["daily_total_limit"] is not None:
        lines.append("AI usage and purchases together stop at %s per day." % money(r["daily_total_limit"]))
    if r["allowed_categories"]:
        lines.append("Only these categories: %s." % ", ".join(r["allowed_categories"]))
    if r["blocked_categories"]:
        lines.append("Never: %s." % ", ".join(r["blocked_categories"]))
    if r["blocked_merchants"]:
        lines.append("Blocked merchants: %s." % ", ".join(r["blocked_merchants"]))
    if r["new_merchant_needs_approval"]:
        lines.append("A merchant not on your list needs approval.")
    if r["hours"]:
        lines.append("Only between %d:00 and %d:00." % (r["hours"]["start"], r["hours"]["end"]))
    if r["max_purchases_per_hour"]:
        lines.append("At most %d purchases an hour." % r["max_purchases_per_hour"])
    return lines


_NUM = r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)"


def _amount(s):
    return float(s.replace(",", ""))


def _hour(h, ampm):
    h = int(h)
    if ampm and ampm.lower() == "pm" and h < 12:
        h += 12
    if ampm and ampm.lower() == "am" and h == 12:
        h = 0
    return h


def _words(s):
    parts = re.split(r",|\bor\b|\band\b|/", s)
    return [p.strip(" .;'\"").lower() for p in parts if p.strip(" .;'\"")]


def parse_rules_offline(text):
    """A small, predictable parser for common phrasings. Used when no AI key is set,
    and it says exactly what it understood."""
    t = " " + (text or "").lower().replace("\n", ". ") + " "
    rules, notes = {}, []
    m = re.search(r"(?:ask|check with|approval|approve|confirm)[^.]*?(?:over|above|more than|anything over)\s*" + _NUM, t)
    if m:
        rules["approval_over"] = _amount(m.group(1))
        notes.append("Ask a person over %s." % money(rules["approval_over"]))
    m = re.search(_NUM + r"\s*(?:a|per|/|each)\s*(?:day|daily)", t) or re.search(r"(?:daily|per day|a day)[^.\d$]*" + _NUM, t)
    if m:
        rules["daily_purchase_limit"] = _amount(m.group(1))
        notes.append("Daily purchase limit %s." % money(rules["daily_purchase_limit"]))
    m = re.search(r"(?:max|maximum|no more than|at most|up to|limit)\s*" + _NUM + r"\s*(?:per|a|each|for any|for one)\s*(?:purchase|order|item|buy|thing)", t) \
        or re.search(r"(?:per|each|any single|one)\s*(?:purchase|order|item)[^.\d$]*" + _NUM, t)
    if m:
        rules["max_per_purchase"] = _amount(m.group(1))
        notes.append("Max per purchase %s." % money(rules["max_per_purchase"]))
    m = re.search(r"(?:total|everything|all in|combined|tokens and purchases|ai and purchases)[^.\d$]*" + _NUM, t)
    if m:
        rules["daily_total_limit"] = _amount(m.group(1))
        notes.append("Combined daily cap %s." % money(rules["daily_total_limit"]))
    blocked = []
    for m in re.finditer(r"(?:never|don't|do not|no)\s+(?:buy|purchase|pay for|spend on)\s+([^.]+)", t):
        blocked += _words(m.group(1))
    if blocked:
        rules["blocked_categories"] = sorted(set(blocked))
        notes.append("Never buy: %s." % ", ".join(rules["blocked_categories"]))
    m = re.search(r"only\s+(?:buy|purchase|spend on|pay for)\s+([^.]+)", t)
    if m:
        rules["allowed_categories"] = _words(m.group(1))
        notes.append("Only buy: %s." % ", ".join(rules["allowed_categories"]))
    m = re.search(r"(\d+)\s*(?:purchases|orders|buys|times)\s*(?:an|a|per)\s*hour", t)
    if m:
        rules["max_purchases_per_hour"] = int(m.group(1))
        notes.append("At most %d purchases an hour." % rules["max_purchases_per_hour"])
    m = re.search(r"(?:between|from)\s*(\d{1,2})\s*(am|pm)?\s*(?:and|to|-)\s*(\d{1,2})\s*(am|pm)?", t)
    if m:
        rules["hours"] = {"start": _hour(m.group(1), m.group(2)), "end": _hour(m.group(3), m.group(4))}
        notes.append("Only between %d:00 and %d:00." % (rules["hours"]["start"], rules["hours"]["end"]))
    elif "business hours" in t or "work hours" in t:
        rules["hours"] = {"start": 9, "end": 17}
        notes.append("Only during business hours (9:00 to 17:00).")
    if re.search(r"(?:any|anything|new|unknown)\s+(?:new\s+)?(?:merchant|vendor|store|seller|site)s?", t):
        rules["new_merchant_needs_approval"] = True
        notes.append("New merchants need approval.")
    if not notes:
        notes.append("Nothing recognized. Try phrases like \"max $50 a day\", \"ask me over $20\", \"never buy gift cards\".")
    return rules, notes


RULES_PROMPT = """You turn a business owner's plain-English spending rules for an AI agent into JSON.
Return ONLY a JSON object with any of these keys (omit keys the text doesn't mention):
max_per_purchase (dollars), daily_purchase_limit (dollars), approval_over (dollars: purchases above this need a human),
daily_total_limit (dollars: AI usage + purchases combined per day), allowed_categories (list of short lowercase words),
blocked_categories (list), blocked_merchants (list), new_merchant_needs_approval (bool), hours ({"start": H, "end": H} 24h),
max_purchases_per_hour (int). Add a key "notes": a list of short plain-English lines describing each rule you set and
anything you could not map. Never invent limits the text doesn't state."""


def compile_rules(text, api_key=None, model="claude-haiku-5-5", base_url="https://api.anthropic.com", timeout=30):
    """Plain English -> validated rules. Uses Claude when a key is set, else the offline parser.
    The result is a preview: nothing is saved until a person presses Save."""
    text = (text or "").strip()
    if not text:
        raise SpendError("Type a few words about how this agent may spend.")
    if len(text) > 2000:
        raise SpendError("Keep the rules under 2,000 characters.")
    if api_key:
        body = {"model": model, "max_tokens": 600, "system": RULES_PROMPT,
                "messages": [{"role": "user", "content": text}]}
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/messages", data=json.dumps(body).encode(), method="POST")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode())
            raw = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            notes = [str(n) for n in data.pop("notes", [])][:12]
            rules = validate_rules({k: v for k, v in data.items() if k in DEFAULT_RULES})
            given = {k for k in data if k in DEFAULT_RULES}
            return {"rules": rules, "set": sorted(given), "source": "claude", "model": model, "notes": notes,
                    "plain": describe_rules(rules)}
        except (urllib.error.URLError, ValueError, SpendError, KeyError, AttributeError) as e:
            fallback_note = "Claude couldn't be used (%s), so the offline parser read your rules." % type(e).__name__
    else:
        fallback_note = "No ANTHROPIC_API_KEY set, so the offline parser read your rules."
    parsed, notes = parse_rules_offline(text)
    rules = validate_rules(parsed)
    return {"rules": rules, "set": sorted(parsed), "source": "offline", "notes": [fallback_note] + notes,
            "plain": describe_rules(rules)}


# ---- the governor for purchases ------------------------------------------------------------
class SpendGovernor:
    def __init__(self, store, paypal, money=None):
        self.store = store
        self.paypal = paypal
        self.money = money

    # -- request ------------------------------------------------------------------------
    def request(self, agent, d):
        try:
            amount = round(float(d.get("amount")), 2)
        except (TypeError, ValueError):
            raise SpendError("'amount' must be a number of dollars.")
        merchant = str(d.get("merchant") or "").strip()[:120]
        if amount <= 0 or amount > 1_000_000:
            raise SpendError("'amount' must be more than $0 and at most $1,000,000.")
        if not merchant:
            raise SpendError("'merchant' is required: who is being paid?")
        item = str(d.get("item") or "").strip()[:300]
        category = str(d.get("category") or "").strip().lower()[:60]
        reason = str(d.get("reason") or "").strip()[:1000]
        task_id = str(d.get("task_id") or "").strip()[:80] or None

        rules = agent_rules(agent)
        denied, ask, ok, flags = [], [], [], []
        known = self.store.merchant(merchant)
        if not category and known:
            category = known["category"]
        aid = agent["id"]
        today = start_of_today()

        # Hard stops (denied).
        if agent["status"] == "stopped":
            denied.append("This agent is stopped. Nothing can be bought until a person starts it again.")
        elif agent["status"] == "paused":
            denied.append("This agent is paused by the loop guard. Review it, then press Resume.")
        if merchant.lower() in rules["blocked_merchants"]:
            denied.append("%s is on this agent's blocked list." % merchant)
        if category and category in rules["blocked_categories"]:
            denied.append("'%s' is a blocked category for this agent." % category)
        if rules["allowed_categories"] and category not in rules["allowed_categories"]:
            denied.append("This agent may only buy: %s. '%s' isn't one of them."
                          % (", ".join(rules["allowed_categories"]), category or "no category"))
        if rules["hours"]:
            h = datetime.datetime.now().hour
            if not (rules["hours"]["start"] <= h < rules["hours"]["end"]):
                denied.append("Purchases are only allowed between %d:00 and %d:00." % (rules["hours"]["start"], rules["hours"]["end"]))
        per_hour = rules["max_purchases_per_hour"]
        if per_hour and self.store.purchase_count_since(aid, time.time() - 3600) >= per_hour:
            denied.append("Too many purchase requests: the limit is %d an hour. That pace looks like a runaway agent." % per_hour)
            flags.append("velocity")
        if amount > rules["max_per_purchase"]:
            denied.append("%s is over the %s per-purchase limit." % (money(amount), money(rules["max_per_purchase"])))
        bought = self.store.purchases_since(aid, today)
        if bought + amount > rules["daily_purchase_limit"]:
            denied.append("That would make %s of purchases today, over the %s daily limit."
                          % (money(bought + amount), money(rules["daily_purchase_limit"])))
        if rules["daily_total_limit"] is not None:
            tokens = self.store.spend_since(aid, today)
            if tokens + bought + amount > rules["daily_total_limit"]:
                denied.append("AI usage (%s) plus purchases (%s) plus this would pass the %s combined daily limit."
                              % (money(tokens), money(bought), money(rules["daily_total_limit"])))
        if agent["use_wallet"]:
            bal = self.store.balance(aid)
            if bal < amount:
                denied.append("The agent's PayPal-funded wallet has %s, not enough for %s." % (money(bal), money(amount)))

        # Soft stops (a person decides).
        if not denied:
            if amount > rules["approval_over"]:
                ask.append("%s is over the %s auto-approve limit." % (money(amount), money(rules["approval_over"])))
            if not known and rules["new_merchant_needs_approval"]:
                ask.append("%s isn't on your merchant list yet. New payees need a person's OK." % merchant)
                flags.append("new_merchant")
            if known and not known["paypal_email"] and self.paypal.configured:
                ask.append("%s has no PayPal payee on file, so it can't be paid automatically." % merchant)
            text = " ".join([merchant, item, reason])
            if INJECTION.search(text):
                ask.append("The request contains text that reads like instructions to the agent, a common sign of prompt injection.")
                flags.append("possible_injection")
            dup = self.store.one("SELECT id FROM spend_requests WHERE agent_id=? AND merchant=? COLLATE NOCASE AND amount=? "
                                 "AND ts>=? AND status IN ('paid','approved','pending') LIMIT 1",
                                 (aid, merchant, amount, time.time() - DUP_WINDOW))
            if dup:
                ask.append("The same purchase (%s at %s) was requested in the last 10 minutes. It may be a double charge."
                           % (money(amount), merchant))
                flags.append("duplicate")
            past = [r["amount"] for r in self.store.q("SELECT amount FROM spend_requests WHERE agent_id=? AND status='paid' "
                                                      "ORDER BY ts DESC LIMIT 50", (aid,))]
            if len(past) >= UNUSUAL_MIN_HISTORY:
                typical = statistics.median(past)
                if typical > 0 and amount > typical * UNUSUAL_MULTIPLE:
                    ask.append("%s is more than %dx this agent's typical purchase (%s)." % (money(amount), int(UNUSUAL_MULTIPLE), money(typical)))
                    flags.append("unusual_amount")
            if not ask:
                ok.append("Within the %s auto-approve limit and today's %s budget." % (money(rules["approval_over"]),
                                                                                     money(rules["daily_purchase_limit"])))
                if known:
                    ok.append("%s is a known merchant." % merchant)

        decision = "denied" if denied else ("needs_approval" if ask else "approved")
        sid = "sp_" + secrets.token_hex(8)
        row = self.store.add_spend(id=sid, agent_id=aid, task_id=task_id, amount=amount, merchant=merchant, item=item,
                                   category=category, reason=reason, decision=decision,
                                   status={"denied": "denied", "needs_approval": "pending", "approved": "approved"}[decision],
                                   reasons=denied or ask or ok, flags=flags, payee=(known or {}).get("paypal_email", ""))
        self.store.audit("spend.request", sid, {"agent_id": aid, "agent": agent["name"], "amount": amount, "merchant": merchant,
                                                "item": item, "category": category, "reason": reason, "task_id": task_id,
                                                "decision": decision, "reasons": row["reasons"], "flags": flags, "rules": rules})
        if decision == "denied":
            self.store.event(aid, "spend", "Blocked %s at %s: %s" % (money(amount), merchant, denied[0]))
        elif decision == "needs_approval":
            self.store.event(aid, "spend", "%s wants %s at %s. Waiting for your approval." % (agent["name"], money(amount), merchant))
        else:
            row = self._pay(row, agent, by="rules")
        return row

    # -- paying -------------------------------------------------------------------------
    def _pay(self, row, agent, by):
        sid, amount = row["id"], row["amount"]
        payee = row["payee"] or ((self.store.merchant(row["merchant"]) or {}).get("paypal_email") or "")
        note = "%s: %s" % (agent["name"], row["item"] or row["merchant"])
        fields = {"decided_by": by, "decided_ts": time.time(), "payee": payee}
        if self.paypal.configured:
            if not payee:
                raise SpendError("Add %s's PayPal email on the Merchants list before paying it." % row["merchant"], 409)
            try:
                out = self.paypal.create_payout(payee, amount, note, sid.replace("sp_", "LT"))
            except PayPalError as e:
                self.store.update_spend(sid, status="failed", payout_status="ERROR", note=str(e)[:300], **fields)
                self.store.audit("spend.payout_failed", sid, {"error": str(e)[:300]})
                self.store.event(agent["id"], "spend", "PayPal payout for %s at %s failed: %s" % (money(amount), row["merchant"], e))
                return self.store.spend(sid)
            bid = (out.get("batch_header") or {}).get("payout_batch_id")
            item_id, status = None, (out.get("batch_header") or {}).get("batch_status", "PENDING")
            try:
                batch = self.paypal.get_payout_batch(bid) if bid else {}
                items = batch.get("items") or []
                if items:
                    item_id, status = items[0].get("payout_item_id"), items[0].get("transaction_status", status)
            except PayPalError:
                pass  # the batch exists; its item ID can be fetched later
            fields.update(payout_batch_id=bid, payout_item_id=item_id, payout_status=status, simulated=0)
        else:
            fields.update(payout_batch_id="SIM-" + sid[3:], payout_item_id="SIMITEM-" + sid[3:], payout_status="SIMULATED", simulated=1)
        if agent["use_wallet"]:
            self.store.wallet_move(agent["id"], -amount, "purchase", ref="buy-" + sid)
        row = self.store.update_spend(sid, status="paid", **fields)
        self.store.audit("spend.paid", sid, {"by": by, "payee": payee, "payout_batch_id": fields["payout_batch_id"],
                                             "payout_status": fields["payout_status"], "simulated": bool(fields["simulated"])})
        self.store.event(agent["id"], "spend", "Paid %s to %s%s." % (money(amount), row["merchant"],
                                                                     " (simulated payout)" if fields["simulated"] else " via PayPal"))
        return row

    # -- a person decides ------------------------------------------------------------------
    def approve(self, sid, by="owner"):
        row = self.store.spend(sid)
        if not row:
            raise SpendError("No such spend request.", 404)
        if row["status"] != "pending":
            raise SpendError("This request is already %s." % row["status"], 409)
        agent = self.store.agent(row["agent_id"])
        if not agent or agent["status"] == "stopped":
            raise SpendError("The agent is stopped, so nothing can be paid for it.", 409)
        rules = agent_rules(agent)
        bought_other = self.store.purchases_since(agent["id"], start_of_today()) - row["amount"]
        if bought_other + row["amount"] > rules["daily_purchase_limit"] or row["amount"] > rules["max_per_purchase"]:
            raise SpendError("Approving this would break the agent's hard limits. Raise them first if you mean it.", 409)
        if agent["use_wallet"] and self.store.balance(agent["id"]) < row["amount"]:
            raise SpendError("The agent's wallet doesn't have enough for this. Top it up with PayPal first.", 409)
        if self.paypal.configured and not (row["payee"] or (self.store.merchant(row["merchant"]) or {}).get("paypal_email")):
            raise SpendError("Add %s's PayPal email on the Merchants list first, so Lowtides knows who to pay." % row["merchant"], 409)
        self.store.audit("spend.approved", sid, {"by": by})
        self.store.update_spend(sid, status="approved")
        return self._pay(self.store.spend(sid), agent, by=by)

    def deny(self, sid, note="", by="owner"):
        row = self.store.spend(sid)
        if not row:
            raise SpendError("No such spend request.", 404)
        if row["status"] != "pending":
            raise SpendError("This request is already %s." % row["status"], 409)
        self.store.audit("spend.denied", sid, {"by": by, "note": note[:300]})
        self.store.event(row["agent_id"], "spend", "Denied %s at %s." % (money(row["amount"]), row["merchant"]))
        return self.store.update_spend(sid, status="denied", decided_by=by, decided_ts=time.time(), note=note[:300])

    # -- kill switch -----------------------------------------------------------------------
    def kill(self, agent_id, by="owner"):
        """Stop the agent and pull back every dollar still in flight: deny pending
        purchases, void open PayPal holds, and cancel payouts nobody has claimed yet."""
        agent = self.store.agent(agent_id)
        if not agent:
            raise SpendError("No such agent.", 404)
        self.store.update_agent(agent_id, status="stopped")
        out = {"stopped": True, "denied": 0, "holds_voided": 0, "payouts_cancelled": 0, "problems": []}
        for r in self.store.spends(status="pending", agent_id=agent_id, limit=1000):
            self.store.update_spend(r["id"], status="denied", decided_by="kill switch", decided_ts=time.time(), note="Agent killed")
            self.store.audit("spend.denied", r["id"], {"by": "kill switch"})
            out["denied"] += 1
        for t in self.store.q("SELECT id FROM tasks WHERE agent_id=? AND status='open' AND hold_auth_id IS NOT NULL "
                              "AND COALESCE(paypal_status,'') NOT IN ('VOIDED','CAPTURED')", (agent_id,)):
            try:
                if self.money:
                    self.money.cancel_hold(t["id"])
                    out["holds_voided"] += 1
            except Exception as e:  # keep going: one stuck hold must not stop the rest
                out["problems"].append("Hold on task %s: %s" % (t["id"], e))
        since = time.time() - 30 * 86400
        for r in self.store.q("SELECT * FROM spend_requests WHERE agent_id=? AND status='paid' AND ts>=? "
                              "AND payout_status IN ('UNCLAIMED','PENDING','SIMULATED')", (agent_id, since)):
            try:
                if r["simulated"]:
                    pass
                elif r["payout_item_id"]:
                    self.paypal.cancel_payout_item(r["payout_item_id"])
                else:
                    continue
            except PayPalError as e:
                out["problems"].append("Payout %s couldn't be cancelled (it may already be claimed): %s" % (r["id"], e))
                continue
            self.store.update_spend(r["id"], status="cancelled", payout_status="RETURNED" if not r["simulated"] else "SIMULATED-RETURNED",
                                    note="Cancelled by the kill switch")
            if agent["use_wallet"]:
                self.store.wallet_move(agent_id, r["amount"], "refund", ref="unbuy-" + r["id"])
            self.store.audit("spend.cancelled", r["id"], {"by": "kill switch", "simulated": bool(r["simulated"])})
            out["payouts_cancelled"] += 1
        self.store.audit("agent.killed", str(agent_id), dict(out, by=by))
        self.store.event(agent_id, "kill", "KILL: %s stopped. %d pending purchases denied, %d PayPal holds voided, %d payouts cancelled."
                         % (agent["name"], out["denied"], out["holds_voided"], out["payouts_cancelled"]))
        return out

    # -- forecast ---------------------------------------------------------------------------
    def forecast(self):
        now = time.time()
        today = start_of_today()
        mins_left = max(0.0, (today + 86400 - now) / 60.0)
        rows = []
        for a in self.store.agents():
            r = agent_rules(a)
            tokens = self.store.spend_since(a["id"], today)
            bought = self.store.purchases_since(a["id"], today)
            spent = tokens + bought
            limit = r["daily_total_limit"] if r["daily_total_limit"] is not None else float(a["daily_budget"]) + r["daily_purchase_limit"]
            last_hour = self.store.spend_since(a["id"], now - 3600) + self.store.purchases_since(a["id"], now - 3600)
            rate = last_hour / 60.0  # dollars per minute at the last hour's pace
            projected = spent + rate * mins_left
            to_limit = None
            if rate > 0 and spent < limit:
                to_limit = round((limit - spent) / rate, 1)
            rows.append({"id": a["id"], "name": a["name"], "status": a["status"], "tokens_today": round(tokens, 4),
                         "purchases_today": round(bought, 2), "spent_today": round(spent, 4), "limit": round(limit, 2),
                         "rate_per_hour": round(last_hour, 4), "projected_today": round(projected, 2),
                         "minutes_to_limit": to_limit if to_limit is not None and to_limit <= mins_left else None})
        return {"agents": rows, "generated": now}

    # -- dispute evidence ---------------------------------------------------------------------
    def evidence(self, sid):
        """Everything a merchant or PayPal dispute needs: what the agent asked for and why,
        the rules in force, who approved it, the payout, and proof the log wasn't edited."""
        row = self.store.spend(sid)
        if not row:
            raise SpendError("No such spend request.", 404)
        entries = self.store.audit_entries(ref=sid)
        rules = {}
        for e in entries:
            if e["kind"] == "spend.request":
                rules = json.loads(e["data"]).get("rules", {})
        return {"lowtides_evidence_pack": 1, "generated": datetime.datetime.now().isoformat(timespec="seconds"),
                "request": row, "rules_in_force": rules, "rules_in_plain_english": describe_rules(validate_rules(rules)) if rules else [],
                "timeline": [{"ts": e["ts"], "when": datetime.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"),
                              "kind": e["kind"], "data": json.loads(e["data"]), "hash": e["hash"], "prev_hash": e["prev_hash"]}
                             for e in entries],
                "audit_chain": self.store.audit_verify()}
