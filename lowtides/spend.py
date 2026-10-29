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
import threading
import time
import urllib.error
import urllib.request

from .guard import start_of_today
from .paypal import PAYOUT_CANCELLABLE, PAYOUT_FAILED, PayPalError

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
    # The Leash: limits on how much data one agent may push through a single AI call.
    "max_reply_tokens": None,       # per-agent reply cap; None = only the global policy applies
    "max_input_tokens": None,       # largest prompt the agent may send (estimated tokens)
    "max_cost_per_call": None,      # dollars; refuse a call whose pre-flight estimate is above this
    "dump_alert_multiple": 5.0,     # flag input this many times the agent's usual size; 0/None = off
}

MONEY_FIELDS = ("max_per_purchase", "daily_purchase_limit", "approval_over", "daily_total_limit")
TOKEN_FIELDS = ("max_reply_tokens", "max_input_tokens")
MAX_TOKENS = 2_000_000
TOKENS_PER_WORD = 1.33
TOKENS_PER_PAGE = 500
SHORT_REPLY_TOKENS = 400
LIST_FIELDS = ("allowed_categories", "blocked_categories", "blocked_merchants")

# Text in a purchase that reads like orders aimed at the agent itself. Agents that
# read web pages or emails can be steered this way into paying the wrong party.
INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules)|disregard (your|the|all) (rules|instructions)"
    r"|system prompt|you are now|new instructions|developer mode|jailbreak"
    r"|(send|wire|transfer) (the )?(money|funds|payment|it) to|pay (this|the following) (account|address|email) instead"
    r"|act as (an? |the )?(admin|administrator|owner)|override (the )?(budget|limit|rules)", re.I)

DUP_WINDOW = 600          # seconds: same agent, merchant and amount = possible double charge
UNUSUAL_MULTIPLE = 3.0    # times the agent's typical purchase
EXTERNAL = "paid_by_caller"  # flag: the requesting tool settles with PayPal itself, so no payout
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
        elif k in TOKEN_FIELDS:
            if v is None:
                out[k] = None
                continue
            try:
                if isinstance(v, bool):
                    raise ValueError
                f = float(v)
                if f != int(f):
                    raise ValueError
                n = int(f)
            except (TypeError, ValueError, OverflowError):
                raise SpendError("'%s' must be a whole number of tokens, or null for no cap." % k)
            if n < 1 or n > MAX_TOKENS:
                raise SpendError("'%s' must be between 1 and 2,000,000 tokens." % k)
            out[k] = n
        elif k == "max_cost_per_call":
            if v is None:
                out[k] = None
                continue
            try:
                if isinstance(v, bool):
                    raise ValueError
                f = float(v)
            except (TypeError, ValueError):
                raise SpendError("'max_cost_per_call' must be a dollar amount, or null for no cap.")
            if not 0.0001 <= f <= 10_000:
                raise SpendError("'max_cost_per_call' must be between $0.0001 and $10,000.")
            out[k] = round(f, 4)
        elif k == "dump_alert_multiple":
            if v is None:
                out[k] = None
                continue
            try:
                if isinstance(v, bool):
                    raise ValueError
                f = float(v)
            except (TypeError, ValueError):
                raise SpendError("'dump_alert_multiple' must be a number like 5 (for 5x usual), or 0 to turn it off.")
            if f != 0 and not 1.5 <= f <= 1000:
                raise SpendError("'dump_alert_multiple' must be 0 (off) or between 1.5 and 1,000.")
            out[k] = round(f, 2)
    return out


def _cost(x):
    """Dollars, with extra places for sub-cent amounts."""
    x = float(x)
    return money(x) if x >= 0.01 and round(x, 2) == x else ("$%.4f" % x).rstrip("0")


def _num(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


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
    # .get(): callers may hand in an older, partial rules dict.
    if r.get("max_reply_tokens"):
        n = r["max_reply_tokens"]
        lines.append("Replies are capped at about {:,} tokens (~{:,} words).".format(n, int(round(n * 0.75))))
    if r.get("max_input_tokens"):
        n = r["max_input_tokens"]
        pages = n / TOKENS_PER_PAGE
        lines.append("Prompts over about {} page{} (~{:,} tokens) are blocked.".format(
            _num(pages) if pages < 10 else "{:,}".format(int(round(pages))), "" if pages == 1 else "s", n))
    if r.get("max_cost_per_call"):
        lines.append("No single AI call over %s." % _cost(r["max_cost_per_call"]))
    if r.get("dump_alert_multiple"):
        lines.append("Calls %sx bigger than usual are flagged." % _num(r["dump_alert_multiple"]))
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
    _parse_leash(t, rules, notes)
    if not notes:
        notes.append("Nothing recognized. Try phrases like \"max $50 a day\", \"ask me over $20\", \"never buy gift cards\", "
                     "\"keep replies short\", \"never send more than 50 pages\".")
    return rules, notes


_TOK = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k\b)?\s*"
_CENTS = r"(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:cents?|¢))"
_CALL = r"(?:ai\s+|model\s+|api\s+)?(?:call|request)"


