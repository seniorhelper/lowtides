"""Consent Shopping: the Handshake.

A shopper's AI agent carries a signed Pass (its limits). A store publishes a signed Store
Policy (its rules for AI shoppers). GuardPooch checks the store is real. A Handshake lays
the three side by side and checks BOTH directions. Three signals come out:

    match      both sides said yes, so the payment may go ahead
    needs_yes  nothing is forbidden, but a person has to say yes first (a held approval)
    no_match   a rule on one side says no, so no money moves

`evaluate()` is a pure function: same inputs, same answer, no clock, no database, no network.
The rest of this file is the plumbing around it: the signed Store Policy, signed Handshake
receipts written into the hash-chained audit log, the proof pack anyone can verify with public
keys only, merchant KPIs, the shopper's preferences and the HTTP routes.

Money only moves through the existing spend path (lowtides/spend.py): the Handshake can only
make a payment HARDER to make than the agent's own rules, never easier. A "match" still goes
through every spend rule; if the rules disagree the signal is downgraded and the receipt says so.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

from . import ed25519, guardpooch, passport, stores, trust
from .guard import start_of_today
from .passport import b64d, b64e
from .spend import SpendError, agent_rules, money, poss, tidy_join, validate_rules

SIGNALS = ("match", "needs_yes", "no_match")
STRICTNESS = ("relaxed", "balanced", "strict")
PUBLIC = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
POLICY_FIELDS = ("max_order_no_human", "categories", "min_trust", "require_evidence", "blocked_agents", "mode")
SHOPPER_MODES = ("autopilot", "assist", "off")   # autopilot: buy greens alone; assist: always ask; off: buy nothing
STORE_MODES = ("auto", "review", "closed")        # auto: in-policy orders go through; review: a person checks each; closed: refuse all
SCOUT = "Scout"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hs_stores (
  id TEXT PRIMARY KEY,
  policy TEXT NOT NULL,
  doc TEXT NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hs_handshakes (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  agent_id INTEGER NOT NULL,
  store_id TEXT NOT NULL,
  signal TEXT NOT NULL,
  amount REAL NOT NULL,
  category TEXT DEFAULT '',
  executed INTEGER NOT NULL DEFAULT 0,
  spend_id TEXT,
  shopper_fail INTEGER NOT NULL DEFAULT 0,
  revoked INTEGER NOT NULL DEFAULT 0,
  danger INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL,
  rating TEXT,
  rated_ts REAL
);
CREATE INDEX IF NOT EXISTS hs_agent ON hs_handshakes(agent_id, ts);
CREATE INDEX IF NOT EXISTS hs_store ON hs_handshakes(store_id, ts);
CREATE TABLE IF NOT EXISTS hs_prefs (
  agent_id INTEGER PRIMARY KEY,
  guard_strictness TEXT NOT NULL DEFAULT 'balanced',
  deal_mode INTEGER NOT NULL DEFAULT 50,
  mode TEXT NOT NULL DEFAULT 'autopilot'
);
CREATE TABLE IF NOT EXISTS hs_receipts (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  added REAL NOT NULL,
  store TEXT NOT NULL,
  title TEXT NOT NULL,
  amount REAL NOT NULL,
  category TEXT DEFAULT '',
  note TEXT DEFAULT ''
);
"""
TABLES = ("hs_stores", "hs_handshakes", "hs_prefs", "hs_receipts")


class HandshakeError(Exception):
    def __init__(self, message, http=400):
        super().__init__(message)
        self.http = http


# =============================================================================================
# 1. The match engine (pure)
# =============================================================================================
def _norm(x):
    """'Gift Cards ' -> 'gift card' (lower case, trimmed, a trailing s dropped) so singular and plural match."""
    x = re.sub(r"\s+", " ", str(x or "").strip().lower())
    return x[:-1] if len(x) > 3 and x.endswith("s") else x


def _chk(cid, label, level, detail):
    return {"id": cid, "label": label, "ok": level == "ok", "level": level, "detail": detail}


def _hits(words, category, title):
    cat, t = _norm(category), str(title or "").lower()
    for w in words:
        n = _norm(w)
        if n and (n == cat or (len(n) >= 4 and n in t)):
            return w
    return None


def evaluate(pass_view, policy, guardpooch, trust, order, prefs):
    """Check one purchase in both directions.

    pass_view  {state: VALID|HELD|REVOKED, reason, signature_ok, expired, issuer, agent_id, agent_name,
                spent_today, limits: {max_per_purchase, daily_purchase_limit, approval_over,
                allowed_categories, blocked_categories}}
    policy     {max_order_no_human, categories, min_trust, require_evidence, blocked_agents}
    guardpooch {verdict: safe|caution|danger, score, reasons}
    trust      {score}
    order      {amount, category, title, evidence (optional, default True)}
    prefs      {guard_strictness: relaxed|balanced|strict, mode: autopilot|assist|off (default autopilot)}
    policy.mode  auto|review|closed (default auto)
    Returns {signal, shopper_checks, store_checks, headline}. Each check is {id, label, ok, detail, level}
    where level is "ok", "ask" (a person must say yes) or "fail" (a rule says no).
    """
    lim = pass_view.get("limits") or {}
    amount = round(float(order["amount"]), 2)
    category, title = order.get("category") or "", order.get("title") or ""
    spent = float(pass_view.get("spent_today") or 0)
    strict = (prefs or {}).get("guard_strictness", "balanced")
    name = pass_view.get("agent_name") or "The agent"
    s, t = [], []

    # ---- shopper side: the Pass and the shopper's rules, then GuardPooch ----------------------
    state = pass_view.get("state")
    if state == "VALID":
        s.append(_chk("pass_state", "Pass is valid", "ok", "%s Pass is VALID." % poss(name)))
    elif state == "HELD":
        s.append(_chk("pass_state", "Pass is valid", "fail",
                      "%s Pass is HELD%s, so it can't be used until a person resumes it." % (poss(name), ": " + pass_view["reason"].rstrip(". ") if pass_view.get("reason") else "")))
    else:
        s.append(_chk("pass_state", "Pass is valid", "fail",
                      "%s Pass is REVOKED%s." % (poss(name), ": " + pass_view["reason"].rstrip(". ") if pass_view.get("reason") else "")))
    if pass_view.get("expired"):
        s.append(_chk("pass_expiry", "Pass has not expired", "fail", "The Pass has expired. Ask for a fresh one."))
    else:
        s.append(_chk("pass_expiry", "Pass has not expired", "ok", "The Pass is within its time limit."))
    smode = (prefs or {}).get("mode", "autopilot")
    if smode == "off":
        s.append(_chk("shopper_mode", "Shopper is switched on", "fail", "%s is switched Off, so it can't buy anything until you turn it back on." % name))
    elif smode == "assist":
        s.append(_chk("shopper_mode", "Shopper may buy by itself", "ask", "%s is in Assist mode: it asks you before any money moves." % name))

    cap = lim.get("max_per_purchase")
    if cap is not None and amount > cap:
        s.append(_chk("per_item_cap", "Within the per-item limit", "fail", "%s is over the %s per-item limit." % (money(amount), money(cap))))
    else:
        s.append(_chk("per_item_cap", "Within the per-item limit", "ok",
                      "%s is within the %s per-item limit." % (money(amount), money(cap)) if cap is not None else "No per-item limit."))
    daily = lim.get("daily_purchase_limit")
    if daily is not None:
        left = max(0.0, daily - spent)
        if spent + amount > daily:
            s.append(_chk("daily_cap", "Within today's budget", "fail",
                          "%s would pass today's %s limit (%s left)." % (money(amount), money(daily), money(left))))
        else:
            s.append(_chk("daily_cap", "Within today's budget", "ok", "%s left today; this uses %s." % (money(left), money(amount))))
    allowed = lim.get("allowed_categories") or []
    if allowed and _norm(category) not in {_norm(a) for a in allowed}:
        s.append(_chk("allowed_categories", "Category is allowed", "fail",
                      "%s may only buy: %s. '%s' isn't one of them." % (name, ", ".join(allowed), category or "no category")))
    else:
        s.append(_chk("allowed_categories", "Category is allowed", "ok",
                      "'%s' is allowed." % category if category else "No category restriction."))
    hit = _hits(lim.get("blocked_categories") or [], category, title)
    if hit:
        s.append(_chk("never_buy", "Not on the never-buy list", "fail", "'%s' is on %s never-buy list." % (hit, poss(name))))
    else:
        s.append(_chk("never_buy", "Not on the never-buy list", "ok", "Nothing on the never-buy list matches."))
    verdict = (guardpooch or {}).get("verdict", "safe")
    why = tidy_join((guardpooch or {}).get("reasons") or [])[:300]
    if verdict == "danger":
        s.append(_chk("guardpooch", "GuardPooch says the store is real", "fail", "GuardPooch: Fake store. %s" % why))
    elif verdict == "caution":
        if strict == "strict":
            s.append(_chk("guardpooch", "GuardPooch says the store is real", "fail", "GuardPooch: Be careful, and your setting is strict. %s" % why))
        elif strict == "relaxed":
            s.append(_chk("guardpooch", "GuardPooch says the store is real", "ok", "GuardPooch: Be careful, which your relaxed setting allows. %s" % why))
        else:
            s.append(_chk("guardpooch", "GuardPooch says the store is real", "ask", "GuardPooch: Be careful. %s" % why))
    else:
        s.append(_chk("guardpooch", "GuardPooch says the store is real", "ok", "GuardPooch: safe."))
    ask_line = lim.get("approval_over")
    if ask_line is not None and amount > ask_line:
        s.append(_chk("ask_above", "Under your ask-me line", "ask", "%s is over your %s ask-me line, so you decide." % (money(amount), money(ask_line))))
    else:
        s.append(_chk("ask_above", "Under your ask-me line", "ok", "%s is under your ask-me line." % money(amount)))

    # ---- store side: the Store Policy ----------------------------------------------------------
    if pass_view.get("signature_ok", True) and pass_view.get("issuer"):
        t.append(_chk("accepts_pass", "Store accepts this Pass", "ok", "The Pass signature verifies and it was issued by %s." % pass_view["issuer"]))
    else:
        t.append(_chk("accepts_pass", "Store accepts this Pass", "fail", "The Pass signature or issuer can't be verified."))
    tmode = policy.get("mode") or "auto"
    if tmode == "closed":
        t.append(_chk("store_mode", "Store is open to AI shoppers", "fail", "The store is closed to AI shoppers right now."))
    elif tmode == "review":
        t.append(_chk("store_mode", "Store accepts AI orders by itself", "ask", "The store reviews every AI order: a person has to say yes first."))
    blocked = [str(b).strip().lower() for b in policy.get("blocked_agents") or []]
    me = {str(pass_view.get("agent_name") or "").strip().lower(), str(pass_view.get("agent_id")), "agent:%s" % pass_view.get("agent_id")}
    if blocked and me & set(blocked):
        t.append(_chk("blocked_agent", "Agent is not blocked", "fail", "The store has blocked %s." % name))
    else:
        t.append(_chk("blocked_agent", "Agent is not blocked", "ok", "%s is not on the store's blocked list." % name))
    score = (trust or {}).get("score", 0)
    if score < policy.get("min_trust", 0):
        t.append(_chk("min_trust", "Trust score is high enough", "fail", "Trust score %s is below the store's minimum of %s." % (score, policy["min_trust"])))
    else:
        t.append(_chk("min_trust", "Trust score is high enough", "ok", "Trust score %s meets the store's minimum of %s." % (score, policy.get("min_trust", 0))))
    sold = policy.get("categories") or []
    if sold and _norm(category) not in {_norm(c) for c in sold}:
        t.append(_chk("categories", "Store sells this to AI shoppers", "fail", "The store doesn't sell '%s' to AI shoppers (it sells: %s)." % (category or "no category", ", ".join(sold))))
    else:
        t.append(_chk("categories", "Store sells this to AI shoppers", "ok", "'%s' is sold to AI shoppers." % category if category else "Any category is sold to AI shoppers."))
    top = policy.get("max_order_no_human")
    if top is not None and amount > top:
        t.append(_chk("max_order", "Under the store's no-human limit", "ask",
                      "%s is over the store's %s limit for orders with no human involved, so a person must say yes." % (money(amount), money(top))))
    else:
        t.append(_chk("max_order", "Under the store's no-human limit", "ok", "%s is within the store's %s limit." % (money(amount), money(top or 0))))
    if policy.get("require_evidence"):
        if order.get("evidence", True):
            t.append(_chk("evidence", "Evidence is attached", "ok", "A signed receipt and proof pack come with this order."))
        else:
            t.append(_chk("evidence", "Evidence is attached", "fail", "The store requires evidence and none can be attached."))
    else:
        t.append(_chk("evidence", "Evidence is attached", "ok", "The store doesn't require evidence; one is attached anyway."))

    allc = s + t
    signal = "no_match" if any(c["level"] == "fail" for c in allc) else ("needs_yes" if any(c["level"] == "ask" for c in allc) else "match")
    return {"signal": signal, "shopper_checks": s, "store_checks": t, "headline": _headline(signal, allc, amount, pass_view.get("store_name"), name)}


