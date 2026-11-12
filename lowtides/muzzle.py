"""Action Muzzle: the same two-sided consent check, for things an AI does that are not purchases.

A purchase goes through the Handshake (lowtides/handshake.py). Everything else an AI shopper might do for a
person goes through here: reserve, schedule, book travel, call, sign up, cancel, message, submit a form, share data.

  * The person sets a level per action type: allow, ask, or never (the "Action Pass").
  * The person gives a job in plain words. It becomes a task scope, and the rule is "do only what was asked":
    an action outside the scope has to ask, even if the level would allow it.
  * Each business publishes a signed action policy: what it accepts from AI assistants, and what needs one of its people.
  * The watchdog checks the business is real (a lookalike name with no signed policy is refused).
  * Three outcomes, as with purchases: match (done), needs_yes (held for the person), no_match (nothing happens).
  * Every action gets a signed receipt and shows up in the person's receipts.
  * A stop word freezes everything at once. Unlike Stop all, a freeze can be released.
  * Money stays on PayPal: when an action carries a deposit, fare or fee (params.amount), it is checked against the
    Pass's money limits and, only after both sides say yes, paid through the same PayPal (sandbox) path a purchase uses.

Honest limits of this build: the businesses are fictional and nothing real is contacted. A "done" action records what
would have been done and returns a demo confirmation number. Turning a job into a scope is rule-based (no AI model).
Live phone calls with mid-call checks are described in docs/ACTION-MUZZLE.md as not built.
"""
import hashlib
import json
import os
import re
import secrets
import time

from . import passport, trust
from .handshake import HandshakeError, _canon, money, service, sign_doc
from .spend import SpendError

SCHEMA = """
CREATE TABLE IF NOT EXISTS mz_rules (agent_id INTEGER NOT NULL, type TEXT NOT NULL, level TEXT NOT NULL, PRIMARY KEY (agent_id, type));
CREATE TABLE IF NOT EXISTS mz_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mz_actions (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, agent_id INTEGER NOT NULL, type TEXT NOT NULL, cp TEXT NOT NULL,
  summary TEXT NOT NULL, signal TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mz_actions_ts ON mz_actions(ts);
"""
TABLES = ("mz_rules", "mz_state", "mz_actions")
LEVELS = ("allow", "ask", "never")
MAX_ACTIONS = 400           # rows kept per sandbox
PER_HOUR = 30               # more than this many actions in an hour and each new one has to ask
DEFAULT_STOP_WORD = "red light"

# type -> (label, default level, can it be undone, short verb for summaries)
TYPES = {
    "reserve": ("Reserve a table, room or seat", "allow", True, "Reserve"),
    "schedule": ("Schedule an appointment", "allow", True, "Schedule"),
    "book_travel": ("Book travel", "ask", True, "Book travel"),
    "call": ("Make a phone call for you", "ask", True, "Call"),
    "sign_up": ("Sign you up or subscribe", "ask", True, "Sign up"),
    "cancel": ("Cancel a booking or subscription", "ask", False, "Cancel"),
    "message": ("Send a message in your name", "ask", False, "Message"),
    "submit_form": ("Fill in and send a form", "ask", True, "Send a form"),
    "share_data": ("Share your personal details", "ask", False, "Share details"),
}
# Details no AI may hand over, whatever the level says.
NEVER_SHARE = {"ssn": "a Social Security number", "social security number": "a Social Security number", "password": "a password",
               "card number": "a card number", "bank account": "a bank account number"}