def _tokens(num, k, unit):
    """'20', 'k', 'pages' -> estimated tokens."""
    n = _amount(num) * (1000 if k else 1)
    unit = unit or "tokens"
    if unit.startswith("word"):
        n *= TOKENS_PER_WORD
    elif unit.startswith("page"):
        n *= TOKENS_PER_PAGE
    return int(round(n))


def _parse_leash(t, rules, notes):
    """The Leash: reply size, prompt size, cost per call, dump alerts."""
    m = re.search(r"(?:repl(?:y|ies)|answers?|responses?|outputs?)\b[^.\d$]*?" + _TOK + r"(tokens?|words?)\b", t)
    if m:
        rules["max_reply_tokens"] = _tokens(m.group(1), m.group(2), m.group(3))
    elif re.search(r"(?:keep|make)\s+(?:the\s+|its\s+|your\s+|all\s+)?(?:repl(?:y|ies)|answers?|responses?)\s+(?:short|brief|concise)"
                   r"|\b(?:short|brief|concise)\s+(?:repl(?:y|ies)|answers|responses)", t):
        rules["max_reply_tokens"] = SHORT_REPLY_TOKENS
    if "max_reply_tokens" in rules:
        notes.append("Replies capped at about {:,} tokens.".format(rules["max_reply_tokens"]))
    m = re.search(r"(?:send|prompts?|inputs?|uploads?|feed|paste)\b[^.\d$]*?" + _TOK + r"(tokens?|words?|pages?)\b", t)
    if m:
        rules["max_input_tokens"] = _tokens(m.group(1), m.group(2), m.group(3))
        notes.append("Prompts over about {:,} tokens are blocked.".format(rules["max_input_tokens"]))
    m = re.search(r"(?:single|one|each|any|every)\s+" + _CALL + r"[^.\d$]*?" + _CENTS, t) \
        or re.search(_CENTS + r"\s*(?:per|a|each|for any|for one|for a)\s+(?:single\s+)?" + _CALL, t)
    if m:
        rules["max_cost_per_call"] = float(m.group(1)) if m.group(1) else float(m.group(2)) / 100
        notes.append("No single AI call over %s." % _cost(rules["max_cost_per_call"]))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|times)\s+(?:bigger|larger|more|the size|the usual|normal|usual)", t)
    if m:
        rules["dump_alert_multiple"] = float(m.group(1))
        notes.append("Flag calls %sx bigger than usual." % _num(rules["dump_alert_multiple"]))
    elif re.search(r"(?:no|don't|do not|turn off|disable|stop)\s+(?:the\s+)?(?:dump|big[- ]call|size)\s+(?:alerts?|flags?|warnings?)", t):
        rules["dump_alert_multiple"] = 0.0
        notes.append("Big-call alerts are off.")