def _headline(signal, checks, amount, store, agent):
    store = store or "the store"
    if signal == "match":
        return "Both said yes: %s can buy %s from %s." % (agent, money(amount), store)
    if signal == "needs_yes":
        first = next(c for c in checks if c["level"] == "ask")
        return "Needs your yes: %s" % first["detail"]
    first = next(c for c in checks if c["level"] == "fail")
    return "No match — no money moved: %s" % first["detail"]


def reasons_of(result):
    """The plain-English reasons behind a non-green signal (empty for a match)."""
    return [c["detail"] for c in result["shopper_checks"] + result["store_checks"] if c["level"] != "ok"]


# =============================================================================================
# 2. Signing and verifying (Ed25519, the same key and style as the Pass)
# =============================================================================================
def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign_doc(book, doc):
    """Return `doc` plus kid, alg and an Ed25519 signature over its canonical JSON."""
    k = book._key()
    body = dict(doc, kid=k["kid"], alg=passport.ALG)
    body["signature"] = b64e(ed25519.sign(bytes.fromhex(k["seed"]), _canon(body)))
    return body


def _pub_for(keys, kid):
    for k in (keys or {}).get("keys", []):
        if k.get("kid") == kid:
            try:
                return b64d(k["x"])
            except (KeyError, ValueError):
                return None
    return None


def verify_doc(doc, keys):
    """True when `doc` carries a signature that verifies against the public key `keys` lists for its kid.
    `keys` is the JSON from /.well-known/lowtides-pass-keys.json. Needs no secret and no database."""
    if not isinstance(doc, dict) or not doc.get("signature") or not doc.get("kid"):
        return False
    pub = _pub_for(keys, doc["kid"])
    if pub is None or doc.get("alg") != passport.ALG:
        return False
    try:
        sig = b64d(doc["signature"])
    except ValueError:
        return False
    rest = {k: v for k, v in doc.items() if k != "signature"}
    return ed25519.verify(pub, _canon(rest), sig)


def verify_pass_token(token, keys):
    """Offline check of a Pass (JWS) against public keys only. Returns (ok, payload or None)."""
    try:
        h, p, sg = token.strip().split(".")
        header = json.loads(b64d(h))
        pub = _pub_for(keys, header.get("kid"))
        if pub is None or header.get("alg") != passport.ALG:
            return False, None
        ok = ed25519.verify(pub, (h + "." + p).encode("ascii"), b64d(sg))
        return ok, json.loads(b64d(p)) if ok else None
    except (ValueError, AttributeError, KeyError):
        return False, None