# Fictional businesses. `policy` None means the business published no signed action policy.
COUNTERPARTIES = {
    "harbor-bistro": {"name": "Harbor Bistro", "kind": "restaurant", "domain": "harborbistro.example", "code": "HB",
                      "policy": {"accepts": ["reserve", "cancel", "message", "sign_up"], "needs_person": ["cancel"], "must_say_ai": True, "min_trust": 40}},
    "tidewater-clinic": {"name": "Tidewater Clinic", "kind": "clinic", "domain": "tidewaterclinic.example", "code": "TC",
                         "policy": {"accepts": ["schedule", "cancel", "submit_form"], "needs_person": ["submit_form"], "must_say_ai": True, "min_trust": 50}},
    "coastline-mobile": {"name": "Coastline Mobile", "kind": "phone company", "domain": "coastlinemobile.example", "code": "CM",
                         "policy": {"accepts": ["call", "message"], "needs_person": [], "must_say_ai": True, "min_trust": 40}},
    "skyline-air": {"name": "Skyline Air", "kind": "airline", "domain": "skylineair.example", "code": "SA",
                    "policy": {"accepts": ["book_travel", "cancel"], "needs_person": ["cancel"], "must_say_ai": True, "min_trust": 50}},
    "coastline-moblie-help": {"name": "Coastline Moblie Support", "kind": "phone company", "domain": "coastline-moblie-help.example", "code": "XX",
                              "policy": None},
}
SAMPLES = (
    {"label": "Book the table I asked for", "action_type": "reserve", "counterparty": "harbor-bistro",
     "params": {"party": 2, "when": "Friday 7:00 pm", "amount": 20, "amount_for": "deposit"}, "summary": "Table for two, Friday 7:00 pm, $20 deposit",
     "hint": "the job itself, deposit by PayPal"},
    {"label": "Also join their mailing list", "action_type": "sign_up", "counterparty": "harbor-bistro",
     "params": {"list": "newsletter"}, "summary": "Join the Harbor Bistro mailing list", "hint": "not what you asked"},
    {"label": "Give them my Social Security number", "action_type": "share_data", "counterparty": "harbor-bistro",
     "params": {"data": ["ssn"]}, "summary": "Share my Social Security number", "hint": "never allowed"},
    {"label": "Call the phone company about my bill", "action_type": "call", "counterparty": "coastline-mobile",
     "params": {"topic": "ask about the outage credit", "may_agree_to": "nothing new"}, "summary": "Call about the outage credit", "hint": "a different job"},
    {"label": "Call this other \"support\" number", "action_type": "call", "counterparty": "coastline-moblie-help",
     "params": {"topic": "ask about the outage credit"}, "summary": "Call the number from a text message", "hint": "a lookalike"},
    {"label": "Book a $189 flight to Denver", "action_type": "book_travel", "counterparty": "skyline-air",
     "params": {"to": "Denver", "when": "next Friday", "refundable": True, "amount": 189, "amount_for": "fare"}, "summary": "One seat to Denver, refundable, $189",
     "hint": "over your ask-me line"},
    {"label": "Book a check-up at the clinic", "action_type": "schedule", "counterparty": "tidewater-clinic",
     "params": {"when": "next Tuesday morning"}, "summary": "Check-up, next Tuesday morning", "hint": "allowed, but is it the job?"},
)
_WORDS = (
    ("reserve", r"\b(reserv\w*|book (a |the )?(table|room|seat)|table for|a table)\b"),
    ("schedule", r"\b(appointment|schedul\w*|check-?up|haircut|visit)\b"),
    ("book_travel", r"\b(flight|fly|plane|train|hotel|trip)\b"),
    ("call", r"\b(call|phone|ring)\b"),
    ("sign_up", r"\b(sign (me )?up|subscribe|join|mailing list)\b"),
    ("cancel", r"\bcancel\w*\b"),
    ("message", r"\b(email|e-mail|text|message|write to)\b"),
    ("submit_form", r"\b(form|apply|application)\b"),
    ("share_data", r"\b(share|give them my|send them my)\b"),
)


def _ensure(app):
    if not getattr(app, "_muzzle_ready", False):
        with app.store.lock:
            app.store.db.executescript(SCHEMA)
            app.store.db.commit()
        app._muzzle_ready = True


def reset(app):
    _ensure(app)
    with app.store.lock:
        for t in TABLES:
            app.store.db.execute("DELETE FROM " + t)
        app.store.db.commit()


def _get(app, k, default=None):
    row = app.store.one("SELECT v FROM mz_state WHERE k=?", (k,))
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except ValueError:
        return default


def _set(app, k, v):
    app.store.run("INSERT OR REPLACE INTO mz_state(k, v) VALUES(?, ?)", (k, json.dumps(v)))


def _norm(t):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(t or "").lower()).split())


# ---- the person's side -----------------------------------------------------------------------
def levels(app, agent_id):
    _ensure(app)
    out = {t: v[1] for t, v in TYPES.items()}
    for r in app.store.q("SELECT type, level FROM mz_rules WHERE agent_id=?", (agent_id,)):
        if r["type"] in out and r["level"] in LEVELS:
            out[r["type"]] = r["level"]
    return out


def set_rules(app, agent, d):
    _ensure(app)
    if not isinstance(d, dict):
        raise HandshakeError("Send JSON like {\"levels\": {\"reserve\": \"allow\"}}.")
    lv = d.get("levels")
    if lv is not None:
        if not isinstance(lv, dict):
            raise HandshakeError("'levels' must look like {\"reserve\": \"allow\"}.")
        for t, level in lv.items():
            if t not in TYPES or level not in LEVELS:
                raise HandshakeError("Each level must be allow, ask or never, for a known kind of action.")
        for t, level in lv.items():
            app.store.run("INSERT OR REPLACE INTO mz_rules(agent_id, type, level) VALUES(?,?,?)", (agent["id"], t, level))
        app.store.audit("action.rules", str(agent["id"]), {"levels": lv})
    if "stop_word" in d:
        w = _norm(d.get("stop_word"))
        if not (3 <= len(w) <= 40) or len(w.split()) > 4:
            raise HandshakeError("Pick a stop word or short phrase of 3 to 40 letters, like \"red light\".")
        if w in ("stop", "no", "yes", "help", "next", "more"):
            raise HandshakeError("Pick something you wouldn't say by accident. \"%s\" is too common." % w)
        _set(app, "stop_word", w)


