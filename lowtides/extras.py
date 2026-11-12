"""Three shopper conveniences on top of the Handshake. None of them can go around it.

  * Autopilot ask   POST /api/shopper/autopilot   "cordless drill under $90": the shopper picks the best item both
                    sides accept and runs one ordinary Handshake. The shopper's mode decides what happens next:
                    Full Autopilot pays a green match, AI Assist holds it for a yes, Off buys nothing.
  * Receipts        GET/POST /api/receipts        every purchase the shoppers tried, plus receipts the person adds
                    for things bought elsewhere, with a plain-English search ("refunded this week over $20").
                    The search is rule-based: no AI model reads the receipts.
  * Talk to a human POST /api/help                the person's message and their chat with the guide are saved for
                    a person and, when LOWTIDES_HELP_WEBHOOK is set, sent to it. GET /api/help lists them for the
                    operator (header X-Help-Token = LOWTIDES_HELP_TOKEN).

Python standard library only.
"""
import datetime
import json
import os
import re
import secrets
import threading
import time
import urllib.request

from . import stores as catalog_stores
from .handshake import HandshakeError, service
from .pricing import money
from . import passport

MAX_RECEIPTS = 200
MAX_HELP = 5000
HELP_PER_HOUR = 5

# ---------------------------------------------------------------------------------------------
# understanding a typed ask
# ---------------------------------------------------------------------------------------------
_AMT = r"\$?\s*(\d+(?:\.\d{1,2})?)"
_FILLER = set("""a an the some any please me my i need want would like to get buy find order grab pick up show for of and
with that is are it scout can you could cheapest best good new one something looking look search shop""".split())


def _tokens(text):
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in re.findall(r"[a-z0-9]+", text.lower())]


def understand(text):
    """'a cordless drill under $90' -> ('cordless drill', 90.0). The price cap is None when none was given."""
    t = " ".join(str(text or "").lower().split())[:200]
    cap = None
    m = re.search(r"\b(?:under|below|less than|at most|up to|max(?:imum)?(?: of)?|no more than|for)\s*" + _AMT, t) \
        or re.search(_AMT + r"\s*(?:or less|or under|max|tops)\b", t)
    if m:
        cap = float(m.group(1))
        t = (t[:m.start()] + " " + t[m.end():]).strip()
    words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in _FILLER]
    return " ".join(words), cap


def matches(q, hay):
    """Every word of the ask has to appear (plural or not) in the item's title, category or store name."""
    hay_l = hay.lower()
    if not q or q in hay_l:
        return True
    have = set(_tokens(hay_l))
    return all(w in have or w in hay_l for w in _tokens(q))


# ---------------------------------------------------------------------------------------------
# autopilot: say it, and the shopper acts inside its Pass
# ---------------------------------------------------------------------------------------------
def autopilot(app, agent, text, issuer):
    svc = service(app)
    q, cap = understand(text)
    if not q:
        raise HandshakeError("Say what to buy, like \"cordless drill under $90\".")
    mode = svc.prefs(agent)["mode"]
    found = svc.catalog(agent, issuer, q, cap)
    counts = {s: sum(1 for i in found if i["signal"] == s) for s in ("match", "needs_yes", "no_match")}
    out = {"ask": str(text)[:200], "understood": {"q": q, "max_price": cap}, "mode": mode, "considered": len(found),
           "counts": counts, "picked": None, "handshake": None}
    name = agent["name"]
    what = "“%s”%s" % (q, " under %s" % money(cap) if cap is not None else "")
    if not found:
        out["said"] = "%s found nothing for %s in the demo stores. Nothing was bought." % (name, what)
        return out
    pick = next((i for i in found if i["signal"] == "match"), None) or next((i for i in found if i["signal"] == "needs_yes"), None)
    if mode == "off":
        out["said"] = "%s is Off, so it bought nothing. Switch to AI Assist or Full Autopilot to let it shop." % name
        return out
    if not pick:
        top = found[0]
        out["said"] = "%s found %d for %s, but none passed both sides. Closest: %s at %s. %s Nothing was bought." % (
            name, len(found), what, top["title"], top["store_name"], top["headline"])
        return out
    out["picked"] = pick
    hs = svc.handshake(agent, pick["store_id"], item=pick["id"], execute=True, issuer=issuer)
    out["handshake"] = hs
    head = "%s checked %d and picked %s at %s for %s." % (name, len(found), pick["title"], pick["store_name"], money(pick["price"]))
    if hs["signal"] == "match" and hs.get("order"):
        tail = "Both sides said yes, so it paid (%s)." % ("simulated: nothing was sent to PayPal" if hs["order"].get("simulated") else "PayPal sandbox")
    elif hs["signal"] == "needs_yes":
        tail = "It is waiting for your yes. No money has moved."
    else:
        tail = "It was refused at purchase time. No money moved."
    out["said"] = head + " " + tail
    return out