def audit_entry_hash(e):
    """Recompute an audit entry's hash from its fields (the same rule the hash chain uses)."""
    raw = json.dumps([e["prev_hash"], round(e["ts"], 6), e["kind"], e["ref"], e["data"]], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def policy_digest(doc):
    return hashlib.sha256(_canon(doc)).hexdigest()


def verify_proof(pack, keys=None):
    """Check a proof pack with public keys only. `keys` defaults to the ones inside the pack, but a
    careful verifier passes the keys they fetched themselves from the issuer's /.well-known file.
    Returns {ok, checks:[{id, ok, detail}]}."""
    keys = keys or pack.get("public_keys")
    out = []

    def add(cid, ok, detail):
        out.append({"id": cid, "ok": bool(ok), "detail": detail})
    rc = pack.get("receipt") or {}
    add("receipt_signature", verify_doc(rc, keys), "The Handshake receipt's Ed25519 signature %s." % ("verifies" if verify_doc(rc, keys) else "does NOT verify"))
    pol = pack.get("store_policy") or {}
    if pol.get("signature"):
        add("policy_signature", verify_doc(pol, keys), "The Store Policy's signature %s." % ("verifies" if verify_doc(pol, keys) else "does NOT verify"))
    else:
        add("policy_signature", rc.get("signal") == "no_match", "The store published no signed policy." + (" The Handshake was refused." if rc.get("signal") == "no_match" else ""))
    add("policy_matches_receipt", policy_digest(pol) == rc.get("policy_sha256"), "The receipt names exactly this Store Policy.")
    tok = (pack.get("pass") or {}).get("token")
    ok, payload = verify_pass_token(tok, keys) if tok else (False, None)
    add("pass_signature", ok, "The Pass signature %s." % ("verifies" if ok else "does NOT verify"))
    add("pass_matches_receipt", bool(ok and payload.get("jti") == (rc.get("pass") or {}).get("jti")), "The receipt names exactly this Pass.")
    ent = (pack.get("audit") or {}).get("entry") or {}
    try:
        h_ok = audit_entry_hash(ent) == ent.get("hash")
        data = json.loads(ent.get("data") or "{}")
    except (KeyError, TypeError, ValueError):
        h_ok, data = False, {}
    add("audit_entry_hash", h_ok, "The audit-log entry's hash recomputes from its contents.")
    add("audit_matches_receipt", data.get("receipt") == rc, "The audit-log entry holds this exact receipt.")
    return {"ok": all(c["ok"] for c in out), "checks": out}


# =============================================================================================
# 3. The service: stores, policies, handshakes, prefs, KPIs
# =============================================================================================
def service(app):
    """The app's Handshake service, made on first use (keeps server.py changes tiny)."""
    s = getattr(app, "handshakes", None)
    if s is None:
        s = app.handshakes = Service(app)
    return s


def reset(app):
    """Demo reset: forget every store edit, handshake and preference, then re-seed the stores."""
    s = service(app)
    with s.lock:
        with app.store.lock:
            for t in TABLES:
                app.store.db.execute("DELETE FROM " + t)
            app.store.db.commit()
        s.seed_stores()
    from . import muzzle            # here, not at the top: muzzle imports this module
    muzzle.reset(app)
    from . import reports           # same reason
    reports.reset(app)


def _num(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not (lo <= v <= hi):
        raise HandshakeError("'%s' must be a number between %s and %s." % (name, lo, hi))
    return v


def _words(v, name, n=40, size=60):
    if not isinstance(v, list) or len(v) > n or not all(isinstance(x, str) and len(x.strip()) <= size for x in v):
        raise HandshakeError("'%s' must be a list of up to %d short words." % (name, n))
    return sorted({x.strip().lower() for x in v if x.strip()})


class Service:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.lock = threading.RLock()
        self._sig_ok = {}
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
            for sql in ("ALTER TABLE hs_prefs ADD COLUMN mode TEXT NOT NULL DEFAULT 'autopilot'",   # databases made before
                        "ALTER TABLE hs_handshakes ADD COLUMN rating TEXT",                     # modes and ratings existed
                        "ALTER TABLE hs_handshakes ADD COLUMN rated_ts REAL"):
                try:
                    self.store.db.execute(sql)
                except Exception:
                    pass
            self.store.db.commit()
        self.seed_stores()

    @property
    def book(self):
        return passport.book(self.app)

    # ---- Store Policies -------------------------------------------------------------------------
    def _policy_doc(self, st, policy, updated):
        return {"type": "lowtides-store-policy", "v": 1,
                "store": {"id": st["id"], "name": st["name"], "domain": st["domain"], "demo": True},
                "policy": {k: policy.get(k, "auto" if k == "mode" else None) for k in POLICY_FIELDS}, "updated_at": _iso(updated)}

    def seed_stores(self):
        with self.lock:
            now = time.time()
            for st in stores.DEMO_STORES:
                if self.store.one("SELECT 1 AS x FROM hs_stores WHERE id=?", (st["id"],)):
                    continue
                doc = self._policy_doc(st, st["policy"], now)
                if st["signed"]:
                    doc = sign_doc(self.book, doc)
                    self.store.audit("store.policy.signed", st["id"], {"store": st["id"], "updated_at": doc["updated_at"], "kid": doc["kid"]})
                self.store.run("INSERT INTO hs_stores(id,policy,doc,updated) VALUES(?,?,?,?)",
                               (st["id"], json.dumps(st["policy"]), json.dumps(doc), now))

    def store_row(self, store_id):
        st = stores.seed_store(store_id)
        row = self.store.one("SELECT * FROM hs_stores WHERE id=?", (store_id,)) if st else None
        if not row:
            raise HandshakeError("No such store.", 404)
        return dict(st, policy=json.loads(row["policy"]), doc=json.loads(row["doc"]), updated=row["updated"])

    def policy_doc(self, store_id):
        return self.store_row(store_id)["doc"]

    def set_policy(self, store_id, d):
        with self.lock:
            st = self.store_row(store_id)
            if not st["signed"]:
                raise HandshakeError("%s is a demo scam store: it has no signed policy and can't get one." % st["name"], 403)
            if not isinstance(d, dict):
                raise HandshakeError("Send JSON like {\"max_order_no_human\": 100}.")
            extra = set(d) - set(POLICY_FIELDS)
            if extra:
                raise HandshakeError("Unknown policy field: %s." % ", ".join(sorted(extra)))
            p = dict(st["policy"])
            if "max_order_no_human" in d:
                p["max_order_no_human"] = round(float(_num(d["max_order_no_human"], "max_order_no_human", 0, 1_000_000)), 2)
            if "min_trust" in d:
                p["min_trust"] = int(_num(d["min_trust"], "min_trust", 0, 100))
            if "categories" in d:
                p["categories"] = _words(d["categories"], "categories")
            if "blocked_agents" in d:
                p["blocked_agents"] = _words(d["blocked_agents"], "blocked_agents", 50)
            if "mode" in d:
                if d["mode"] not in STORE_MODES:
                    raise HandshakeError("'mode' must be auto, review or closed.")
                p["mode"] = d["mode"]
            if "require_evidence" in d:
                if not isinstance(d["require_evidence"], bool):
                    raise HandshakeError("'require_evidence' must be true or false.")
                p["require_evidence"] = d["require_evidence"]
            now = time.time()
            doc = sign_doc(self.book, self._policy_doc(st, p, now))
            self.store.run("UPDATE hs_stores SET policy=?, doc=?, updated=? WHERE id=?", (json.dumps(p), json.dumps(doc), now, store_id))
            self.store.audit("store.policy.signed", store_id, {"store": store_id, "policy": p, "updated_at": doc["updated_at"], "kid": doc["kid"]})
            return self.store_view(store_id)

    def _config(self):
        return (self.app.config.data.get("guardpooch") or {}) if hasattr(self.app, "config") else {}

    def guard(self, store_id):
        st = self.store_row(store_id)
        known = [{"id": k, "name": stores.seed_store(k)["name"], "domain": stores.seed_store(k)["domain"]} for k in stores.KNOWN_GOOD]
        known_items = [it for k in stores.KNOWN_GOOD if k != store_id for it in stores.catalog(k)]
        sig = st["doc"].get("signature")
        if sig not in self._sig_ok:         # checking a signature in pure Python is slow; do it once per signature
            self._sig_ok[sig] = bool(sig) and verify_doc(st["doc"], self.book.jwks())
        signed = self._sig_ok[sig]
        rdap = bool(self._config().get("rdap")) and not self.app.public_demo
        age, source = guardpooch.domain_age_days(st["domain"], rdap_enabled=rdap)
        return guardpooch.check({"id": st["id"], "name": st["name"], "domain": st["domain"]}, known, signed, age_days=age,
                                age_source=source, store_items=stores.catalog(store_id), known_items=known_items)

    def store_trust(self, agent_id, store_id, gp=None):
        """What this shopper should think of this store: GuardPooch's verdict plus the shopper's own orders there."""
        gp = gp or self.guard(store_id)
        if agent_id is None:
            try:
                agent_id = self.resolve_agent(None)["id"]
            except HandshakeError:
                pass
        return trust.store_trust(gp["verdict"], gp["score"], **trust.store_history(self.store, agent_id, store_id))

    def store_view(self, store_id, agent_id=None):
        st = self.store_row(store_id)
        doc = st["doc"]
        pol = dict({"mode": "auto"}, **st["policy"])
        pol.update(updated_at=doc["updated_at"], kid=doc.get("kid"), signature=doc.get("signature"))
        gp = self.guard(store_id)
        return {"id": st["id"], "name": st["name"], "domain": st["domain"], "tagline": st["tagline"], "demo": True,
                "policy": pol, "policy_url": "/.well-known/lowtides-store-policy.json?store=" + st["id"],
                "policy_signed": bool(doc.get("signature")), "guardpooch": gp,
                "store_trust": self.store_trust(agent_id, store_id, gp)}

    def stores(self, agent_id=None):
        return [self.store_view(i, agent_id) for i in stores.store_ids()]

    # ---- agents, the Pass, prefs -------------------------------------------------------------------
    def resolve_agent(self, agent_id):
        if agent_id in (None, ""):
            agents = self.store.agents()
            for a in agents:
                if a["name"] == SCOUT:
                    return a
            mine = self.store.one("SELECT p.agent_id FROM hs_prefs p JOIN agents a ON a.id=p.agent_id ORDER BY p.agent_id LIMIT 1")
            if mine:                    # a renamed Scout, or another shopper made in My agents
                return self.store.agent(mine["agent_id"])
            if not agents:
                raise HandshakeError("There is no shopper agent yet. Create one first.", 409)
            return agents[0]
        try:
            a = self.store.agent(int(agent_id))
        except (TypeError, ValueError):
            raise HandshakeError("'agent_id' must be a number.")
        if not a:
            raise HandshakeError("No such agent.", 404)
        return a

    def pass_view(self, agent, issuer):
        state = self.book.current(agent, issuer)
        lim = (state or {}).get("limits") or {}
        if agent["status"] == "paused":
            st, why = "HELD", "the loop guard paused this agent"
        elif not state or state.get("revoked") or not state.get("signature_ok"):
            st, why = "REVOKED", (state or {}).get("revoked_reason") or "%s has no valid Pass." % agent["name"]
        else:
            st, why = "VALID", None
        return {"state": st, "reason": why, "signature_ok": bool(state and state.get("signature_ok")),
                "expired": bool(state and state.get("expired")), "issuer": (state or {}).get("issuer"),
                "agent_id": agent["id"], "agent_name": agent["name"], "jti": (state or {}).get("jti"), "kid": (state or {}).get("kid"),
                "token": (state or {}).get("token"), "expires_at": (state or {}).get("expires_at"),
                "spent_today": round(self.store.purchases_since(agent["id"], start_of_today()), 2),
                "limits": {k: lim.get(k) for k in ("max_per_purchase", "daily_purchase_limit", "approval_over", "allowed_categories", "blocked_categories")}}

    def prefs(self, agent):
        r = agent_rules(agent)
        row = self.store.one("SELECT * FROM hs_prefs WHERE agent_id=?", (agent["id"],)) or {}
        return {"daily_cap": r["daily_purchase_limit"], "per_item_cap": r["max_per_purchase"], "ask_above": r["approval_over"],
                "guard_strictness": row.get("guard_strictness", "balanced"), "deal_mode": int(row.get("deal_mode", 50)),
                "mode": row.get("mode") or "autopilot",
                "categories_allowed": r["allowed_categories"], "never_buy": r["blocked_categories"]}

    def set_prefs(self, agent, d, issuer):
        if not isinstance(d, dict):
            raise HandshakeError("Send JSON like {\"daily_cap\": 200}.")
        known = {"agent_id", "daily_cap", "per_item_cap", "ask_above", "guard_strictness", "deal_mode", "categories_allowed", "never_buy", "mode"}
        if set(d) - known:
            raise HandshakeError("Unknown setting: %s." % ", ".join(sorted(set(d) - known)))
        with self.lock:
            rules = agent_rules(agent)
            for src, dst in (("daily_cap", "daily_purchase_limit"), ("per_item_cap", "max_per_purchase"), ("ask_above", "approval_over")):
                if src in d:
                    rules[dst] = round(float(_num(d[src], src, 0, 1_000_000)), 2)
            if "categories_allowed" in d:
                rules["allowed_categories"] = _words(d["categories_allowed"], "categories_allowed")
            if "never_buy" in d:
                rules["blocked_categories"] = _words(d["never_buy"], "never_buy")
            cur = self.store.one("SELECT * FROM hs_prefs WHERE agent_id=?", (agent["id"],)) or {}
            strict = d.get("guard_strictness", cur.get("guard_strictness", "balanced"))
            if strict not in STRICTNESS:
                raise HandshakeError("'guard_strictness' must be relaxed, balanced or strict.")
            deal = int(_num(d["deal_mode"], "deal_mode", 0, 100)) if "deal_mode" in d else int(cur.get("deal_mode", 50))
            smode = d.get("mode", cur.get("mode") or "autopilot")
            if smode not in SHOPPER_MODES:
                raise HandshakeError("'mode' must be autopilot, assist or off.")
            try:
                rules = validate_rules(rules)
            except SpendError as e:
                raise HandshakeError(str(e))
            self.store.run("INSERT INTO hs_prefs(agent_id,guard_strictness,deal_mode,mode) VALUES(?,?,?,?) ON CONFLICT(agent_id) "
                           "DO UPDATE SET guard_strictness=excluded.guard_strictness, deal_mode=excluded.deal_mode, mode=excluded.mode",
                           (agent["id"], strict, deal, smode))
            self.store.update_agent(agent["id"], rules=json.dumps(rules))
            self.store.audit("rules.saved", str(agent["id"]), {"rules": rules, "via": "shopper prefs", "guard_strictness": strict, "deal_mode": deal, "mode": smode})
            self.store.event(agent["id"], "control", "Shopping preferences updated. The Pass was re-issued with the new limits.")
            agent = self.store.agent(agent["id"])
            reissued = False
            if agent["status"] == "on" and not self.book.frozen():
                self.book.issue(agent, issuer)     # the old Pass is replaced; stores see the new limits
                reissued = True
            out = self.prefs(agent)
            out["pass_reissued"] = reissued
            return out

    # ---- one Handshake ------------------------------------------------------------------------------
    def _spend_order(self, row):
        if not row or row["status"] not in ("paid", "cancelled"):
            return None
        sim = bool(row["simulated"])
        env = "simulated" if sim else (row.get("payout_env") or "sandbox")
        return {"paypal_order_id": row.get("payout_batch_id"), "payout_item_id": row.get("payout_item_id"),
                "status": row.get("payout_status") or row["status"], "simulated": sim, "kind": "payout", "env": env,
                "spend_id": row["id"], "amount": row["amount"],
                "label": (row.get("note") if sim and (row.get("note") or "").startswith("The PayPal sandbox balance was empty")
                          else "Simulated payout: nothing was sent to PayPal.") if sim
                else "PayPal %s payout %s" % (env, row.get("payout_batch_id"))}

    def handshake(self, agent, store_id, item=None, amount=None, category=None, title=None, execute=False, issuer="", seed_history=False):
        with self.lock:
            st = self.store_row(store_id)
            if item is not None:
                it = stores.find_item(store_id, item)
                if not it:
                    raise HandshakeError("No such item in %s." % st["name"], 404)
                amount, category, title = it["price"], it["category"], it["title"]
            try:
                if isinstance(amount, bool):
                    raise ValueError
                amount = round(float(amount), 2)
            except (TypeError, ValueError):
                raise HandshakeError("Give an item, or an amount in dollars.")
            if not 0 < amount <= 1_000_000:
                raise HandshakeError("'amount' must be more than $0 and at most $1,000,000.")
            category = str(category or "").strip().lower()[:60]
            title = str(title or "").strip()[:200] or (category or "purchase")
            pv = self.pass_view(agent, issuer)
            pv["store_name"] = st["name"]
            gp = self.guard(store_id)
            tr = trust.compute(self.store, agent["id"])
            prefs = self.prefs(agent)
            audit_ok = self.store.audit_verify()["ok"]
            order_in = {"amount": amount, "category": category, "title": title, "evidence": audit_ok}
            ev = evaluate(pv, st["policy"], gp, tr, order_in, prefs)
            signal, s_checks, t_checks = ev["signal"], ev["shopper_checks"], ev["store_checks"]
            headline = ev["headline"]
            spend, order, approval_id = None, None, None
            if execute and signal in ("match", "needs_yes"):
                self._register_store(st)
                try:
                    spend = self.app.spend.request(agent, {"amount": amount, "merchant": st["name"], "item": title, "category": category,
                                                           "reason": "Consent Shopping Handshake with %s (%s)" % (st["name"], st["domain"])},
                                                   extra_ask=[c["detail"] for c in s_checks + t_checks if c["level"] == "ask"],
                                                   force_sim=seed_history)
                except SpendError as e:
                    spend = None
                    signal = "no_match"
                    s_checks.append(_chk("rules_engine", "Lowtides rules at purchase time", "fail", str(e)))
                if spend is not None:
                    if spend["decision"] == "denied" or spend["status"] == "failed":
                        signal = "no_match"
                        why = (spend["reasons"] or [spend.get("note") or "The payment was refused."])[0] if spend["decision"] == "denied" else (spend.get("note") or "PayPal refused the payment.")
                        s_checks.append(_chk("rules_engine", "Lowtides rules at purchase time", "fail", why))
                    elif spend["status"] == "pending":
                        if signal == "match":
                            signal = "needs_yes"
                            s_checks.append(_chk("rules_engine", "Lowtides rules at purchase time", "ask", tidy_join(spend["reasons"])))
                        approval_id = spend["id"]
                    else:
                        order = self._spend_order(spend)
                if signal != ev["signal"]:
                    headline = _headline(signal, s_checks + t_checks, amount, st["name"], agent["name"])
            hid = "hs_" + secrets.token_hex(8)
            now = time.time()
            doc = st["doc"]
            receipt = sign_doc(self.book, {
                "type": "lowtides-handshake-receipt", "v": 1, "id": hid, "at": _iso(now), "signal": signal, "headline": headline,
                "agent": {"id": agent["id"], "name": agent["name"]},
                "store": {"id": st["id"], "name": st["name"], "domain": st["domain"], "demo": True},
                "order": {"amount": amount, "category": category, "title": title, "currency": "USD"},
                "pass": {"jti": pv["jti"], "state": pv["state"], "kid": pv["kid"]},
                "policy_sha256": policy_digest(doc), "policy_updated_at": doc["updated_at"],
                "guardpooch": {"verdict": gp["verdict"], "score": gp["score"]}, "trust": {"score": tr["score"]},
                "shopper_checks": s_checks, "store_checks": t_checks,
                "spend_id": spend["id"] if spend else None, "approval_id": approval_id, "executed": bool(execute),
                "money_moved": bool(order and not order["simulated"]),
                "settlement": ("PayPal sandbox" if order and not order["simulated"] else "simulated" if order else "none")})
            self.store.audit("handshake.receipt", hid, {"receipt": receipt})
            shopper_fail = any(c["level"] == "fail" and c["id"] != "shopper_mode" for c in s_checks)   # Off is the owner's choice, not misbehavior
            self.store.run("INSERT INTO hs_handshakes(id,ts,agent_id,store_id,signal,amount,category,executed,spend_id,shopper_fail,revoked,danger,data) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (hid, now, agent["id"], store_id, signal, amount, category, int(bool(execute)), spend["id"] if spend else None,
                            int(shopper_fail), int(pv["state"] != "VALID" or pv["expired"]), int(gp["verdict"] == "danger"), "{}"))
            after = trust.compute(self.store, agent["id"]) if execute else tr     # a refused attempt counts, so read it after the insert
            result = {"id": hid, "signal": signal, "headline": headline, "shopper_checks": s_checks, "store_checks": t_checks,
                      "guardpooch": gp, "trust": {"score": after["score"], "grade": after["grade"], "before": tr["score"]},
                      "receipt": receipt, "order": order, "approval_id": approval_id, "at": receipt["at"],
                      "agent": {"id": agent["id"], "name": agent["name"]}, "store": {"id": st["id"], "name": st["name"], "domain": st["domain"]},
                      "item": {"title": title, "category": category, "amount": amount}, "executed": bool(execute),
                      "spend_id": spend["id"] if spend else None, "pass_state": pv["state"],
                      "store_trust": self.store_trust(agent["id"], store_id, gp)}
            snapshot = dict(result, _policy_doc=doc, _pass_token=pv["token"])
            self.store.run("UPDATE hs_handshakes SET data=? WHERE id=?", (json.dumps(snapshot), hid))
            if execute and signal == "no_match":
                self.store.event(agent["id"], "spend", "Handshake with %s: no match. %s" % (st["name"], headline))
            return self._live(result)

    def _register_store(self, st):
        payee = os.environ.get("LOWTIDES_DEMO_PAYEE", "merchant-demo@example.com")
        known = self.store.merchant(st["name"])
        if not known or not known.get("paypal_email"):
            m = self.store.add_merchant(st["name"], payee, (st["policy"]["categories"] or [""])[0])
            self.store.audit("merchant.saved", st["name"], {"paypal_email": payee, "category": m["category"], "via": "handshake"})

    def _live(self, result):
        """Refresh the parts of a stored Handshake that can change later (a held approval gets paid)."""
        sid = result.get("spend_id")
        if sid:
            row = self.store.spend(sid)
            if row:
                result["order"] = self._spend_order(row) or result.get("order")
                if result.get("approval_id"):
                    result["approval"] = {"id": sid, "status": row["status"]}
        return result

    def get(self, hid):
        row = self.store.one("SELECT * FROM hs_handshakes WHERE id=?", (hid,))
        if not row:
            raise HandshakeError("No such Handshake.", 404)
        return row

    def public(self, row):
        d = json.loads(row["data"])
        d.pop("_policy_doc", None)
        d.pop("_pass_token", None)
        d["rating"] = row.get("rating")
        return self._live(d)

    # ---- one-tap rating after a paid purchase ----------------------------------------------------------
    def rate(self, hid, rating):
        if rating not in ("up", "down"):
            raise HandshakeError("'rating' must be up or down.")
        with self.lock:
            row = self.get(hid)
            sp = self.store.spend(row["spend_id"]) if row["spend_id"] else None
            if not row["executed"] or not sp or sp["status"] != "paid":
                raise HandshakeError("Only a paid purchase can be rated.", 409)
            self.store.run("UPDATE hs_handshakes SET rating=?, rated_ts=? WHERE id=?", (rating, time.time(), hid))
            self.store.audit("handshake.rated", hid, {"rating": rating, "agent_id": row["agent_id"], "store": row["store_id"]})
            return {"id": hid, "rating": rating, "store_trust": self.store_trust(row["agent_id"], row["store_id"]),
                    "agent_trust": {k: v for k, v in trust.compute(self.store, row["agent_id"]).items() if k in ("score", "grade")}}

    def list(self, limit=50, agent_id=None, store_id=None):
        sql, args = "SELECT * FROM hs_handshakes WHERE 1=1", []
        if agent_id:
            sql += " AND agent_id=?"
            args.append(agent_id)
        if store_id:
            sql += " AND store_id=?"
            args.append(store_id)
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        return [self.public(r) for r in self.store.q(sql, args)]

    # ---- the proof pack ---------------------------------------------------------------------------------
    def proof(self, hid):
        row = self.get(hid)
        snap = json.loads(row["data"])
        pub = self.public(row)
        ent = self.store.one("SELECT * FROM audit WHERE kind='handshake.receipt' AND ref=? ORDER BY id LIMIT 1", (hid,))
        chain = self.store.audit_verify()
        approval = paypal = None
        if row["spend_id"]:
            sp = self.store.spend(row["spend_id"])
            if sp:
                approval = {"id": sp["id"], "decision": sp["decision"], "status": sp["status"], "decided_by": sp["decided_by"],
                            "reasons": sp["reasons"], "amount": sp["amount"], "merchant": sp["merchant"]}
                if sp["status"] in ("paid", "cancelled"):
                    paypal = {"environment": "simulated" if sp["simulated"] else (sp.get("payout_env") or "sandbox"),
                              "simulated": sp["simulated"], "payout_batch_id": sp["payout_batch_id"], "payout_item_id": sp["payout_item_id"],
                              "status": sp["payout_status"], "summary": sp["payout_proof"]}
        keys = self.book.jwks()
        pack = {"lowtides_handshake_proof": 1, "generated": _iso(time.time()),
                "handshake": {"id": hid, "at": pub["at"], "signal": pub["signal"], "headline": pub["headline"]},
                "pass": {"token": snap.get("_pass_token"), "jti": pub["receipt"]["pass"]["jti"], "state_at_handshake": pub["receipt"]["pass"]["state"]},
                "store_policy": snap.get("_policy_doc"), "receipt": pub["receipt"], "approval": approval, "paypal": paypal,
                "audit": {"entry": ent, "chain_ok": chain["ok"], "chain_entries": chain["count"],
                          "note": "chain_ok is this server's own check of the whole log. The entry's hash can be recomputed by anyone."},
                "public_keys": keys,
                "how_to_verify": ["Fetch the issuer's public keys from /.well-known/lowtides-pass-keys.json (do not trust the copy in this file).",
                                  "Check the receipt, the Store Policy and the Pass signatures (Ed25519) against those keys.",
                                  "Recompute the audit entry's hash from its fields and compare it to the stored hash.",
                                  "Or run lowtides.handshake.verify_proof(pack, keys)."]}
        pack["verification"] = verify_proof(pack, keys)
        return pack

    # ---- Match Finder -------------------------------------------------------------------------------------
    def catalog(self, agent, issuer, q="", max_price=None, store_id=None):
        pv = self.pass_view(agent, issuer)
        tr = trust.compute(self.store, agent["id"])
        prefs = self.prefs(agent)
        audit_ok = self.store.audit_verify()["ok"]
        out = []
        q = (q or "").strip().lower()
        from .extras import matches      # every word of the search has to be there, plural or not
        for sid in stores.store_ids():
            if store_id and sid != store_id:
                continue
            st = self.store_row(sid)
            gp = self.guard(sid)
            stt = self.store_trust(agent["id"], sid, gp)
            for it in stores.catalog(sid):
                if max_price is not None and it["price"] > max_price:
                    continue
                if q and not matches(q, it["title"] + " " + it["category"] + " " + st["name"]):
                    continue
                ev = evaluate(dict(pv, store_name=st["name"]), st["policy"], gp, tr,
                              {"amount": it["price"], "category": it["category"], "title": it["title"], "evidence": audit_ok}, prefs)
                out.append({"id": it["id"], "store_id": sid, "store_name": st["name"], "title": it["title"], "category": it["category"],
                            "price": it["price"], "signal": ev["signal"], "reasons": reasons_of(ev), "headline": ev["headline"],
                            "guardpooch_verdict": gp["verdict"], "guardpooch_score": gp["score"],
                            "store_trust": {"badge": stt["badge"], "score": stt["score"]}})
        order = {s: i for i, s in enumerate(SIGNALS)}
        if prefs["deal_mode"] >= 50:       # deal hunter: cheapest first inside each group
            out.sort(key=lambda x: (order[x["signal"]], x["price"], x["title"]))
        else:                               # careful: the safest store first, then cheapest
            out.sort(key=lambda x: (order[x["signal"]], -x["guardpooch_score"], x["price"], x["title"]))
        return out

    # ---- merchant KPIs ---------------------------------------------------------------------------------------
    def kpis(self, store_id=None):
        if store_id:
            self.store_row(store_id)
        # look-only checks (execute:false, e.g. "Test it with Scout") are not AI orders: they count toward no KPI
        rows = self.store.q("SELECT * FROM hs_handshakes WHERE executed=1" + (" AND store_id=?" if store_id else "") + " ORDER BY ts", (store_id,) if store_id else ())
        total = len(rows)
        by_signal = {s: sum(1 for r in rows if r["signal"] == s) for s in SIGNALS}
        spends = {}
        for r in rows:
            if r["spend_id"]:
                sp = self.store.spend(r["spend_id"])
                if sp:
                    spends[r["id"]] = sp
        orders = [sp for sp in spends.values() if sp["status"] in ("paid", "cancelled")]
        paid = [sp for sp in orders if sp["status"] == "paid"]
        captured = round(sum(sp["amount"] for sp in paid), 2)
        with_proof = 0
        for r in rows:
            if self.store.one("SELECT 1 AS x FROM audit WHERE kind='handshake.receipt' AND ref=?", (r["id"],)):
                with_proof += 1

        def rate(n, d):
            return round(n / d, 3) if d else 0.0
        days = {}
        for r in rows:
            day = datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
            d = days.setdefault(day, {"day": day, "match": 0, "needs_yes": 0, "no_match": 0, "captured_usd": 0.0})
            d[r["signal"]] += 1
            sp = spends.get(r["id"])
            if sp and sp["status"] == "paid":
                d["captured_usd"] = round(d["captured_usd"] + sp["amount"], 2)
        agents = {}
        for r in rows:
            a = agents.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "name": None, "trust": 0, "orders": 0, "captured_usd": 0.0, "refunds": 0, "refused": 0})
            sp = spends.get(r["id"])
            if sp and sp["status"] in ("paid", "cancelled"):
                a["orders"] += 1
                if sp["status"] == "paid":
                    a["captured_usd"] = round(a["captured_usd"] + sp["amount"], 2)
                else:
                    a["refunds"] += 1
            if r["signal"] == "no_match":
                a["refused"] += 1
        for aid, a in agents.items():
            ag = self.store.agent(aid)
            a["name"] = ag["name"] if ag else "(deleted agent)"
            a["trust"] = trust.compute(self.store, aid)["score"] if ag else 0
        refunded = [sp for sp in orders if sp["status"] == "cancelled"]
        kept_by_agent = {}
        for r in rows:
            sp = spends.get(r["id"])
            if sp and sp["status"] == "paid":
                kept_by_agent[r["agent_id"]] = kept_by_agent.get(r["agent_id"], 0) + 1
        buyers = {r["agent_id"] for r in rows if spends.get(r["id"]) and spends[r["id"]]["status"] in ("paid", "cancelled")}
        returning = sum(1 for n in kept_by_agent.values() if n >= 2)
        stopped = [r for r in rows if r["signal"] == "no_match" and r["executed"]]   # real order attempts, not look-only checks
        rated = [r for r in rows if r.get("rating") in ("up", "down")]
        return {"store_id": store_id, "handshakes": total, "orders": len(orders), "captured_usd": captured,
                "gross_usd": round(sum(sp["amount"] for sp in orders), 2), "refunded_usd": round(sum(sp["amount"] for sp in refunded), 2),
                "net_captured_usd": captured, "kept_order_rate": rate(len(paid), len(orders)),
                "returning_shoppers": returning, "shoppers_with_orders": len(buyers), "retention_rate": rate(returning, len(buyers)),
                "ratings": len(rated), "rated_up": sum(1 for r in rated if r["rating"] == "up"),
                "satisfaction_rate": rate(sum(1 for r in rated if r["rating"] == "up"), len(rated)),
                "bad_orders_stopped": len(stopped), "bad_orders_stopped_usd": round(sum(r["amount"] for r in stopped), 2),
                "approval_rate": rate(by_signal["match"], total), "held_rate": rate(by_signal["needs_yes"], total),
                "refused_rate": rate(by_signal["no_match"], total), "refund_rate": rate(len(orders) - len(paid), len(orders)),
                "avg_order": round(captured / len(paid), 2) if paid else 0.0, "proof_coverage": rate(with_proof, total),
                "revoked_blocked": sum(1 for r in rows if r["signal"] == "no_match" and r["revoked"]),
                "scam_blocked": sum(1 for r in rows if r["signal"] == "no_match" and r["danger"]),
                "by_signal": by_signal, "by_day": list(days.values()), "by_agent": sorted(agents.values(), key=lambda a: -a["captured_usd"])}

    # ---- store opportunities: rule-based what-ifs over the store's own Handshakes ---------------------------
    @staticmethod
    def _signal_of(levels):
        return "no_match" if "fail" in levels else ("needs_yes" if "ask" in levels else "match")

    def _whatif_signal(self, snap, policy):
        """Re-run the store side of `evaluate` for a past Handshake under `policy`; the shopper side is kept as it was
        (a store's rules can't change what the shopper's own Pass said)."""
        chk = {c["id"]: c for c in snap.get("store_checks") or []}
        trust_at = ((snap.get("receipt") or {}).get("trust") or {}).get("score", (snap.get("trust") or {}).get("before", 0))
        ag = snap.get("agent") or {}
        it = snap.get("item") or {}
        accepts = (chk.get("accepts_pass") or {}).get("level", "ok") == "ok"
        pv = {"state": "VALID", "signature_ok": accepts, "issuer": "issuer" if accepts else "", "agent_id": ag.get("id"),
              "agent_name": ag.get("name"), "limits": {}, "spent_today": 0}
        ev = evaluate(pv, policy, {"verdict": "safe", "reasons": []}, {"score": trust_at},
                      {"amount": it.get("amount", 0), "category": it.get("category", ""), "title": it.get("title", ""),
                       "evidence": (chk.get("evidence") or {}).get("level", "ok") == "ok"}, {})
        levels = [c["level"] for c in snap.get("shopper_checks") or []] + [c["level"] for c in ev["store_checks"]]
        return self._signal_of(levels)

    def opportunities(self, store_id):
        """Findings computed only from this store's own Handshake and spend rows plus its current policy.
        Each is {id, kind: grow|protect, title, detail, impact_usd|None, action: {label, policy_patch}|None}."""
        st = self.store_row(store_id)
        out = {"store_id": store_id, "store_name": st["name"], "findings": [], "based_on": {"requests": 0, "orders": 0},
               "label": "Rule-based suggestions from your own orders"}
        if not st["signed"]:
            out["note"] = "%s has no signed Store Policy, so there is nothing to tune." % st["name"]
            return out
        pol = dict({"mode": "auto"}, **st["policy"])
        rows = self.store.q("SELECT * FROM hs_handshakes WHERE store_id=? ORDER BY ts", (store_id,))
        latest = {}                      # one vote per AI shopper per item: repeats and re-checks don't inflate the numbers
        for r in rows:
            snap = json.loads(r["data"] or "{}")
            if snap.get("item"):
                latest[(r["agent_id"], snap["item"].get("title"), snap["item"].get("amount"))] = (r, snap)
        reqs = list(latest.values())
        spends = {r["id"]: self.store.spend(r["spend_id"]) for r, _ in reqs if r["spend_id"]}
        orders = [(r, snap, spends[r["id"]]) for r, snap in reqs if spends.get(r["id"]) and spends[r["id"]]["status"] in ("paid", "cancelled")]
        out["based_on"] = {"requests": len(reqs), "orders": len(orders)}
        if not reqs:
            out["note"] = "Not enough AI orders at %s yet. Suggestions appear once AI shoppers have tried to buy here." % st["name"]
            return out
        rank = {"no_match": 0, "needs_yes": 1, "match": 2}
        base = [(r, snap, self._whatif_signal(snap, pol)) for r, snap in reqs]

        def grow(fid, title, patch, label):
            cand = dict(pol, **patch)
            won = [(r, snap, b, self._whatif_signal(snap, cand)) for r, snap, b in base]
            won = [(r, snap, b, n) for r, snap, b, n in won if rank[n] > rank[b]]
            if not won:
                return
            usd = round(sum(snap["item"]["amount"] for _, snap, _, _ in won), 2)
            agents = len({r["agent_id"] for r, _, _, _ in won})
            auto = sum(1 for *_, n in won if n == "match")
            out["findings"].append({
                "id": fid, "kind": "grow", "title": title,
                "detail": "%d order%s from %d AI shopper%s, worth %s, would have been accepted%s." % (
                    len(won), "" if len(won) == 1 else "s", agents, "" if agents == 1 else "s", money(usd),
                    "" if auto == len(won) else " (%d of them after a person says yes)" % (len(won) - auto)),
                "impact_usd": usd, "action": {"label": label, "policy_patch": patch}})

        if pol.get("mode") != "auto":
            grow("mode_auto", "Accept in-policy AI orders automatically", {"mode": "auto"}, "Switch to Auto-accept")
        if pol.get("min_trust", 0) > 0:
            lower = max(0, int(pol["min_trust"]) - 10)
            grow("min_trust_down", "Lower your minimum trust score from %s to %s" % (pol["min_trust"], lower), {"min_trust": lower},
                 "Lower to %s" % lower)
        top = pol.get("max_order_no_human") or 0
        grow("max_order_up", "Raise the no-person limit from %s to %s" % (money(top), money(top + 50)),
             {"max_order_no_human": round(top + 50, 2)}, "Raise to %s" % money(top + 50))
        missing = {}
        for r, snap, b in base:
            for c in snap.get("store_checks") or []:
                if c["id"] == "categories" and c["level"] == "fail":
                    cat = (snap["item"].get("category") or "").strip().lower()
                    if cat and _norm(cat) not in {_norm(x) for x in pol.get("categories") or []}:
                        missing[cat] = missing.get(cat, 0) + snap["item"]["amount"]
        for cat in sorted(missing, key=lambda c: -missing[c])[:2]:
            grow("open_" + re.sub(r"[^a-z0-9]+", "_", cat), "Sell %s to AI shoppers" % cat,
                 {"categories": sorted(set(pol.get("categories") or []) | {cat})}, "Open %s" % cat)

        # protective: refunds concentrated in low-trust shoppers -> raise the minimum
        def t_at(snap):
            return ((snap.get("receipt") or {}).get("trust") or {}).get("score", 0)
        refunded = [(r, snap, sp) for r, snap, sp in orders if sp["status"] == "cancelled"]
        if len(refunded) >= 2:
            t = min(100, (max(t_at(snap) for _, snap, _ in refunded) // 10) * 10 + 10)
            if t > pol.get("min_trust", 0):
                saved = round(sum(sp["amount"] for _, snap, sp in refunded if t_at(snap) < t), 2)
                lost = round(sum(sp["amount"] for _, snap, sp in orders if sp["status"] == "paid" and t_at(snap) < t), 2)
                if saved > lost:
                    out["findings"].append({
                        "id": "min_trust_up", "kind": "protect", "title": "Raise your minimum trust score to %s" % t,
                        "detail": "All %d pulled-back orders (%s) came from AI shoppers below trust %s. Kept orders you would also turn away: %s." % (
                            len(refunded), money(saved), t, money(lost)),
                        "impact_usd": saved, "action": {"label": "Raise to %s" % t, "policy_patch": {"min_trust": t}}})
        # protective: an AI shopper that keeps trying with a revoked Pass or after being blocked
        tries = {}
        for r in rows:
            if r["executed"] and r["signal"] == "no_match":
                snap = json.loads(r["data"] or "{}")
                ids = {c["id"] for c in snap.get("store_checks") or [] if c["level"] == "fail"}
                if r["revoked"] or "blocked_agent" in ids:
                    tries.setdefault(r["agent_id"], [0, (snap.get("agent") or {}).get("name") or ""])[0] += 1
        blocked = {str(b).lower() for b in pol.get("blocked_agents") or []}
        for aid, (n, name) in tries.items():
            if n < 3 or not name:
                continue
            if name.lower() in blocked:
                out["findings"].append({"id": "keeps_trying_%s" % aid, "kind": "protect", "title": "%s keeps trying after you blocked it" % name,
                                        "detail": "%d refused attempts. Nothing was paid; your block is working." % n, "impact_usd": None, "action": None})
            else:
                out["findings"].append({"id": "block_%s" % aid, "kind": "protect", "title": "%s keeps trying with a revoked Pass" % name,
                                        "detail": "%d refused attempts at %s. Nothing was paid, but you can block it outright." % (n, st["name"]),
                                        "impact_usd": None,
                                        "action": {"label": "Block %s" % name, "policy_patch": {"blocked_agents": sorted(blocked | {name.lower()})}}})
        if not out["findings"]:
            out["note"] = "Nothing to suggest right now: no refused or held order at %s would change with one small rule change." % st["name"]
        return out

    # ---- the shopper's agents ("My agents") -----------------------------------------------------------------
    PRESETS = {
        "everyday": {"name": "Everyday Scout", "rules": {"max_per_purchase": 200, "daily_purchase_limit": 400, "approval_over": 100,
                                                "blocked_categories": ["gift cards", "crypto"], "max_purchases_per_hour": 20},
                     "strict": "balanced", "deal": 50, "mode": "autopilot"},
        "bargain": {"name": "Bargain Scout", "rules": {"max_per_purchase": 60, "daily_purchase_limit": 150, "approval_over": 40,
                                                       "blocked_categories": ["gift cards", "crypto"], "max_purchases_per_hour": 20},
                    "strict": "balanced", "deal": 90, "mode": "autopilot"},
        "restock": {"name": "Restock", "rules": {"max_per_purchase": 100, "daily_purchase_limit": 200, "approval_over": 50,
                                                 "allowed_categories": ["pet supplies", "office", "hardware"],
                                                 "blocked_categories": ["gift cards", "crypto"], "max_purchases_per_hour": 20},
                    "strict": "balanced", "deal": 50, "mode": "assist"},
    }
    MAX_SHOPPERS = 6

    def shoppers(self, issuer):
        ids = [r["agent_id"] for r in self.store.q("SELECT agent_id FROM hs_prefs ORDER BY agent_id")]
        out = []
        vault = getattr(self.app, "vault", None)
        for aid in ids:
            a = self.store.agent(aid)
            if not a:
                continue
            pv = self.pass_view(a, issuer)
            pr = self.prefs(a)
            v = vault.status(aid) if vault else {"status": "none", "label": "not connected", "simulated": True}
            out.append({"id": aid, "name": a["name"], "status": a["status"], "mode": pr["mode"], "pass_state": pv["state"],
                        "trust": trust.compute(self.store, aid)["score"], "spent_today": pv["spent_today"], "daily_cap": pr["daily_cap"],
                        "per_item_cap": pr["per_item_cap"], "ask_above": pr["ask_above"],
                        "paypal": {"status": v.get("status"), "label": v.get("label"), "simulated": bool(v.get("simulated"))}})
        return out

    def add_shopper(self, preset, name, issuer):
        p = self.PRESETS.get(preset)
        if not p:
            raise HandshakeError("'preset' must be everyday, bargain or restock.")
        name = str(name or p["name"]).strip()[:60] or p["name"]
        with self.lock:
            n = self.store.one("SELECT COUNT(*) AS n FROM hs_prefs")["n"]
            if n >= self.MAX_SHOPPERS:
                raise HandshakeError("You already have %d AI shoppers, the most this demo allows." % n, 409)
            aid = self.store.add_agent(name, 5.0, 1.0)
            self.store.update_agent(aid, rules=json.dumps(validate_rules(dict(p["rules"]))))
            self.store.run("INSERT OR REPLACE INTO hs_prefs(agent_id,guard_strictness,deal_mode,mode) VALUES(?,?,?,?)",
                           (aid, p["strict"], p["deal"], p["mode"]))
            self.store.audit("agent.created", str(aid), {"name": name, "preset": preset, "via": "shopper control center"})
            self.store.event(aid, "control", "%s joined from the %s preset." % (name, preset))
            if not self.book.frozen():             # a shopper made during a freeze gets its Pass on release
                self.book.issue(self.store.agent(aid), issuer)
        return [x for x in self.shoppers(issuer) if x["id"] == aid][0]

    # ---- the demo shopper -------------------------------------------------------------------------------------
    def ensure_scout(self, issuer):
        """Public demo only: make sure this visitor's sandbox has the consumer shopper agent, Scout.
        Made the first time a shopping route is used (not at reset), so the original runaway-agent
        story and its numbers are untouched. Scout starts with a few simulated purchases so it has
        a trust score worth showing."""
        if not self.app.public_demo:
            return None
        with self.lock:
            for a in self.store.agents():
                if a["name"] == SCOUT:
                    return a
            mine = self.store.one("SELECT p.agent_id FROM hs_prefs p JOIN agents a ON a.id=p.agent_id ORDER BY p.agent_id LIMIT 1")
            if mine:                    # Scout was renamed in My agents: don't make a second one
                return self.store.agent(mine["agent_id"])
            aid = self.store.add_agent(SCOUT, 5.0, 1.0)
            rules = validate_rules({"max_per_purchase": 200, "daily_purchase_limit": 400, "approval_over": 100,
                                    "blocked_categories": ["gift cards", "crypto"], "max_purchases_per_hour": 20})
            self.store.update_agent(aid, rules=json.dumps(rules))
            self.store.run("INSERT OR REPLACE INTO hs_prefs(agent_id,guard_strictness,deal_mode) VALUES(?,?,?)", (aid, "relaxed", 50))
            self.store.event(aid, "control", "Scout, the demo shopper, joined. Its rules: up to $200 an item, $400 a day, ask over $100, never gift cards or crypto.")
            agent = self.store.agent(aid)
            for sid, item in (("tidepool", "tide-treats"), ("tidepool", "tide-toys"), ("pinepost", "pine-planter"), ("pinepost", "pine-paint")):
                self.handshake(agent, sid, item=item, execute=True, issuer=issuer, seed_history=True)
            self.store.run("UPDATE hs_prefs SET guard_strictness='balanced' WHERE agent_id=?", (aid,))
            return self.store.agent(aid)


# =============================================================================================
# 4. HTTP
# =============================================================================================
def _qs_num(qs, key):
    v = qs.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        raise HandshakeError("'%s' must be a number." % key)


def agent_result(r):
    """The Handshake answer an outside agent gets: the signal, both sides' checks, and what to do next."""
    order = r.get("order")
    sig = r["signal"]
    if sig == "match" and r.get("executed") and order and order.get("simulated"):
        nxt = "Both sides said yes. The payment was simulated (nothing was sent to PayPal). Do not pay for this again."
    elif sig == "match" and r.get("executed"):
        nxt = "Paid through the PayPal sandbox. Both sides said yes. Keep the receipt id; do not pay for this again."
    elif sig == "match":
        nxt = "Both sides say yes. Send the same request with \"execute\": true to pay."
    elif sig == "needs_yes" and r.get("approval_id"):
        nxt = "Held for a person. Do not pay another way. Check again later with GET /v1/handshake/%s." % r["id"]
    elif sig == "needs_yes":
        nxt = "A person has to say yes. Send \"execute\": true to ask the owner, then wait. Do not pay another way."
    else:
        nxt = "No match. Nothing moved. Do not retry the same purchase or look for a way around it."
    gp = r.get("guardpooch") or {}
    return {"id": r["id"], "signal": sig, "headline": r["headline"], "executed": bool(r.get("executed")),
            "shopper_checks": r["shopper_checks"], "store_checks": r["store_checks"],
            "store": r["store"], "item": r["item"], "agent": r["agent"], "pass_state": r.get("pass_state"),
            "store_check": {"verdict": gp.get("verdict"), "score": gp.get("score")},
            "trust": r.get("trust"), "order": order, "approval_id": r.get("approval_id"), "approval": r.get("approval"),
            "money_moved": bool(order and not order.get("simulated") and order.get("status") != "cancelled"),
            "receipt_id": r["id"], "receipt_signed": bool((r.get("receipt") or {}).get("signature")), "at": r.get("at"),
            "next": nxt}


def _agent_route(h, app, method, path, mh):
    """Agent-facing Consent Shopping (auth: the agent's own lt- key, like /v1/spend):
    GET /v1/stores, POST /v1/handshake, GET /v1/handshake/{id}. Same engine as the site's buttons."""
    agent = h._agent_or_401()
    if not agent:
        return True
    try:
        svc = service(app)
        issuer = passport.issuer_url(h, app)
        if path == "/v1/stores":
            out = []
            for sid in stores.store_ids():
                v = svc.store_view(sid, agent["id"])
                out.append({"id": v["id"], "name": v["name"], "domain": v["domain"], "demo": True, "policy": v["policy"],
                            "policy_url": v["policy_url"], "policy_signed": v["policy_signed"],
                            "store_check": {"verdict": v["guardpooch"]["verdict"], "score": v["guardpooch"]["score"]},
                            "items": [{"id": i["id"], "title": i["title"], "price": i["price"], "category": i["category"]}
                                      for i in stores.catalog(sid)]})
            h._send(200, {"agent": agent["name"], "stores": out})
        elif mh:
            row = svc.get(mh.group(1))
            if row["agent_id"] != agent["id"]:
                raise HandshakeError("No such Handshake for this agent.", 404)
            h._send(200, agent_result(svc.public(row)))
        else:
            d = h._json_in()
            if not isinstance(d, dict) or not isinstance(d.get("store_id"), str):
                raise HandshakeError("Send JSON like {\"store_id\": \"harbor\", \"item_id\": \"harbor-hammer\"} "
                                     "or {\"store_id\": \"harbor\", \"amount\": 12.5, \"category\": \"tools\", \"title\": \"...\"}.")
            if "execute" in d and not isinstance(d["execute"], bool):
                raise HandshakeError("'execute' must be true or false.")
            h._send(200, agent_result(svc.handshake(agent, d["store_id"], item=d.get("item_id"), amount=d.get("amount"),
                                                    category=d.get("category"), title=d.get("title"),
                                                    execute=bool(d.get("execute")), issuer=issuer)))
    except HandshakeError as e:
        h._send(e.http, {"error": str(e)})
    except SpendError as e:
        h._send(e.http, {"error": str(e)})
    return True


def route(h, app, method, path):
    """Handle the Consent Shopping endpoints. Returns True when it answered the request."""
    if method == "GET" and path == "/.well-known/lowtides-store-policy.json":
        qs = h._path()[1]
        target = app
        sb = getattr(getattr(app, "demo_root", None) or app, "sandboxes", None)
        if sb is not None:      # a visitor's own edits are served to that visitor; everyone else sees the shared demo
            sid = sb.parse(h._cookie("lt_demo"))
            target = (sb.get(sid, create=False, wait=False) if sid else None) or app
        try:
            h._send(200, service(target).policy_doc(qs.get("store", "")), dict(PUBLIC, **{"Cache-Control": "no-cache"}))
        except HandshakeError as e:
            h._send(e.http, {"error": str(e)}, PUBLIC)
        return True
    mh = re.fullmatch(r"/v1/handshake/(hs_[0-9a-f]+)", path)
    if (method, path) in (("GET", "/v1/stores"), ("POST", "/v1/handshake")) or (mh and method == "GET"):
        return _agent_route(h, app, method, path, mh)
    m = re.fullmatch(r"/api/stores/([a-z0-9-]+)/policy", path)
    if m and method in ("PUT", "POST"):
        path = "/api/stores/policy"
    elif m:
        return False
    mine = (("GET", "/api/stores"), ("PUT", "/api/stores/policy"), ("POST", "/api/stores/policy"), ("GET", "/api/catalog"), ("POST", "/api/handshake"),
            ("GET", "/api/handshakes"), ("GET", "/api/merchant/kpis"), ("GET", "/api/shopper/prefs"), ("PUT", "/api/shopper/prefs"),
            ("POST", "/api/shopper/prefs"), ("GET", "/api/merchant/opportunities"), ("GET", "/api/shopper/agents"),
            ("POST", "/api/shopper/agents"), ("GET", "/api/shopper/connect"))
    mp = re.fullmatch(r"/api/handshakes/(hs_[0-9a-f]+)/proof", path)
    mt = re.fullmatch(r"/api/agents/(\d+)/trust", path)
    mr = re.fullmatch(r"/api/handshakes/(hs_[0-9a-f]+)/rating", path)
    if not ((method, path) in mine or (mp and method == "GET") or (mt and method == "GET") or (mr and method == "POST")):
        return False
    if not h._api_allowed(write=method != "GET"):
        return True
    qs = h._path()[1]
    try:
        svc = service(app)
        issuer = passport.issuer_url(h, app)
        svc.ensure_scout(issuer)
        if method == "GET" and path == "/api/stores":
            try:
                who = svc.resolve_agent(qs.get("agent_id"))
            except HandshakeError as e:
                if e.http != 404:
                    raise
                who = svc.resolve_agent(None)      # a shopper that was just reset away: show the default one instead of an error
            h._send(200, {"stores": svc.stores(who["id"])})
        elif path == "/api/stores/policy":
            h._send(200, svc.set_policy(m.group(1), h._json_in()))
        elif path == "/api/catalog":
            agent = svc.resolve_agent(qs.get("agent_id"))
            q, cap = qs.get("q"), _qs_num(qs, "max_price")
            if q and cap is None and re.search(r"\d", q):      # "drill under $90" typed into the search box
                from .extras import understand
                q2, cap2 = understand(q)
                if cap2 is not None:
                    q, cap = q2, cap2
            h._send(200, {"agent": {"id": agent["id"], "name": agent["name"]}, "understood": {"q": q or "", "max_price": cap},
                          "items": svc.catalog(agent, issuer, q, cap, qs.get("store_id") or None)})
        elif path == "/api/handshake":
            d = h._json_in()
            if not isinstance(d, dict):
                raise HandshakeError("Send JSON like {\"agent_id\": 1, \"store_id\": \"harbor\", \"item_id\": \"harbor-hammer\"}.")
            agent = svc.resolve_agent(d.get("agent_id"))
            if not isinstance(d.get("store_id"), str):
                raise HandshakeError("'store_id' is required.")
            if "execute" in d and not isinstance(d["execute"], bool):
                raise HandshakeError("'execute' must be true or false.")
            h._send(200, svc.handshake(agent, d["store_id"], item=d.get("item_id"), amount=d.get("amount"), category=d.get("category"),
                                       title=d.get("title"), execute=bool(d.get("execute")), issuer=issuer))
        elif path == "/api/handshakes":
            agent_id = int(qs["agent_id"]) if (qs.get("agent_id") or "").isdigit() else None
            h._send(200, {"handshakes": svc.list(int(qs.get("limit") or 50), agent_id, qs.get("store_id") or None)})
        elif mp:
            h._send(200, svc.proof(mp.group(1)))
        elif mt:
            agent = svc.resolve_agent(mt.group(1))
            h._send(200, trust.compute(app.store, agent["id"]))
        elif mr:
            d = h._json_in()
            h._send(200, svc.rate(mr.group(1), d.get("rating") if isinstance(d, dict) else None))
        elif path == "/api/merchant/opportunities":
            if not qs.get("store_id"):
                raise HandshakeError("'store_id' is required.")
            h._send(200, svc.opportunities(qs["store_id"]))
        elif path == "/api/shopper/agents":
            if method == "GET":
                h._send(200, {"agents": svc.shoppers(issuer)})
            else:
                d = h._json_in()
                d = d if isinstance(d, dict) else {}
                h._send(200, {"agent": svc.add_shopper(d.get("preset"), d.get("name"), issuer)})
        elif path == "/api/shopper/connect":
            agent = svc.resolve_agent(qs.get("agent_id"))
            h._send(200, {"agent": {"id": agent["id"], "name": agent["name"]}, "key": agent["key"], "base_url": issuer,
                          "tools": ["list_stores", "handshake", "get_pass", "get_budget", "request_spend", "check_spend"]},
                    {"Cache-Control": "no-store"})
        elif path == "/api/merchant/kpis":
            h._send(200, svc.kpis(qs.get("store_id") or None))
        elif path == "/api/shopper/prefs":
            if method == "GET":
                agent = svc.resolve_agent(qs.get("agent_id"))
                h._send(200, svc.prefs(agent))
            else:
                d = h._json_in()
                agent = svc.resolve_agent((d.pop("agent_id", None) if isinstance(d, dict) else None) or qs.get("agent_id"))
                h._send(200, svc.set_prefs(agent, d, issuer))
    except HandshakeError as e:
        h._send(e.http, {"error": str(e)})
    except (SpendError, passport.PassError) as e:
        h._send(getattr(e, "http", 400), {"error": str(e)})
    return True