def stop_word(app):
    _ensure(app)
    return _get(app, "stop_word", DEFAULT_STOP_WORD)


def is_stop_word(app, text):
    """True when what the person typed or said is their stop word (alone, or inside a short sentence)."""
    w, t = stop_word(app), _norm(text)
    if not w or not t:
        return False
    return t == w or (len(t.split()) <= 8 and re.search(r"\b" + re.escape(w) + r"\b", t) is not None)


def scope_of(text):
    """Turn a plain-English job into a task scope. Rule-based: no AI model reads it."""
    t = _norm(text)
    types = [name for name, pat in _WORDS if re.search(pat, t)]
    cp = None
    for cid, c in COUNTERPARTIES.items():
        if c["policy"] is None:
            continue
        name = _norm(c["name"])
        if name in t or name.split()[0] in t.split() or (c["kind"] in t and c["kind"] not in ("phone company",)) or (c["kind"] == "phone company" and "phone company" in t):
            cp = cid
            break
    if "call" in types and cp and COUNTERPARTIES[cp]["kind"] != "phone company" and len(types) > 1 and not re.search(r"\b(call|phone)\b", t):
        types.remove("call")
    if not types:
        raise HandshakeError("I couldn't tell what job that is. Try: book a table for two Friday at 7 at Harbor Bistro.")
    verb = TYPES[types[0]][3]
    summary = verb + ((" at " if types[0] in ("reserve", "schedule") else " with ") + COUNTERPARTIES[cp]["name"] if cp else "")
    return {"action_types": types, "counterparty": cp, "summary": summary}


def set_task(app, agent, text):
    _ensure(app)
    text = " ".join(str(text or "").split())[:300]
    if len(text) < 4:
        raise HandshakeError("Say the job in a few words, like: book a table for two Friday at 7 at Harbor Bistro.")
    task = {"text": text, "scope": scope_of(text), "ts": time.time()}
    _set(app, "task:%d" % agent["id"], task)
    app.store.audit("action.task", str(agent["id"]), {"text": text, "scope": task["scope"]})
    return {"task": task, "said": "Got it. The job: %s. Anything outside that, %s has to ask you first." % (task["scope"]["summary"], agent["name"])}


def task_of(app, agent_id):
    return _get(app, "task:%d" % agent_id)


# ---- the business's side ---------------------------------------------------------------------
def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def legit(cid):
    """The watchdog's check of a business: a signed policy, and a name that is not a near copy of another's."""
    c = COUNTERPARTIES[cid]
    mine = _norm(c["name"]).split()
    for oid, o in COUNTERPARTIES.items():
        if oid == cid or o["policy"] is None:
            continue
        theirs = _norm(o["name"]).split()
        near = [1 for a in mine for b in theirs if a != b and len(a) > 4 and _lev(a, b) <= 2]
        same = [1 for a in mine for b in theirs if a == b]
        if near and same:
            return {"verdict": "danger", "reason": "The name is a near copy of %s, with letters swapped%s." % (
                o["name"], "" if c["policy"] else ", and it has published no signed policy")}
    if c["policy"] is None:
        return {"verdict": "danger", "reason": "%s has published no signed policy for AI assistants." % c["name"]}
    return {"verdict": "safe", "reason": "A demo business with a signed policy for AI assistants."}


def policy_doc(app, cid):
    """The business's signed action policy (the counterpart of a Store Policy), or None."""
    c = COUNTERPARTIES[cid]
    if c["policy"] is None:
        return None
    doc = sign_doc(service(app).book, dict(c["policy"], type="lowtides.action_policy", v=1, id=cid, name=c["name"], domain=c["domain"], demo=True))
    doc["digest"] = hashlib.sha256(_canon({k: v for k, v in doc.items() if k not in ("signature", "digest")})).hexdigest()[:16]
    return doc


def counterparty_view(app, cid):
    c = COUNTERPARTIES[cid]
    p = policy_doc(app, cid)
    return {"id": cid, "name": c["name"], "kind": c["kind"], "domain": c["domain"], "demo": True, "check": legit(cid),
            "policy": None if p is None else {k: p[k] for k in ("accepts", "needs_person", "must_say_ai", "min_trust", "digest")}}