# ---------------------------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------------------------
_STOP = set("""what did do i we buy bought purchase purchased spend spent at from on in the a an show me all my our receipt
receipts purchases orders order for how much everything and of with was were any list find scout it that this is are
have has had by to items item thing things stuff get got""".split())
_STATUS_WORDS = (("done", r"\b(done|carried out|booked|reserved|scheduled|actions?)\b"),
                 ("refunded", r"\b(refund\w*|returned?|pulled back|sent back|cancel\w*)\b"),
                 ("waiting", r"\b(held|waiting(?: for (?:my|a|your) yes)?|pending|on hold|needs? (?:my|a|your) yes|yellow)\b"),
                 ("blocked", r"\b(blocked|refused|denied|stopped|declined|red|no match)\b"),
                 ("paid", r"\b(paid|kept|green|went through|completed)\b"),
                 ("added", r"\b(add(?:ed)?|outside|elsewhere|my own|manual\w*|uploaded)\b"))


def _day_start(d):
    return time.mktime(d.timetuple())


def parse_search(text, now=None):
    """Turn 'refunded this week over $20 at harbor' into filters. Returns (filters, plain list of what was understood)."""
    t = " " + " ".join(str(text or "").lower().split())[:200] + " "
    f, said = {}, []
    today = datetime.date.fromtimestamp(now or time.time())

    def cut(m):
        nonlocal t
        t = t[:m.start()] + " " + t[m.end():]

    m = re.search(r"\bbetween\s*" + _AMT + r"\s*(?:and|to|-)\s*" + _AMT, t)
    if m:
        f["min"], f["max"] = sorted((float(m.group(1)), float(m.group(2))))
        said.append("between %s and %s" % (money(f["min"]), money(f["max"])))
        cut(m)
    m = re.search(r"\b(?:over|above|more than|at least|bigger than)\s*" + _AMT, t)
    if m:
        f["min"] = float(m.group(1))
        said.append("over %s" % money(f["min"]))
        cut(m)
    m = re.search(r"\b(?:under|below|less than|at most|up to|cheaper than)\s*" + _AMT, t)
    if m:
        f["max"] = float(m.group(1))
        said.append("under %s" % money(f["max"]))
        cut(m)
    spans = ((r"\btoday\b", today, None, "today"),
             (r"\byesterday\b", today - datetime.timedelta(days=1), today, "yesterday"),
             (r"\bthis week\b", today - datetime.timedelta(days=today.weekday()), None, "this week"),
             (r"\blast week\b", today - datetime.timedelta(days=today.weekday() + 7), today - datetime.timedelta(days=today.weekday()), "last week"),
             (r"\bthis month\b", today.replace(day=1), None, "this month"),
             (r"\blast month\b", (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1), today.replace(day=1), "last month"))
    for pat, start, end, word in spans:
        m = re.search(pat, t)
        if m:
            f["since"] = _day_start(start)
            if end:
                f["until"] = _day_start(end)
            said.append(word)
            cut(m)
            break
    m = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+days?\b", t)
    if m and "since" not in f:
        f["since"] = _day_start(today - datetime.timedelta(days=int(m.group(1))))
        said.append("the last %s days" % m.group(1))
        cut(m)
    for status, pat in _STATUS_WORDS:
        m = re.search(pat, t)
        if m:
            f["status"] = status
            said.append({"added": "added by you", "waiting": "waiting for a yes"}.get(status, status))
            cut(m)
            break
    words = [w for w in _tokens(t) if w not in _STOP]
    if words:
        f["words"] = words
        said.append("matching “%s”" % " ".join(words))
    return f, said


def _spend_status(svc, row):
    if not row["spend_id"]:
        return "blocked", None
    sp = svc.store.spend(row["spend_id"])
    if not sp:
        return "blocked", None
    st = {"paid": "paid", "cancelled": "refunded", "pending": "waiting"}.get(sp["status"], "blocked")
    return st, sp