RULES_PROMPT = """You turn a business owner's plain-English spending rules for an AI agent into JSON.
Return ONLY a JSON object with any of these keys (omit keys the text doesn't mention):
max_per_purchase (dollars), daily_purchase_limit (dollars), approval_over (dollars: purchases above this need a human),
daily_total_limit (dollars: AI usage + purchases combined per day), allowed_categories (list of short lowercase words),
blocked_categories (list), blocked_merchants (list), new_merchant_needs_approval (bool), hours ({"start": H, "end": H} 24h),
max_purchases_per_hour (int).
Data limits for each AI call (the "leash"): max_reply_tokens (int: longest reply; "keep replies short" = 400;
words x 1.33 = tokens), max_input_tokens (int: largest prompt it may send; one page = 500 tokens; "20k" = 20000),
max_cost_per_call (dollars: refuse any single AI call estimated above this; 25 cents = 0.25),
dump_alert_multiple (number: flag a call this many times bigger than usual, e.g. 10 for "10x"; 0 = off).
Add a key "notes": a list of short plain-English lines describing each rule you set and
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
        body = {"model": model, "max_tokens": 800, "system": RULES_PROMPT,
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
        self._decide_lock = threading.Lock()

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

        # Decide and record under one lock, so a burst of simultaneous requests can't each
        # read the same "spent so far" total and all squeeze under a limit (reserve-then-commit).
        with self._decide_lock:
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
            if str(d.get("settle") or "").lower() == "external":
                # The caller (e.g. PayPal's Agent Toolkit) moves the money itself; Lowtides only decides and records.
                flags.append(EXTERNAL)
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
    # After creating a payout, poll its batch this long (seconds between tries) for the
    # item ID and status. PayPal processes batches asynchronously; the kill switch needs
    # the item ID to cancel an UNCLAIMED payout. Whatever is still PENDING after this is
    # followed up in a background thread (background_poll) and by refresh_payout().
    poll_delays = (0.25, 0.5, 0.75, 1.0, 1.25)
    background_poll = (2, 3, 5, 10, 20, 30)

    @staticmethod
    def _refusal(e):
        hint = ""
        name = e.name or e.issue
        if name in ("AUTHORIZATION_ERROR", "NOT_AUTHORIZED", "REQUIRED_SCOPE_MISSING", "PERMISSION_DENIED"):
            hint = " Turn on Payouts for this app in the PayPal developer dashboard (Apps & Credentials > app > Features)."
        elif name in ("INSUFFICIENT_FUNDS", "NEGATIVE_BALANCE"):
            hint = " The PayPal business account doesn't have enough balance."
        elif name == "SELF_PAY_NOT_ALLOWED":
            hint = " The payee is the same PayPal account that is paying."
        elif e.status == 401 or name in ("AUTHENTICATION_FAILURE", "invalid_client"):
            hint = " Check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET."
        return ("PayPal refused the payout: %s%s" % (e.summary(), hint))[:500]

    def _pay(self, row, agent, by):
        sid, amount = row["id"], row["amount"]
        payee = row["payee"] or ((self.store.merchant(row["merchant"]) or {}).get("paypal_email") or "")
        note = "%s: %s" % (agent["name"], row["item"] or row["merchant"])
        fields = {"decided_by": by, "decided_ts": time.time(), "payee": payee}
        if EXTERNAL in (row.get("flags") or []):
            fields.update(payout_batch_id=None, payout_item_id=None, payout_status="PAID_BY_CALLER", simulated=0)
        elif self.paypal.configured:
            fields["payout_env"] = getattr(self.paypal, "env", "")
            if not payee:
                why = "Not paid: %s has no PayPal email on the Merchants list." % row["merchant"]
                return self._fail(sid, agent, row, why, fields, "NO_PAYEE")
            out = None
            for attempt in (1, 2):
                try:
                    out = self.paypal.create_payout(payee, amount, note, sid.replace("sp_", "LT"))
                    break
                except PayPalError as e:
                    # A timeout or 5xx may or may not have gone through. Retrying with the same
                    # sender_batch_id + PayPal-Request-Id is safe: PayPal pays it at most once.
                    if e.retryable and attempt == 1:
                        time.sleep(1)
                        continue
                    why = self._refusal(e)
                    if e.retryable:
                        why += " PayPal didn't answer clearly, so check the PayPal activity before retrying."
                    return self._fail(sid, agent, row, why, fields, "ERROR", error=e)
            header = out.get("batch_header") or {}
            bid = header.get("payout_batch_id")
            if not bid:
                return self._fail(sid, agent, row, "PayPal accepted the request but returned no payout batch ID.", fields, "ERROR")
            fields.update(payout_batch_id=bid, payout_item_id=None, payout_status=header.get("batch_status") or "PENDING", simulated=0)
            fields.update(self._poll(bid, self.poll_delays))
            if fields.pop("_failed", None):
                return self._fail(sid, agent, row, fields.pop("note"), fields, fields.get("payout_status") or "FAILED")
        else:
            fields.update(payout_batch_id="SIM-" + sid[3:], payout_item_id="SIMITEM-" + sid[3:], payout_status="SIMULATED", simulated=1)
        if agent["use_wallet"]:
            self.store.wallet_move(agent["id"], -amount, "purchase", ref="buy-" + sid)
        row = self.store.update_spend(sid, status="paid", **fields)
        self.store.audit("spend.paid", sid, {"by": by, "payee": payee, "payout_batch_id": fields["payout_batch_id"],
                                             "payout_item_id": fields.get("payout_item_id"), "payout_env": fields.get("payout_env", ""),
                                             "payout_status": fields["payout_status"], "simulated": bool(fields["simulated"])})
        self.store.event(agent["id"], "spend", "Paid %s to %s%s." % (
            money(amount), row["merchant"], " (simulated payout)" if fields["simulated"]
            else " (approved; the PayPal tool that asked moves the money)" if not fields["payout_batch_id"]
            else " via PayPal %s payout batch %s" % (fields.get("payout_env") or "", fields["payout_batch_id"])))
        if fields["payout_batch_id"] and not fields["simulated"] and not self._settled(fields):
            self._follow_up(sid)
        return row

    def _fail(self, sid, agent, row, why, fields, payout_status, error=None):
        """Record honestly that no money moved: status 'failed', never 'paid'."""
        fields = dict(fields, payout_status=payout_status, note=why[:500])
        self.store.update_spend(sid, status="failed", **fields)
        data = {"error": why[:500], "payout_batch_id": fields.get("payout_batch_id")}
        if error is not None:
            data.update(http=error.status, name=error.name, debug_id=error.debug_id)
        self.store.audit("spend.payout_failed", sid, data)
        self.store.event(agent["id"], "spend", "Not paid: %s at %s. %s" % (money(row["amount"]), row["merchant"], why))
        return self.store.spend(sid)

    @staticmethod
    def _settled(f):
        return bool(f.get("payout_item_id")) and f.get("payout_status") not in ("PENDING", "PROCESSING", "")

    def _poll(self, bid, delays):
        """Read the batch until the item has an ID and a real status (or we run out of tries).
        Returns spend fields to store; '_failed' + 'note' if PayPal says the money didn't go."""
        f = {}
        for d in delays:
            if d:
                time.sleep(d)
            try:
                batch = self.paypal.get_payout_batch(bid)
            except PayPalError:
                continue  # not visible yet; try again
            bstatus = (batch.get("batch_header") or {}).get("batch_status", "")
            item_id, tstatus, why = self.paypal.payout_item(batch)
            if item_id:
                f["payout_item_id"] = item_id
            f["payout_status"] = tstatus or bstatus or "PENDING"
            if tstatus in PAYOUT_FAILED or (bstatus in ("DENIED", "CANCELED") and not tstatus):
                f.update(_failed=True, note=("PayPal didn't send the payout (%s)%s" % (f["payout_status"], ": " + why if why else ".")))
                return f
            if tstatus == "UNCLAIMED":
                f["note"] = ("Sent. Waiting for the receiver to claim it%s. Cancellable until then."
                             % (" (" + why + ")" if why else ""))[:500]
            if self._settled(f):
                return f
        return f

    def _follow_up(self, sid):
        if not self.background_poll:
            return

        def run():
            for d in self.background_poll:
                time.sleep(d)
                try:
                    r = self.refresh_payout(sid)
                except Exception:
                    return
                if not r or r["status"] != "paid" or self._settled(r):
                    return
        threading.Thread(target=run, daemon=True, name="payout-" + sid).start()

    def refresh_payout(self, sid):
        """Re-read a real payout from PayPal and store its item ID / status. If PayPal says the
        money never went (FAILED, RETURNED...), the request becomes 'failed' and any wallet
        debit is given back. Safe to call on view; does nothing for simulated or settled rows."""
        row = self.store.spend(sid)
        if not row or row["simulated"] or not row["payout_batch_id"] or row["status"] != "paid":
            return row
        f = self._poll(row["payout_batch_id"], (0,))
        if not f:
            return row
        failed, note = f.pop("_failed", None), f.pop("note", None)
        if failed:
            agent = self.store.agent(row["agent_id"]) or {}
            self.store.update_spend(sid, status="failed", note=note[:500], **f)
            if agent.get("use_wallet"):
                self.store.wallet_move(row["agent_id"], row["amount"], "refund", ref="unbuy-" + sid)
            self.store.audit("spend.payout_failed", sid, {"error": note[:500], "payout_batch_id": row["payout_batch_id"],
                                                         "payout_status": f.get("payout_status")})
            self.store.event(row["agent_id"], "spend", "PayPal didn't deliver %s to %s: %s" % (money(row["amount"]), row["merchant"], note))
            return self.store.spend(sid)
        if note:
            f["note"] = note
        if f.get("payout_item_id") != row["payout_item_id"] or f.get("payout_status") != row["payout_status"]:
            self.store.audit("spend.payout_update", sid, {"payout_item_id": f.get("payout_item_id"),
                                                         "payout_status": f.get("payout_status")})
            return self.store.update_spend(sid, **f)
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
                              "AND payout_status IN ('UNCLAIMED','PENDING','PROCESSING','ONHOLD','SIMULATED')", (agent_id, since)):
            try:
                if r["simulated"]:
                    pass
                else:
                    # Get the latest item ID and status first: the batch is processed async.
                    try:
                        r = self.refresh_payout(r["id"]) or r
                    except Exception:
                        pass
                    if r["status"] != "paid":
                        continue  # PayPal already sent it back; nothing to cancel
                    if not r["payout_item_id"] or r["payout_status"] not in PAYOUT_CANCELLABLE:
                        out["problems"].append("Payout %s (batch %s) is %s at PayPal, so it can't be cancelled yet. "
                                               "PayPal only cancels UNCLAIMED payouts." % (
                                                   r["id"], r["payout_batch_id"], r["payout_status"] or "PENDING"))
                        continue
                    self.paypal.cancel_payout_item(r["payout_item_id"])
            except PayPalError as e:
                why = ("PayPal had a server error, so it's still unclaimed; unclaimed payouts return by themselves after 30 days"
                       if e.retryable else "it may already be claimed")
                out["problems"].append("Payout %s couldn't be cancelled (%s): %s" % (r["id"], why, e.summary()))
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
            tok_hour = self.store.spend_since(a["id"], now - 3600)
            buy_hour = self.store.purchases_since(a["id"], now - 3600)
            # Show the limit the agent is closest to: AI usage, purchases, or the combined cap.
            sides = [("ai_usage", tokens, float(a["daily_budget"]), tok_hour),
                     ("purchases", bought, r["daily_purchase_limit"], buy_hour)]
            if r["daily_total_limit"] is not None:
                sides.append(("combined", tokens + bought, r["daily_total_limit"], tok_hour + buy_hour))
            kind, spent, limit, last_hour = max(sides, key=lambda x: (x[1] / x[2]) if x[2] else float("inf"))
            rate = last_hour / 60.0  # dollars per minute at the last hour's pace
            projected = spent + rate * mins_left
            to_limit = None
            if rate > 0 and spent < limit:
                to_limit = round((limit - spent) / rate, 1)
            rows.append({"id": a["id"], "name": a["name"], "status": a["status"], "tokens_today": round(tokens, 4),
                         "purchases_today": round(bought, 2), "spent_today": round(spent, 4), "limit": round(limit, 2),
                         "limit_kind": kind,
                         "rate_per_hour": round(last_hour, 4), "projected_today": round(projected, 2),
                         "minutes_to_limit": to_limit if to_limit is not None and to_limit <= mins_left else None})
        return {"agents": rows, "generated": now}

    # -- dispute evidence ---------------------------------------------------------------------
    def _proof(self, row):
        if row["simulated"]:
            return {"simulated": True, "summary": row.get("payout_proof", "")}
        if not row["payout_batch_id"]:
            return {"simulated": False, "summary": row.get("note") or "No payout was created."}
        return {"simulated": False, "environment": row.get("payout_env") or getattr(self.paypal, "env", ""),
                "payout_batch_id": row["payout_batch_id"], "payout_item_id": row["payout_item_id"],
                "transaction_status": row["payout_status"], "receiver": row["payee"],
                "verify_with": "GET %s/v1/payments/payouts/%s" % (getattr(self.paypal, "base", ""), row["payout_batch_id"]),
                "summary": row.get("payout_proof", "")}

    def evidence(self, sid):
        """Everything a merchant or PayPal dispute needs: what the agent asked for and why,
        the rules in force, who approved it, the payout, and proof the log wasn't edited."""
        row = self.store.spend(sid)
        if not row:
            raise SpendError("No such spend request.", 404)
        try:
            row = self.refresh_payout(sid) or row
        except Exception:
            pass  # evidence must still download if PayPal is unreachable
        entries = self.store.audit_entries(ref=sid)
        rules = {}
        for e in entries:
            if e["kind"] == "spend.request":
                rules = json.loads(e["data"]).get("rules", {})
        return {"lowtides_evidence_pack": 1, "generated": datetime.datetime.now().isoformat(timespec="seconds"),
                "request": row, "paypal_payout": self._proof(row), "rules_in_force": rules, "rules_in_plain_english": describe_rules(validate_rules(rules)) if rules else [],
                "timeline": [{"ts": e["ts"], "when": datetime.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"),
                              "kind": e["kind"], "data": json.loads(e["data"]), "hash": e["hash"], "prev_hash": e["prev_hash"]}
                             for e in entries],
                "audit_chain": self.store.audit_verify()}