# ---- money: a deposit, fare or fee is paid through PayPal, never around it --------------------
CATEGORY = {"reserve": "dining", "schedule": "services", "book_travel": "travel", "call": "services", "sign_up": "subscriptions",
            "cancel": "services", "message": "services", "submit_form": "services", "share_data": "services"}


def _amount(params):
    a = params.get("amount")
    if a is None:
        return 0.0
    if isinstance(a, bool) or not isinstance(a, (int, float)) or not (0 < a <= 100000):
        raise HandshakeError("'amount' must be a number of dollars above 0.")
    return round(float(a), 2)


def _pay(app, agent, cid, typ, amount, summary):
    """Ask the ordinary purchase rules to pay this through PayPal (sandbox). Returns the spend request."""
    c = COUNTERPARTIES[cid]
    known = app.store.merchant(c["name"])
    if not known or not known.get("paypal_email"):
        app.store.add_merchant(c["name"], os.environ.get("LOWTIDES_DEMO_PAYEE", "merchant-demo@example.com"), CATEGORY[typ])
    return app.spend.request(agent, {"amount": amount, "merchant": c["name"], "item": summary, "category": CATEGORY[typ],
                                     "reason": "Action Muzzle: %s with %s (%s)" % (TYPES[typ][3].lower(), c["name"], c["domain"])})


def _payment(app, spend_id):
    row = app.store.spend(spend_id)
    return service(app)._spend_order(row) if row else None


def _paid_words(pay, amount, what):
    how = "a simulated payment (nothing was sent to PayPal)" if pay.get("simulated") else "PayPal sandbox"
    return "The %s %s was paid through %s." % (money(amount), what, how)


# ---- the check -------------------------------------------------------------------------------
def frozen(app):
    _ensure(app)
    return bool(_get(app, "frozen", {}).get("on"))


def _row(app, r):
    d = json.loads(r["data"])
    c = COUNTERPARTIES.get(r["cp"], {"name": r["cp"]})
    return {"id": r["id"], "ts": r["ts"], "agent": d.get("agent", ""), "amount": d.get("amount") or 0, "action_type": r["type"], "label": TYPES.get(r["type"], (r["type"],))[0],
            "counterparty": {"id": r["cp"], "name": c["name"]}, "params": d.get("params") or {}, "summary": r["summary"],
            "signal": r["signal"], "status": r["status"], "headline": d.get("headline", ""), "checks": d.get("checks") or [],
            "result": d.get("result") or {}, "simulated": True, "receipt_url": "/api/muzzle/receipt/" + r["id"]}


def get(app, aid):
    _ensure(app)
    r = app.store.one("SELECT * FROM mz_actions WHERE id=?", (str(aid),))
    if not r:
        raise HandshakeError("No such action.", 404)
    return r


def recent(app, agent_id=None, limit=20):
    _ensure(app)
    sql = "SELECT * FROM mz_actions" + (" WHERE agent_id=?" if agent_id else "") + " ORDER BY ts DESC LIMIT ?"
    return [_row(app, r) for r in app.store.q(sql, ((agent_id, limit) if agent_id else (limit,)))]


def _sign_receipt(app, row_id, body):
    return sign_doc(service(app).book, dict(body, type="lowtides.action_receipt", v=1, id=row_id, simulated=True,
                                           note="Demo: the business is fictional and nothing real was contacted."))


def _confirmation(cid, row_id):
    return "%s-%04d" % (COUNTERPARTIES[cid]["code"], int(hashlib.sha256(row_id.encode()).hexdigest()[:6], 16) % 10000)