def all_receipts(app, agent_id=None):
    """Every purchase a shopper tried (executed Handshakes) plus the receipts the person added, newest first."""
    svc = service(app)
    out = []
    names = {sid: svc.store_row(sid)["name"] for sid in catalog_stores.store_ids()}
    sql = "SELECT * FROM hs_handshakes WHERE executed=1" + (" AND agent_id=?" if agent_id else "") + " ORDER BY ts DESC LIMIT 500"
    for r in svc.store.q(sql, (agent_id,) if agent_id else ()):
        snap = json.loads(r["data"] or "{}")
        status, sp = _spend_status(svc, r)
        item = snap.get("item") or {}
        pay = None
        if sp and status in ("paid", "refunded"):
            pay = "Simulated payment" if sp["simulated"] else "PayPal sandbox %s" % (sp.get("payout_batch_id") or "")
        out.append({"id": r["id"], "kind": "lowtides", "ts": r["ts"], "store": names.get(r["store_id"], r["store_id"]),
                    "title": item.get("title") or r["category"] or "purchase", "amount": r["amount"], "category": r["category"] or "",
                    "status": status, "signal": r["signal"], "shopper": (snap.get("agent") or {}).get("name") or "",
                    "payment": pay, "signed": True, "proof_url": "/api/handshakes/%s/proof" % r["id"], "note": snap.get("headline") or ""})
    for r in svc.store.q("SELECT * FROM hs_receipts ORDER BY ts DESC LIMIT ?", (MAX_RECEIPTS,)):
        out.append({"id": r["id"], "kind": "added", "ts": r["ts"], "store": r["store"], "title": r["title"], "amount": r["amount"],
                    "category": r["category"] or "", "status": "added", "signal": None, "shopper": "", "payment": None,
                    "signed": False, "proof_url": None, "note": r["note"] or ""})
    from . import muzzle
    out.extend(muzzle.receipts(app, agent_id))      # things the AI did that were not purchases (Action Muzzle)
    out.sort(key=lambda x: -x["ts"])
    return out


def search_receipts(app, text="", agent_id=None, now=None):
    rows = all_receipts(app, agent_id)
    f, said = parse_search(text, now)
    hit = []
    for r in rows:
        if "min" in f and r["amount"] < f["min"]:
            continue
        if "max" in f and r["amount"] > f["max"]:
            continue
        if "since" in f and r["ts"] < f["since"]:
            continue
        if "until" in f and r["ts"] >= f["until"]:
            continue
        if "status" in f and r["status"] != f["status"]:
            continue
        if f.get("words"):
            hay = " ".join((r["store"], r["title"], r["category"], r["shopper"], r["note"] if r["kind"] == "added" else "")).lower()
            have = set(_tokens(hay))
            if not all(w in have or w in hay for w in f["words"]):
                continue
        hit.append(r)
    spent = round(sum(r["amount"] for r in hit if r["status"] in ("paid", "added", "done")), 2)
    n = len(hit)
    if not rows:
        summary = "No receipts yet. Buy something with your AI shopper, or add a receipt."
    elif not n:
        summary = "Nothing matches%s. %d receipt%s in all." % (" " + ", ".join(said) if said else "", len(rows), "" if len(rows) == 1 else "s")
    else:
        summary = "%d receipt%s%s. %s paid." % (n, "" if n == 1 else "s", " " + ", ".join(said) if said else "", money(spent))
    return {"query": str(text or "")[:200], "understood": said, "count": n, "total": len(rows), "spent_usd": spent,
            "summary": summary, "receipts": hit[:100], "search": "rule-based (no AI model reads your receipts)"}


def from_text(text):
    """Best-effort read of a pasted receipt: the store (first line), the total and a date. The person can fix any of it."""
    text = str(text or "")[:4000]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {}
    if lines:
        out["store"] = re.sub(r"[^\w &'.-]", "", lines[0])[:80].strip()
    m = None
    for m in re.finditer(r"(?im)^\s*(?:grand\s+|order\s+)?total\b[^\d\n$]*\$?\s*(\d{1,6}(?:,\d{3})*(?:\.\d{2})?)", text):
        pass
    if m:
        out["amount"] = float(m.group(1).replace(",", ""))
    else:
        amts = [float(a.replace(",", "")) for a in re.findall(r"\$\s*(\d{1,6}(?:,\d{3})*\.\d{2})", text)]
        if amts:
            out["amount"] = max(amts)
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text) or None
    if m:
        out["bought_on"] = m.group(0)
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b", text)
        if m:
            out["bought_on"] = "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    for ln in lines[1:]:
        if re.search(r"[a-zA-Z]{3}", ln) and not re.search(r"(?i)total|tax|subtotal|thank|receipt|order|date|card|visa|www\.|\d{3}[-.]\d{4}", ln):
            out["title"] = re.sub(r"\s*\$?\d+(?:\.\d{2})?\s*$", "", ln)[:120].strip()
            break
    return out


def add_receipt(app, d):
    if not isinstance(d, dict):
        raise HandshakeError("Send JSON like {\"store\": \"Corner Hardware\", \"title\": \"paint brush\", \"amount\": 12.5}.")
    if set(d) - {"store", "title", "amount", "bought_on", "category", "note", "text", "agent_id"}:
        raise HandshakeError("Unknown field. Use store, title, amount, bought_on, category, note or text.")
    if d.get("text"):
        if not isinstance(d["text"], str):
            raise HandshakeError("'text' must be the receipt as plain text.")
        d = dict(from_text(d["text"]), **{k: v for k, v in d.items() if k != "text" and v not in (None, "")})
    store_name = str(d.get("store") or "").strip()[:80]
    title = str(d.get("title") or "").strip()[:120] or "purchase"
    if not store_name:
        raise HandshakeError("Which store was it? Add the store name.")
    amt = d.get("amount")
    if isinstance(amt, str):
        try:
            amt = float(amt.replace("$", "").replace(",", ""))
        except ValueError:
            amt = None
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or not 0 < amt <= 1_000_000:
        raise HandshakeError("Add the total paid, in dollars.")
    now = time.time()
    ts = now
    day = str(d.get("bought_on") or "").strip()
    if day:
        try:
            ts = time.mktime(datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timetuple())
        except ValueError:
            raise HandshakeError("'bought_on' must look like 2026-11-11.")
        if ts > now + 86400:
            raise HandshakeError("That date is in the future.")
    svc = service(app)
    with svc.lock:
        if svc.store.one("SELECT COUNT(*) AS n FROM hs_receipts")["n"] >= MAX_RECEIPTS:
            raise HandshakeError("You already added %d receipts, the most this demo keeps." % MAX_RECEIPTS, 409)
        rid = "rc_" + secrets.token_hex(6)
        svc.store.run("INSERT INTO hs_receipts(id,ts,added,store,title,amount,category,note) VALUES(?,?,?,?,?,?,?,?)",
                      (rid, ts, now, store_name, title, round(float(amt), 2), str(d.get("category") or "").strip().lower()[:60],
                       str(d.get("note") or "").strip()[:300]))
        svc.store.audit("receipt.added", rid, {"store": store_name, "title": title, "amount": round(float(amt), 2)})
    return {"id": rid, "store": store_name, "title": title, "amount": round(float(amt), 2), "status": "added"}


def delete_receipt(app, rid):
    svc = service(app)
    with svc.lock:
        if not isinstance(rid, str) or not svc.store.one("SELECT id FROM hs_receipts WHERE id=?", (rid,)):
            raise HandshakeError("Only a receipt you added can be removed.", 404)
        svc.store.run("DELETE FROM hs_receipts WHERE id=?", (rid,))
        svc.store.audit("receipt.removed", rid, {})
    return {"id": rid, "removed": True}


# ---------------------------------------------------------------------------------------------
# talk to a human
# ---------------------------------------------------------------------------------------------
HELP_SCHEMA = """
CREATE TABLE IF NOT EXISTS help_requests (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  email TEXT NOT NULL,
  message TEXT NOT NULL,
  page TEXT DEFAULT '',
  chat TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'open'
);
"""
_help_lock = threading.Lock()
_help_hits = {}


def _root(app):
    return getattr(app, "demo_root", None) or app


def _help_ready(root):
    if not getattr(root, "_help_ready", False):
        with root.store.lock:
            root.store.db.executescript(HELP_SCHEMA)
            root.store.db.commit()
        root._help_ready = True


def help_staffed():
    return bool(os.environ.get("LOWTIDES_HELP_WEBHOOK") or os.environ.get("LOWTIDES_HELP_TOKEN"))


def _send_webhook(url, body):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "lowtides-help"})
        urllib.request.urlopen(req, timeout=8).close()
    except Exception:
        pass        # the request is already saved; the operator can still read it with GET /api/help