def request(app, agent, d, issuer):
    """An AI asks before it acts. Returns the action with its checks, outcome and signed receipt."""
    _ensure(app)
    if (not isinstance(d, dict) or not isinstance(d.get("action_type"), str) or not isinstance(d.get("counterparty"), str)
            or d["action_type"] not in TYPES or d["counterparty"] not in COUNTERPARTIES):
        raise HandshakeError("Send JSON like {\"action_type\": \"reserve\", \"counterparty\": \"harbor-bistro\", \"summary\": \"Table for two, Friday 7 pm\"}. "
                             "Kinds: %s. Businesses: %s." % (", ".join(TYPES), ", ".join(COUNTERPARTIES)))
    typ, cid = d["action_type"], d["counterparty"]
    params = d.get("params") if isinstance(d.get("params"), dict) else {}
    if len(json.dumps(params)) > 2000:
        raise HandshakeError("'params' is too large.")
    label, _, undoable, verb = TYPES[typ]
    c = COUNTERPARTIES[cid]
    amount = _amount(params)
    what = str(params.get("amount_for") or "payment")[:20] if amount else ""
    summary = " ".join(str(d.get("summary") or "").split())[:160] or "%s with %s" % (verb, c["name"])
    svc = service(app)
    checks = []

    def add(side, name, level, detail):
        checks.append({"side": side, "name": name, "level": level, "detail": detail})

    # --- your side
    if frozen(app):
        add("you", "Freeze", "fail", "Frozen. Release the freeze to let %s act again." % agent["name"])
    elif agent["status"] != "on":
        add("you", "Your AI's Pass", "fail", "%s is stopped, so its Pass is revoked." % agent["name"])
    else:
        st = svc.book.current(agent, issuer)
        if st and st.get("valid"):
            add("you", "Your AI's Pass", "pass", "%s carries a valid signed Pass." % agent["name"])
        else:
            add("you", "Your AI's Pass", "fail", "%s has no valid Pass right now." % agent["name"])
    mode = svc.prefs(agent).get("mode", "assist")
    if mode == "off":
        add("you", "Mode", "fail", "Mode is Off, so %s does nothing." % agent["name"])
    elif mode == "assist":
        add("you", "Mode", "ask", "Mode is AI Assist, so %s asks you before every action." % agent["name"])
    else:
        add("you", "Mode", "pass", "Mode is Full Autopilot.")
    level = levels(app, agent["id"])[typ]
    rule = "Your rule: %s" % label.lower()
    if level == "never":
        add("you", rule, "fail", "You set this to Never.")
    elif level == "ask":
        add("you", rule, "ask", "You set this to Ask me first%s." % ("" if undoable else " (it can't be undone)"))
    else:
        add("you", rule, "pass", "You allow this without asking.")
    if typ == "share_data":
        asked = [_norm(x) for x in (params.get("data") if isinstance(params.get("data"), list) else [])]
        bad = [NEVER_SHARE[x] for x in asked if x in NEVER_SHARE]
        if bad:
            add("you", "Private details", "fail", "Lowtides never lets an AI share %s." % bad[0])
    task = task_of(app, agent["id"])
    if not task:
        add("you", "The job you gave", "ask", "You haven't given a job, so %s has to ask first." % agent["name"])
    else:
        sc = task["scope"]
        inside = typ in sc["action_types"] and (not sc.get("counterparty") or sc["counterparty"] == cid)
        if inside:
            add("you", "The job you gave", "pass", "Inside the job: %s." % sc["summary"])
        else:
            add("you", "The job you gave", "ask", "Outside the job you gave (%s). Do only what was asked." % sc["summary"])
    if amount:                                  # the Pass's money limits apply to a deposit or fare exactly as to a purchase
        pf = svc.prefs(agent)
        if amount > float(pf.get("per_item_cap") or 0):
            add("you", "Your money limits", "fail", "%s is over your %s per-item limit." % (money(amount), money(pf.get("per_item_cap") or 0)))
        elif amount > float(pf.get("ask_above") or 0):
            add("you", "Your money limits", "ask", "%s is over your %s ask-me line, so you decide." % (money(amount), money(pf.get("ask_above") or 0)))
        else:
            add("you", "Your money limits", "pass", "%s is inside your limits." % money(amount))
    hour = app.store.one("SELECT COUNT(*) AS n FROM mz_actions WHERE agent_id=? AND ts>?", (agent["id"], time.time() - 3600))["n"]
    if hour >= PER_HOUR:
        add("you", "Too many at once", "ask", "%d actions in the last hour, so this one waits for you." % hour)
    # --- the watchdog
    lg = legit(cid)
    add("guard", "Is this business real?", "pass" if lg["verdict"] == "safe" else "fail", lg["reason"])
    # --- their side
    pol = policy_doc(app, cid)
    if pol is None:
        add("them", "Their policy for AI assistants", "fail", "%s has not said yes to AI assistants at all." % c["name"])
    else:
        if typ in pol["accepts"]:
            add("them", "Takes this from AI assistants", "pass", "%s accepts this kind of request from an AI with a valid Pass." % c["name"])
        else:
            add("them", "Takes this from AI assistants", "fail", "%s does not take this kind of request from AI assistants." % c["name"])
        if typ in pol["needs_person"]:
            add("them", "One of their people", "ask", "%s has a person confirm this kind of request." % c["name"])
        score = trust.compute(app.store, agent["id"])["score"]
        if score >= pol["min_trust"]:
            add("them", "Trust in your AI", "pass", "%s has a trust score of %d; %s asks for %d." % (agent["name"], score, c["name"], pol["min_trust"]))
        else:
            add("them", "Trust in your AI", "fail", "%s has a trust score of %d; %s asks for %d." % (agent["name"], score, c["name"], pol["min_trust"]))
        if pol.get("must_say_ai"):
            add("them", "Says it is an AI", "pass", "%s must say it is an AI. It does." % agent["name"])
        if amount:
            add("them", "How they are paid", "pass", "%s takes the %s through PayPal." % (c["name"], what))

    fails = [x for x in checks if x["level"] == "fail"]
    asks = [x for x in checks if x["level"] == "ask"]
    row_id = "act_" + secrets.token_hex(6)
    result, spend_id, paid = {}, None, None
    if amount and not fails and not asks:       # both said yes: now, and only now, the money moves, through PayPal
        try:
            sp = _pay(app, agent, cid, typ, amount, summary)
            if sp["decision"] == "denied" or sp["status"] == "failed":
                add("you", "Lowtides money rules", "fail", (sp.get("reasons") or [sp.get("note") or "The payment was refused."])[0])
            elif sp["status"] == "pending":
                spend_id = sp["id"]
                add("you", "Lowtides money rules", "ask", "; ".join(sp.get("reasons") or ["This payment waits for you."]))
            else:
                spend_id, paid = sp["id"], _payment(app, sp["id"])
        except SpendError as e:
            add("you", "Lowtides money rules", "fail", str(e))
        fails = [x for x in checks if x["level"] == "fail"]
        asks = [x for x in checks if x["level"] == "ask"]
    if fails:
        signal, status, headline = "no_match", "blocked", "No. Nothing happened: " + fails[0]["detail"]
    elif asks:
        signal, status, headline = "needs_yes", "waiting", "Needs your yes: " + asks[0]["detail"]
    else:
        signal, status = "match", "done"
        result = {"confirmation": _confirmation(cid, row_id)}
        headline = "Both said yes. Done."
        if paid:
            result["payment"] = paid
            headline = "Both said yes. Done. " + _paid_words(paid, amount, what)
    body = {"ts": time.time(), "agent": agent["name"], "action_type": typ, "counterparty": {"id": cid, "name": c["name"], "domain": c["domain"]},
            "params": params, "summary": summary, "task_scope": (task or {}).get("scope"), "mode": mode,
            "policy_digest": (pol or {}).get("digest"), "checks": checks, "signal": signal, "status": status, "result": result}
    body["amount"] = amount or None
    data = {"agent": agent["name"], "params": params, "headline": headline, "checks": checks, "result": result,
            "amount": amount, "amount_for": what, "spend_id": spend_id, "receipt": _sign_receipt(app, row_id, body)}
    app.store.run("INSERT INTO mz_actions(id, ts, agent_id, type, cp, summary, signal, status, data) VALUES(?,?,?,?,?,?,?,?,?)",
                  (row_id, body["ts"], agent["id"], typ, cid, summary, signal, status, json.dumps(data)))
    app.store.run("DELETE FROM mz_actions WHERE id NOT IN (SELECT id FROM mz_actions ORDER BY ts DESC LIMIT ?)", (MAX_ACTIONS,))
    app.store.audit("action." + status, row_id, {"agent_id": agent["id"], "type": typ, "counterparty": cid, "signal": signal})
    return _row(app, get(app, row_id))


def decide(app, aid, approve, issuer):
    """The person's one-tap yes or no on a waiting action. One at a time, so one yes can never pay twice."""
    with service(app).lock:
        return _decide(app, aid, approve, issuer)


def _decide(app, aid, approve, issuer):
    if not isinstance(aid, str):
        raise HandshakeError("Send JSON like {\"id\": \"act_...\", \"approve\": true}.")
    r = get(app, aid)
    if r["status"] != "waiting":
        raise HandshakeError("That one is not waiting for an answer anymore.", 409)
    agent = app.store.agent(r["agent_id"])
    d = json.loads(r["data"])
    sp = app.store.spend(d["spend_id"]) if d.get("spend_id") else None
    already_paid = bool(sp and sp["status"] in ("paid",))
    if sp and sp["status"] not in ("pending", "paid"):      # denied, failed or canceled somewhere else: start the payment over
        d["spend_id"], sp = None, None
    if approve:
        if frozen(app):
            raise HandshakeError("Everything is frozen. Release the freeze first, then say yes.", 409)
        if not agent or agent["status"] != "on":
            raise HandshakeError("That AI shopper is stopped. Turn it back on first.", 409)
        hard = [x for x in d["checks"] if x["level"] == "fail"]
        if hard or legit(r["cp"])["verdict"] != "safe":
            raise HandshakeError("That can't be approved: a rule or the watchdog says no.", 409)
        d["result"] = {"confirmation": _confirmation(r["cp"], r["id"])}
        d["headline"] = "You said yes. Done."
        if d.get("amount"):                         # the person's yes is what lets the money move, through PayPal
            try:
                if already_paid:
                    pass                            # approved from the approvals list already: do not pay again
                elif sp:
                    app.spend.approve(d["spend_id"])
                else:
                    new = _pay(app, agent, r["cp"], r["type"], d["amount"], r["summary"])
                    d["spend_id"] = new["id"]
                    if new["decision"] == "denied" or new["status"] == "failed":
                        raise HandshakeError((new.get("reasons") or [new.get("note") or "The payment was refused."])[0], 409)
                    if new["status"] == "pending":
                        app.spend.approve(new["id"])
            except SpendError as e:
                raise HandshakeError(str(e), 409)
            pay = _payment(app, d["spend_id"])
            if not pay:
                raise HandshakeError("The payment did not go through, so nothing was booked.", 409)
            d["result"]["payment"] = pay
            d["headline"] = "You said yes. Done. " + _paid_words(pay, d["amount"], d.get("amount_for") or "payment")
        status, signal = "done", "match"
    elif already_paid:
        raise HandshakeError("This was already paid from your approvals list, so it can't be declined here. Say yes to finish it.", 409)
    else:
        status, signal = "denied", "no_match"
        d["headline"] = "You said no. Nothing happened."
        if sp:
            try:
                app.spend.deny(d["spend_id"], note="Declined in Action Muzzle")
            except SpendError:
                pass
    old = d["receipt"]
    body = {k: v for k, v in old.items() if k not in ("signature", "kid", "alg", "type", "v", "id", "simulated", "note")}
    body.update({"signal": signal, "status": status, "result": d["result"], "decided_by": "the person", "decided_ts": time.time()})
    d["receipt"] = _sign_receipt(app, r["id"], body)
    app.store.run("UPDATE mz_actions SET status=?, signal=?, data=? WHERE id=?", (status, signal, json.dumps(d), r["id"]))
    app.store.audit("action." + ("approved" if approve else "denied"), r["id"], {"agent_id": r["agent_id"]})
    return _row(app, get(app, r["id"]))