def help_request(app, d, ip=""):
    if not isinstance(d, dict):
        raise HandshakeError("Send JSON like {\"email\": \"you@example.com\", \"message\": \"...\"}.")
    # Bot guards: a hidden field people never fill, and a form sent faster than a person can type.
    if str(d.get("_honey") or "").strip():
        raise HandshakeError("That didn't send. Please try again.")
    ms = d.get("filled_ms")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms < 3000:
        raise HandshakeError("That was very fast. Please wait a moment and send it again.")
    email = str(d.get("email") or "").strip()[:200]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
        raise HandshakeError("Add an email address so a person can reply.")
    message = str(d.get("message") or "").strip()[:2000]
    if len(message) < 3:
        raise HandshakeError("Say what you need help with.")
    chat = []
    for m in (d.get("chat") if isinstance(d.get("chat"), list) else [])[-30:]:
        if isinstance(m, dict) and m.get("who") in ("you", "guide") and isinstance(m.get("text"), str):
            chat.append({"who": m["who"], "text": m["text"][:1500]})
    now = time.time()
    with _help_lock:
        hits = [t for t in _help_hits.get(ip, []) if t > now - 3600]
        if len(hits) >= HELP_PER_HOUR:
            raise HandshakeError("That is a lot of requests in one hour. Please wait a bit and try again.", 429)
        hits.append(now)
        _help_hits[ip] = hits
        if len(_help_hits) > 5000:
            _help_hits.clear()
    root = _root(app)
    _help_ready(root)
    if root.store.one("SELECT COUNT(*) AS n FROM help_requests")["n"] >= MAX_HELP:
        raise HandshakeError("The help inbox is full right now. Please try again later.", 503)
    hid = "H-" + secrets.token_hex(3).upper()
    root.store.run("INSERT INTO help_requests(id,ts,email,message,page,chat) VALUES(?,?,?,?,?,?)",
                   (hid, now, email, message, str(d.get("page") or "")[:120], json.dumps(chat)))
    hook = os.environ.get("LOWTIDES_HELP_WEBHOOK", "")
    if hook.startswith("https://"):
        threading.Thread(target=_send_webhook, daemon=True, args=(hook, {
            "type": "lowtides.help_request", "id": hid, "email": email, "message": message, "page": str(d.get("page") or "")[:120],
            "chat": chat, "chat_text": "\n".join("%s: %s" % (m["who"], m["text"]) for m in chat)})).start()
    staffed = help_staffed()
    return {"id": hid, "chat_messages": len(chat), "staffed": staffed,
            "said": ("Sent. Your request is %s. A person at Lowtides gets your message and your chat with the guide (%d message%s), and replies by email."
                     % (hid, len(chat), "" if len(chat) == 1 else "s")) if staffed else
                    ("Saved as request %s, with your chat. This server has no person on shift right now, so do not wait for a reply here." % hid)}


def help_list(app, token):
    want = os.environ.get("LOWTIDES_HELP_TOKEN", "")
    if not want or not secrets.compare_digest(str(token or ""), want):
        raise HandshakeError("Not found.", 404)
    root = _root(app)
    _help_ready(root)
    rows = root.store.q("SELECT * FROM help_requests ORDER BY ts DESC LIMIT 200")
    return {"requests": [dict(r, chat=json.loads(r["chat"] or "[]")) for r in rows]}


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------
def route(h, app, method, path):
    mine = (("POST", "/api/shopper/autopilot"), ("GET", "/api/receipts"), ("POST", "/api/receipts"), ("POST", "/api/receipts/delete"),
            ("POST", "/api/help"), ("GET", "/api/help"))
    if (method, path) not in mine:
        return False
    if path == "/api/help" and method == "GET":
        try:
            h._send(200, help_list(app, h.headers.get("X-Help-Token", "")), {"Cache-Control": "no-store"})
        except HandshakeError as e:
            h._send(e.http, {"error": str(e)})
        return True
    if not h._api_allowed(write=method != "GET"):
        return True
    qs = h._path()[1]
    try:
        svc = service(app)
        issuer = passport.issuer_url(h, app)
        if path == "/api/help":
            h._send(200, help_request(app, h._json_in(), h._client_ip()))
            return True
        svc.ensure_scout(issuer)
        if path == "/api/shopper/autopilot":
            d = h._json_in()
            d = d if isinstance(d, dict) else {}
            if not isinstance(d.get("ask"), str):
                raise HandshakeError("Send JSON like {\"ask\": \"cordless drill under $90\"}.")
            h._send(200, autopilot(app, svc.resolve_agent(d.get("agent_id")), d["ask"], issuer))
        elif path == "/api/receipts" and method == "GET":
            aid = int(qs["agent_id"]) if (qs.get("agent_id") or "").isdigit() else None
            h._send(200, search_receipts(app, qs.get("q") or "", aid), {"Cache-Control": "no-store"})
        elif path == "/api/receipts":
            h._send(200, add_receipt(app, h._json_in()))
        else:
            d = h._json_in()
            h._send(200, delete_receipt(app, d.get("id") if isinstance(d, dict) else None))
    except HandshakeError as e:
        h._send(e.http, {"error": str(e)})
    except passport.PassError as e:
        h._send(getattr(e, "http", 400), {"error": str(e)})
    return True