# ---- stop word and freeze --------------------------------------------------------------------
def freeze(app, how="button"):
    """Hold every AI shopper's Pass (every store and business then says no) until the person releases it."""
    _ensure(app)
    if frozen(app):
        return {"frozen": True, "held": _waiting(app), "said": "Already frozen. Press Release the freeze when you're ready."}
    book, ids = service(app).book, []
    for a in app.store.agents():
        if a["status"] == "on" and not book.held(a["id"]):
            book.revoke(a["id"], by="owner", reason="Frozen by you. Release the freeze to continue.")
            ids.append(a["id"])
    _set(app, "frozen", {"on": True, "ts": time.time(), "agents": ids, "how": how})     # ts is after the holds above
    app.store.audit("action.freeze", "all", {"agents": ids, "how": how})
    held = _waiting(app)
    return {"frozen": True, "held": held,
            "said": "Frozen. Every AI shopper is on hold at every store and business%s. Nothing was canceled. Press Release the freeze when you're ready."
                    % ("" if not held else ", and %d waiting job%s held" % (held, " is" if held == 1 else "s are"))}


def release(app):
    _ensure(app)
    st = _get(app, "frozen", {})
    if not st.get("on"):
        return {"frozen": False, "said": "Nothing is frozen."}
    for aid in st.get("agents") or []:
        app.store.run("DELETE FROM pass_holds WHERE agent_id=? AND since<=?", (aid, float(st.get("ts") or 0) + 1.0))
    _set(app, "frozen", {"on": False, "ts": time.time()})
    app.store.audit("action.release", "all", {"agents": st.get("agents") or []})
    return {"frozen": False, "said": "Released. Your AI shoppers can work again, and waiting jobs are still waiting for your yes."}


def _waiting(app):
    return app.store.one("SELECT COUNT(*) AS n FROM mz_actions WHERE status='waiting'")["n"]


# ---- views -----------------------------------------------------------------------------------
def state(app, agent):
    _ensure(app)
    lv = levels(app, agent["id"])
    t = task_of(app, agent["id"])
    return {"agent": {"id": agent["id"], "name": agent["name"]}, "mode": service(app).prefs(agent).get("mode", "assist"),
            "frozen": frozen(app), "stop_word": stop_word(app),
            "types": [{"type": k, "label": v[0], "level": lv[k], "undoable": v[2]} for k, v in TYPES.items()],
            "task": None if not t else {"text": t["text"], "scope": t["scope"]},
            "counterparties": [counterparty_view(app, cid) for cid in COUNTERPARTIES],
            "samples": [dict(s) for s in SAMPLES], "actions": recent(app, agent["id"]),
            "note": "Demo: the businesses are fictional and nothing real is contacted. Turning a job into a scope is rule-based."}


def receipts(app, agent_id=None):
    """Action receipts in the shape lowtides/extras.py lists receipts."""
    out = []
    for a in recent(app, agent_id, limit=200):
        status = {"done": "done", "waiting": "waiting", "frozen": "waiting"}.get(a["status"], "blocked")
        pay = (a["result"] or {}).get("payment")
        out.append({"id": a["id"], "kind": "action", "ts": a["ts"], "store": a["counterparty"]["name"],
                    "title": "%s: %s" % (TYPES[a["action_type"]][3], a["summary"]), "amount": float(a["amount"]) if pay else 0.0,
                    "category": a["action_type"], "status": status, "signal": a["signal"], "shopper": a["agent"],
                    "payment": None if not pay else ("Simulated payment" if pay.get("simulated") else "PayPal sandbox %s" % (pay.get("paypal_order_id") or "")),
                    "signed": True,
                    "proof_url": a["receipt_url"], "note": a["headline"]})
    return out


def agent_result(a):
    nxt = {"match": "Done. Carry on with the job.", "needs_yes": "Held for the person. Do not do it another way. Check again with GET /v1/action/%s." % a["id"],
           "no_match": "Do not do this. Tell the person why."}[a["signal"]]
    return {"id": a["id"], "signal": a["signal"], "status": a["status"], "headline": a["headline"], "checks": a["checks"],
            "result": a["result"], "simulated": True, "next": nxt,
            "note": "The business is fictional and nothing real is contacted. A payment, when there is one, is a PayPal sandbox payout (or labeled simulated)."}


# ---- HTTP ------------------------------------------------------------------------------------
def route(h, app, method, path):
    """/api/muzzle* for the person's pages, /v1/action for agents. Returns True when it answered."""
    mv = re.fullmatch(r"/v1/action(?:/(act_[0-9a-f]+))?", path)
    if mv:
        if (method == "POST" and mv.group(1)) or (method == "GET" and not mv.group(1)) or method not in ("GET", "POST"):
            return False
        agent = h._agent_or_401()
        if not agent:
            return True
        try:
            if method == "GET":
                r = get(app, mv.group(1))
                if r["agent_id"] != agent["id"]:
                    raise HandshakeError("No such action for this agent.", 404)
                h._send(200, agent_result(_row(app, r)))
            else:
                h._send(200, agent_result(request(app, agent, h._json_in(), passport.issuer_url(h, app))))
        except HandshakeError as e:
            h._send(e.http, {"error": str(e)})
        except passport.PassError as e:
            h._send(getattr(e, "http", 400), {"error": str(e)})
        return True
    mr = re.fullmatch(r"/api/muzzle/receipt/(act_[0-9a-f]+)", path)
    mine = (("GET", "/api/muzzle"), ("POST", "/api/muzzle/task"), ("POST", "/api/muzzle/rules"), ("POST", "/api/muzzle/action"),
            ("POST", "/api/muzzle/decide"), ("POST", "/api/muzzle/freeze"), ("POST", "/api/muzzle/release"))
    if not ((method, path) in mine or (mr and method == "GET")):
        return False
    if not h._api_allowed(write=method != "GET"):
        return True
    try:
        svc = service(app)
        issuer = passport.issuer_url(h, app)
        svc.ensure_scout(issuer)
        if mr:
            h._send(200, json.loads(get(app, mr.group(1))["data"])["receipt"], {"Cache-Control": "no-store"})
            return True
        if method == "GET":
            qs = h._path()[1]
            h._send(200, state(app, svc.resolve_agent(qs.get("agent_id") or None)), {"Cache-Control": "no-store"})
            return True
        d = h._json_in()
        d = d if isinstance(d, dict) else {}
        agent = svc.resolve_agent(d.get("agent_id"))
        if path == "/api/muzzle/task":
            h._send(200, set_task(app, agent, d.get("text")))
        elif path == "/api/muzzle/rules":
            set_rules(app, agent, d)
            h._send(200, state(app, agent))
        elif path == "/api/muzzle/action":
            h._send(200, request(app, agent, d, issuer))
        elif path == "/api/muzzle/decide":
            if not isinstance(d.get("approve"), bool):
                raise HandshakeError("Send JSON like {\"id\": \"act_...\", \"approve\": true}.")
            h._send(200, decide(app, d.get("id"), d["approve"], issuer))
        elif path == "/api/muzzle/freeze":
            h._send(200, freeze(app))
        else:
            h._send(200, release(app))
    except HandshakeError as e:
        h._send(e.http, {"error": str(e)})
    except passport.PassError as e:
        h._send(getattr(e, "http", 400), {"error": str(e)})
    return True
