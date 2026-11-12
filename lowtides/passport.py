"""Lowtides Pass: a signed spending permission an AI agent carries.

Merchants and other agents can't tell whether an AI agent is allowed to spend. A
Lowtides Pass answers that: a compact, signed credential that says who the agent is,
who governs it (this Lowtides server) and its live limits. The shape follows the idea
of the Agent Payments Protocol (AP2) "Intent Mandate". It is AP2-aligned, not an
official AP2 integration.

Format (JWS compact serialization, like a JWT):
    base64url(header) "." base64url(payload) "." base64url(Ed25519 signature)
    header = {"alg": "EdDSA", "typ": "lowtides-pass+jws", "kid": "<key id>"}

Anyone can check the signature offline with the public key at
/.well-known/lowtides-pass-keys.json. Checking online (POST /v1/pass/verify) also says
whether the pass was revoked: Stop all, STOP, a loop-guard pause, an owner's
revoke or a rules change all revoke it at once.

The verify call never records a purchase. It runs the same checks a real spend request
would and reports what would happen. The one thing it may write is that a pass is now
revoked, so revocation sticks (a restarted agent or restored limits can't revive it).
"""
import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import statistics
import time
import urllib.parse

from . import ed25519
from .guard import start_of_today
from .spend import (DEFAULT_RULES, DUP_WINDOW, INJECTION, UNUSUAL_MIN_HISTORY, UNUSUAL_MULTIPLE, SpendError, agent_rules,
                    describe_rules, money, validate_rules)

ALG = "EdDSA"
TYP = "lowtides-pass+jws"
VERSION = 1
DEFAULT_TTL_HOURS = 24.0
MAX_TOKEN = 16 * 1024
B64 = re.compile(r"^[A-Za-z0-9_-]*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pass_keys (
  kid TEXT PRIMARY KEY,
  seed TEXT NOT NULL,
  public TEXT NOT NULL,
  created REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS passes (
  jti TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  issued REAL NOT NULL,
  exp REAL NOT NULL,
  rules_hash TEXT NOT NULL,
  token TEXT NOT NULL,
  revoked_ts REAL,
  revoked_by TEXT DEFAULT '',
  revoked_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS passes_agent ON passes(agent_id, issued);
CREATE TABLE IF NOT EXISTS pass_holds (
  agent_id INTEGER PRIMARY KEY,
  since REAL NOT NULL
);
"""



def _shopper_mode(store, agent_id):
    """The shopper mode set in the Handshake prefs (autopilot / assist / off), or None when there is none."""
    try:
        r = store.one("SELECT mode FROM hs_prefs WHERE agent_id=?", (agent_id,))
    except Exception:       # the Handshake tables do not exist yet
        return None
    return r["mode"] if r else None

class PassError(Exception):
    def __init__(self, message, http=400):
        super().__init__(message)
        self.http = http


def b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64d(s):
    if not isinstance(s, str) or not B64.match(s) or len(s) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _usd(x):
    return None if x is None else {"currency": "USD", "value": "%.2f" % float(x)}


def plain_from_limits(lim):
    """The limits written in a pass, in plain English."""
    if not isinstance(lim, dict):
        return []
    try:
        rules = validate_rules({k: v for k, v in lim.items() if k in DEFAULT_RULES})
        lines = [x for x in describe_rules(rules) if not re.search(r"repl|prompt|AI call|bigger than usual", x)]
        if lim.get("ai_daily_budget") is not None:
            lines.append("AI usage up to %s a day." % money(lim["ai_daily_budget"]))
        return lines
    except (SpendError, TypeError, ValueError):
        return []


class PassBook:
    """Issues, stores, verifies and revokes Lowtides Passes for one Lowtides server."""

    def __init__(self, store, paypal=None, ttl_hours=None, key=None, on_issue=None):
        """`key` (public demo sandboxes only): a pass_keys row to sign with instead of making
        a new key, so every sandbox signs with the one key the server publishes.
        `on_issue(jti)` is told about each new pass, so a cookie-less verify call can find
        the sandbox that issued it."""
        self.store = store
        self.paypal = paypal
        self.on_issue = on_issue
        with store.lock:
            store.db.executescript(SCHEMA)
            if key is not None:
                store.db.execute("INSERT OR IGNORE INTO pass_keys(kid,seed,public,created,active) VALUES(?,?,?,?,1)",
                                 (key["kid"], key["seed"], key["public"], key["created"]))
            store.db.commit()
        if ttl_hours is None:
            try:
                ttl_hours = float(os.environ.get("LOWTIDES_PASS_TTL_HOURS") or DEFAULT_TTL_HOURS)
            except ValueError:
                ttl_hours = DEFAULT_TTL_HOURS
        self.ttl = max(300.0, min(30 * 86400.0, float(ttl_hours) * 3600))
        self._key()

    # ---- signing key ---------------------------------------------------------------
    def _key(self):
        """The active signing key, made once and kept in the database."""
        with self.store.lock:
            row = self.store.one("SELECT * FROM pass_keys WHERE active=1 ORDER BY created DESC LIMIT 1")
            if row:
                return row
            seed = ed25519.new_seed()
            pub = ed25519.public_key(seed)
            kid = "lt-pass-" + hashlib.sha256(pub).hexdigest()[:16]
            self.store.run("INSERT OR REPLACE INTO pass_keys(kid,seed,public,created,active) VALUES(?,?,?,?,1)",
                           (kid, seed.hex(), pub.hex(), time.time()))
            return self.store.one("SELECT * FROM pass_keys WHERE kid=?", (kid,))

    def jwks(self):
        keys = []
        for k in self.store.q("SELECT kid, public, active FROM pass_keys ORDER BY created DESC"):
            keys.append({"kty": "OKP", "crv": "Ed25519", "x": b64e(bytes.fromhex(k["public"])), "kid": k["kid"],
                         "alg": ALG, "use": "sig", "status": "active" if k["active"] else "retired"})
        return {"keys": keys, "typ": TYP,
                "about": "Public keys for checking Lowtides Pass signatures. A pass is header.payload.signature "
                         "(base64url, Ed25519). Check revocation with POST /v1/pass/verify."}

    # ---- what a pass says ----------------------------------------------------------
    @staticmethod
    def limits(agent, rules=None):
        r = rules or agent_rules(agent)
        return {"currency": "USD",
                "max_per_purchase": r["max_per_purchase"],
                "daily_purchase_limit": r["daily_purchase_limit"],
                "approval_over": r["approval_over"],
                "daily_total_limit": r["daily_total_limit"],
                "ai_daily_budget": round(float(agent["daily_budget"]), 2),
                "allowed_categories": r["allowed_categories"],
                "blocked_categories": r["blocked_categories"],
                "blocked_merchants": r["blocked_merchants"],
                "new_merchant_needs_approval": r["new_merchant_needs_approval"],
                "hours": r["hours"],
                "max_purchases_per_hour": r["max_purchases_per_hour"]}

    def rules_hash(self, agent):
        return hashlib.sha256(_canon({"name": agent["name"], "limits": self.limits(agent)})).hexdigest()

    def plain(self, agent, rules=None):
        return plain_from_limits(self.limits(agent, rules))

    def payload(self, agent, issuer, now=None):
        now = time.time() if now is None else now
        exp = now + self.ttl
        rules = agent_rules(agent)
        lim = self.limits(agent, rules)
        plain = self.plain(agent, rules)
        iss = issuer.rstrip("/")
        return {
            "v": VERSION,
            "iss": iss,
            "sub": "agent:%d" % agent["id"],
            "agent": {"id": agent["id"], "name": agent["name"]},
            "governed_by": {"name": "Lowtides", "url": iss, "kill_switch": True},
            "iat": int(now),
            "exp": int(exp),
            "jti": "pass_" + secrets.token_hex(12),
            "limits": lim,
            "ap2": {
                "aligned_with": "AP2 Intent Mandate (AP2-aligned, not an official AP2 integration)",
                "mandate": "intent",
                "natural_language_description": " ".join(plain),
                "user_cart_confirmation_required": lim["approval_over"] <= 0,
                "user_cart_confirmation_required_over": _usd(lim["approval_over"]),
                "merchants": None,
                "skus": None,
                "requires_refundability": False,
                "intent_expiry": _iso(exp),
                "constraints": {
                    "max_amount_per_purchase": _usd(lim["max_per_purchase"]),
                    "max_amount_per_day": _usd(lim["daily_purchase_limit"]),
                    "max_combined_ai_and_purchases_per_day": _usd(lim["daily_total_limit"]),
                    "allowed_categories": lim["allowed_categories"],
                    "blocked_categories": lim["blocked_categories"],
                    "blocked_merchants": lim["blocked_merchants"],
                    "new_merchants_need_approval": lim["new_merchant_needs_approval"],
                    "allowed_hours": lim["hours"],
                    "max_purchases_per_hour": lim["max_purchases_per_hour"],
                },
            },
            "rules_hash": self.rules_hash(agent),
            "status_url": iss + "/v1/pass/verify",
            "verify_page": iss + "/pass/",
        }

    # ---- issuing ---------------------------------------------------------------------
    def sign(self, payload):
        k = self._key()
        header = {"alg": ALG, "typ": TYP, "kid": k["kid"]}
        signing_input = b64e(_canon(header)) + "." + b64e(_canon(payload))
        sig = ed25519.sign(bytes.fromhex(k["seed"]), signing_input.encode("ascii"))
        return signing_input + "." + b64e(sig)

    def frozen(self):
        """True while the person's stop word has everything on hold (lowtides/muzzle.py). No Pass is issued meanwhile."""
        try:
            row = self.store.one("SELECT v FROM mz_state WHERE k='frozen'")
            return bool(row and json.loads(row["v"]).get("on"))
        except Exception:
            return False

    def issue(self, agent, issuer):
        if agent["status"] != "on":
            raise PassError("%s is not running, so it can't get a Lowtides Pass. Start it first." % agent["name"], 409)
        if self.frozen():
            raise PassError("Everything is frozen by your stop word. Release the freeze first.", 409)
        now = time.time()
        p = self.payload(agent, issuer, now)
        token = self.sign(p)
        with self.store.lock:
            # One live pass per agent: a new one replaces the old.
            self._revoke_rows(agent["id"], "lowtides", "Replaced by a newer pass.")
            self.store.run("DELETE FROM pass_holds WHERE agent_id=?", (agent["id"],))
            self.store.run("INSERT INTO passes(jti,agent_id,issued,exp,rules_hash,token) VALUES(?,?,?,?,?,?)",
                           (p["jti"], agent["id"], now, float(p["exp"]), p["rules_hash"], token))
        self.store.audit("pass.issued", p["jti"], {"agent_id": agent["id"], "agent": agent["name"], "exp": p["exp"],
                                                   "rules_hash": p["rules_hash"]})
        if self.on_issue:
            self.on_issue(p["jti"])
        return token, p

    def latest(self, agent_id):
        return self.store.one("SELECT * FROM passes WHERE agent_id=? ORDER BY issued DESC, rowid DESC LIMIT 1", (agent_id,))

    def held(self, agent_id):
        return bool(self.store.one("SELECT 1 AS x FROM pass_holds WHERE agent_id=?", (agent_id,)))

    def current(self, agent, issuer, issue=True):
        """The agent's pass and its live status. Issues a fresh one when the last is
        missing, expired, revoked or out of date, unless the agent is stopped or the
        owner revoked it on purpose."""
        row = self.latest(agent["id"])
        state = self.check(row["token"]) if row else None
        if issue and (not state or not state["valid"]) and agent["status"] == "on" and not self.held(agent["id"]) and not self.frozen():
            token, _ = self.issue(agent, issuer)
            state = self.check(token)
            row = self.latest(agent["id"])
        if not row:
            return None
        return dict(state, token=row["token"], owner_revoked=self.held(agent["id"]))

    # ---- revoking --------------------------------------------------------------------
    def _revoke_rows(self, agent_id, by, reason):
        return self.store.run("UPDATE passes SET revoked_ts=?, revoked_by=?, revoked_reason=? WHERE agent_id=? AND revoked_ts IS NULL",
                              (time.time(), by, reason, agent_id))

    def revoke(self, agent_id, by="owner", reason="The owner revoked this pass."):
        """Revoke every pass of this agent and don't hand out a new one until the owner
        issues one again (a running agent would otherwise just fetch a fresh pass)."""
        with self.store.lock:
            n = self.store.db.execute("UPDATE passes SET revoked_ts=?, revoked_by=?, revoked_reason=? "
                                      "WHERE agent_id=? AND revoked_ts IS NULL",
                                      (time.time(), by, reason, agent_id)).rowcount
            self.store.db.execute("INSERT OR REPLACE INTO pass_holds(agent_id,since) VALUES(?,?)", (agent_id, time.time()))
            self.store.db.commit()
        self.store.audit("pass.revoked", str(agent_id), {"by": by, "reason": reason, "passes": n})
        self.store.event(agent_id, "control", "Lowtides Pass revoked by the %s. Merchants checking it now see REVOKED." % by)
        return n

    def _revoked_reason(self, agent, row):
        """Why this pass no longer counts, or None. Reads live state, so Stop all, STOP and
        STOP ALL revoke passes without the stop switch knowing passes exist."""
        if not agent:
            return "The agent this pass belongs to no longer exists."
        if row is None:
            return "This Lowtides server has no record of this pass (it may come from a demo that was reset)."
        if row["revoked_ts"]:
            return row["revoked_reason"] or "This pass was revoked."
        aid, since = agent["id"], row["issued"]
        if agent["status"] == "stopped":
            return "You pressed Stop all, so this Pass is revoked."
        if agent["status"] == "paused":
            return "The loop guard paused this agent, so its pass is revoked."
        if self.store.one("SELECT 1 AS x FROM audit WHERE kind='agent.killed' AND ref=? AND ts>=? LIMIT 1", (str(aid), since)):
            return "The owner pressed Stop all after this pass was issued."
        if self.store.one("SELECT 1 AS x FROM events WHERE agent_id=? AND ts>=? AND (kind='loop' OR "
                          "(kind='control' AND message LIKE 'STOP pressed%')) LIMIT 1", (aid, since)):
            return "This agent was stopped after this pass was issued."
        if row["rules_hash"] != self.rules_hash(agent):
            return "This agent's limits changed after this pass was issued. Ask the agent for a fresh pass."
        return None

    # ---- verifying ---------------------------------------------------------------------
    def decode(self, token):
        """(header, payload, signature_ok, problem). Raises PassError if it isn't a pass at all."""
        if not isinstance(token, str) or not token.strip():
            raise PassError("Paste a Lowtides Pass: three parts separated by dots.")
        token = token.strip()
        if len(token) > MAX_TOKEN:
            raise PassError("That is too long to be a Lowtides Pass.")
        parts = token.split(".")
        if len(parts) != 3:
            raise PassError("A Lowtides Pass has three parts separated by dots. This has %d." % len(parts))
        try:
            header = json.loads(b64d(parts[0]).decode("utf-8"))
            payload = json.loads(b64d(parts[1]).decode("utf-8"))
            sig = b64d(parts[2])
        except (ValueError, UnicodeDecodeError):
            raise PassError("That isn't a readable Lowtides Pass (bad base64url or JSON).")
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise PassError("That isn't a readable Lowtides Pass.")
        if header.get("alg") != ALG:
            return header, payload, False, "Wrong signing method '%s'. Lowtides Passes are signed with EdDSA (Ed25519) only." % header.get("alg")
        if header.get("typ") != TYP:
            return header, payload, False, "This isn't a Lowtides Pass (type '%s')." % header.get("typ")
        k = self.store.one("SELECT * FROM pass_keys WHERE kid=?", (str(header.get("kid") or ""),))
        if not k:
            return header, payload, False, "Unknown signing key '%s'. This pass wasn't signed by this Lowtides server." % header.get("kid")
        ok = ed25519.verify(bytes.fromhex(k["public"]), (parts[0] + "." + parts[1]).encode("ascii"), sig)
        return header, payload, ok, None if ok else "The signature doesn't match. This pass was changed or forged."

    def check(self, token, amount=None, merchant=None, category=None, now=None, gone=None):
        """The pass's live status. `gone` (public demo only): the sandbox that issued this
        pass no longer exists; report it as expired with that message, and look nothing up
        here (this store's agent ids belong to someone else)."""
        now = time.time() if now is None else now
        header, p, sig_ok, problem = self.decode(token)
        out = {"valid": False, "signature_ok": sig_ok, "expired": False, "revoked": False, "revoked_reason": None,
               "agent": None, "issuer": None, "issued_at": None, "expires_at": None, "jti": None, "kid": header.get("kid"),
               "limits": None, "limits_plain": [], "ap2": None, "would_allow": "denied", "reasons": [],
               "checked_at": _iso(now)}
        if not sig_ok:
            out["reasons"] = [problem]
            return out
        try:
            aid = int(str(p.get("sub", "")).split(":", 1)[1])
            exp, iat = float(p["exp"]), float(p["iat"])
        except (IndexError, KeyError, TypeError, ValueError):
            out["reasons"] = ["The pass is signed but missing its agent or dates."]
            return out
        out.update(issuer=p.get("iss"), issued_at=_iso(iat), expires_at=_iso(exp), jti=p.get("jti"),
                   limits=p.get("limits"), limits_plain=plain_from_limits(p.get("limits")), ap2=p.get("ap2"))
        if gone:
            out.update(agent={"id": aid, "name": (p.get("agent") or {}).get("name"), "status": "gone"},
                       expired=True, gone=True, reasons=["EXPIRED: " + gone])
            return out
        agent = self.store.agent(aid)
        row = self.store.one("SELECT * FROM passes WHERE jti=?", (str(p.get("jti", "")),))
        if row and row["agent_id"] != aid:
            row = None
        out["agent"] = {"id": aid, "name": (p.get("agent") or {}).get("name"),
                        "status": agent["status"] if agent else "deleted"}
        out["expired"] = now >= exp
        reason = self._revoked_reason(agent, row)
        if reason:
            out.update(revoked=True, revoked_reason=reason)
            if row and not row["revoked_ts"]:
                # Make it stick: restarting the agent later must not bring an old pass back.
                self.store.run("UPDATE passes SET revoked_ts=?, revoked_by='lowtides', revoked_reason=? WHERE jti=? AND revoked_ts IS NULL",
                               (now, reason, row["jti"]))
        out["valid"] = not out["expired"] and not out["revoked"]
        if out["revoked"]:
            out["reasons"] = ["REVOKED: " + reason]
        elif out["expired"]:
            out["reasons"] = ["EXPIRED: this pass ran out at %s. Ask the agent for a fresh one." % out["expires_at"]]
        if not out["valid"]:
            return out
        if amount is None and not merchant and not category:
            out["would_allow"] = None
            out["reasons"] = ["Valid. Add an amount to check a purchase."]
            return out
        out["would_allow"], out["reasons"] = self.would_allow(agent, amount, merchant, category)
        mode = _shopper_mode(self.store, agent["id"])      # Consent Shopping: the owner's Autopilot / Assist / Off switch
        if mode == "off":
            out["would_allow"] = "denied"
            out["reasons"] = ["OFF: the owner switched this shopper Off, so it can't buy anything until it is switched back on."] + out["reasons"]
        elif mode == "assist" and out["would_allow"] == "allowed":
            out["would_allow"] = "needs_owner_approval"
            out["reasons"] = out["reasons"] + ["Assist mode: the owner approves every purchase."]
        return out

    def would_allow(self, agent, amount=None, merchant=None, category=None):
        """What POST /v1/spend would decide right now, without recording anything.
        Mirrors SpendGovernor.request() check for check (tests compare the two)."""
        if amount is not None:
            try:
                if isinstance(amount, bool):
                    raise ValueError
                amount = round(float(amount), 2)
            except (TypeError, ValueError):
                raise PassError("'amount' must be a number of dollars.")
            if amount <= 0 or amount > 1_000_000:
                raise PassError("'amount' must be more than $0 and at most $1,000,000.")
        merchant = str(merchant or "").strip()[:120]
        category = str(category or "").strip().lower()[:60]
        rules = agent_rules(agent)
        st = self.store
        denied, ask, ok, notes = [], [], [], []
        known = st.merchant(merchant) if merchant else None
        if not category and known:
            category = known["category"]
        aid, today = agent["id"], start_of_today()

        if agent["status"] == "stopped":
            denied.append("This agent is stopped. Nothing can be bought until a person starts it again.")
        elif agent["status"] == "paused":
            denied.append("This agent is paused by the loop guard. Review it, then press Resume.")
        if merchant and merchant.lower() in rules["blocked_merchants"]:
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
        if per_hour and st.purchase_count_since(aid, time.time() - 3600) >= per_hour:
            denied.append("Too many purchase requests: the limit is %d an hour. That pace looks like a runaway agent." % per_hour)
        if amount is not None:
            if amount > rules["max_per_purchase"]:
                denied.append("%s is over the %s per-purchase limit." % (money(amount), money(rules["max_per_purchase"])))
            bought = st.purchases_since(aid, today)
            if bought + amount > rules["daily_purchase_limit"]:
                denied.append("That would make %s of purchases today, over the %s daily limit."
                              % (money(bought + amount), money(rules["daily_purchase_limit"])))
            if rules["daily_total_limit"] is not None:
                tokens = st.spend_since(aid, today)
                if tokens + bought + amount > rules["daily_total_limit"]:
                    denied.append("AI usage (%s) plus purchases (%s) plus this would pass the %s combined daily limit."
                                  % (money(tokens), money(bought), money(rules["daily_total_limit"])))
            if agent["use_wallet"] and st.balance(aid) < amount:
                denied.append("The agent's PayPal-funded wallet has %s, not enough for %s." % (money(st.balance(aid)), money(amount)))
        else:
            notes.append("No amount given, so the dollar limits weren't checked.")
        if not merchant:
            notes.append("No merchant given, so the merchant rules weren't checked.")

        if not denied:
            if amount is not None and amount > rules["approval_over"]:
                ask.append("%s is over the %s auto-approve limit." % (money(amount), money(rules["approval_over"])))
            if merchant and not known and rules["new_merchant_needs_approval"]:
                ask.append("%s isn't on your merchant list yet. New payees need a person's OK." % merchant)
            if known and not known["paypal_email"] and self.paypal is not None and self.paypal.configured:
                ask.append("%s has no PayPal payee on file, so it can't be paid automatically." % merchant)
            if merchant and INJECTION.search(merchant):
                ask.append("The request contains text that reads like instructions to the agent, a common sign of prompt injection.")
            if merchant and amount is not None and st.one(
                    "SELECT id FROM spend_requests WHERE agent_id=? AND merchant=? COLLATE NOCASE AND amount=? "
                    "AND ts>=? AND status IN ('paid','approved','pending') LIMIT 1", (aid, merchant, amount, time.time() - DUP_WINDOW)):
                ask.append("The same purchase (%s at %s) was requested in the last 10 minutes. It may be a double charge."
                           % (money(amount), merchant))
            if amount is not None:
                past = [r["amount"] for r in st.q("SELECT amount FROM spend_requests WHERE agent_id=? AND status='paid' "
                                                  "ORDER BY ts DESC LIMIT 50", (aid,))]
                if len(past) >= UNUSUAL_MIN_HISTORY:
                    typical = statistics.median(past)
                    if typical > 0 and amount > typical * UNUSUAL_MULTIPLE:
                        ask.append("%s is more than %dx this agent's typical purchase (%s)." % (money(amount), int(UNUSUAL_MULTIPLE), money(typical)))
            if not ask:
                ok.append("Within the %s auto-approve limit and today's %s budget." % (money(rules["approval_over"]),
                                                                                     money(rules["daily_purchase_limit"])))
                if known:
                    ok.append("%s is a known merchant." % merchant)
        verdict = "denied" if denied else ("needs_owner_approval" if ask else "allowed")
        return verdict, (denied or ask or ok) + notes


# ---- HTTP -----------------------------------------------------------------------------
def book(app):
    """The app's PassBook, made on first use (keeps server.py changes to a couple of lines)."""
    b = getattr(app, "passes", None)
    if b is None:
        # A demo sandbox signs with the shared root key and reports its passes (sandbox.py).
        b = app.passes = PassBook(app.store, app.paypal, key=getattr(app, "pass_key", None),
                                  on_issue=getattr(app, "on_pass", None))
    return b


GONE = ("This pass came from a demo sandbox that has since been reset or closed (an idle demo is "
        "cleared after a while). Open the demo again and ask the agent for a fresh pass.")


def _peek_jti(token):
    """The jti a token claims, unchecked (only used to find which demo sandbox to ask)."""
    try:
        parts = token.strip().split(".")
        p = json.loads(b64d(parts[1]).decode("utf-8")) if len(parts) == 3 and len(token) <= MAX_TOKEN else None
        return str(p.get("jti") or "") if isinstance(p, dict) else ""
    except (AttributeError, ValueError, UnicodeDecodeError):
        return ""


def verifier(app, token):
    """(App whose passes to check, gone message or None) for a verify call.
    Outside the public demo it is always this app. In the demo a pass is checked against the
    sandbox that issued it, whichever sandbox (or none) the caller's cookie or key belongs to;
    a pass whose sandbox is gone gets a friendly EXPIRED instead of someone else's agent."""
    root = getattr(app, "demo_root", None) or app
    sb = getattr(root, "sandboxes", None)
    if sb is None:
        return app, None
    book(root)  # the shared key and pass tables exist before any lookup
    jti = _peek_jti(token) if isinstance(token, str) else ""
    found = sb.by_pass(jti) if jti else None
    if found is not None:
        return found, None
    if not jti or root.store.one("SELECT 1 AS x FROM passes WHERE jti=?", (jti,)):
        return root, None  # the shared demo's own pass (or not a pass: the normal errors apply)
    return root, GONE


LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def issuer_url(handler, app):
    """The `iss` a new pass carries. A pass is shown to strangers as proof of who issued it,
    so request headers can't choose it: LOWTIDES_PUBLIC_URL wins; otherwise the Host header
    counts only when it is the configured paypal.public_base_url host or a localhost name.
    X-Forwarded-Host and X-Forwarded-Proto are never trusted."""
    env = os.environ.get("LOWTIDES_PUBLIC_URL", "").strip()
    if env:
        return env.rstrip("/")
    base = str(app.config.paypal.get("public_base_url") or "http://localhost:8787").rstrip("/")
    host = (handler.headers.get("Host") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.\-]+(:\d{1,5})?|\[[0-9a-f:]+\](:\d{1,5})?", host):
        return base
    b = urllib.parse.urlparse(base)
    if b.netloc.lower() == host:
        return base
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if name in LOCAL_HOSTS:
        return "http://" + host
    return base


PUBLIC = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def _agent_view(state):
    if not state:
        return None
    keep = ("valid", "revoked", "revoked_reason", "expired", "issued_at", "expires_at", "jti", "kid", "limits_plain",
            "token", "owner_revoked", "agent", "issuer")
    return {k: state.get(k) for k in keep}


def route(h, app, method, path):
    """Handle the pass endpoints. Returns True when it answered the request."""
    if method == "GET" and path == "/.well-known/lowtides-pass-keys.json":
        # In the demo every sandbox signs with the root's key, so the root's list is the list.
        h._send(200, book(getattr(app, "demo_root", None) or app).jwks(), dict(PUBLIC, **{"Cache-Control": "public, max-age=300"}))
        return True
    if method == "POST" and path == "/v1/pass/verify":
        d = h._json_in()
        if not isinstance(d, dict):
            h._send(400, {"error": "Send JSON like {\"pass\": \"...\", \"amount\": 12.5}."}, PUBLIC)
            return True
        token = d.get("pass") or d.get("token")
        try:
            target, gone = verifier(app, token)
            out = book(target).check(token, amount=d.get("amount"), merchant=d.get("merchant"),
                                     category=d.get("category"), gone=gone)
        except (PassError, SpendError) as e:
            h._send(400, {"error": str(e), "valid": False}, PUBLIC)
            return True
        h._send(200, out, PUBLIC)
        return True
    if method == "GET" and path == "/v1/pass":
        agent = h._agent_or_401()
        if not agent:
            return True
        state = _agent_view(book(app).current(agent, issuer_url(h, app)))
        if not state or not state["valid"]:
            why = (state or {}).get("revoked_reason") or "%s is not running, so it has no valid Lowtides Pass." % agent["name"]
            h._send(409, {"error": why, "valid": False, "pass": (state or {}).get("token"),
                      "revoked": bool(state and state["revoked"]), "expires_at": (state or {}).get("expires_at")})
            return True
        out = {k: v for k, v in state.items() if k != "token"}
        out.update({"pass": state["token"],
                    "how_to_use": "Show this pass to a merchant or another agent before buying. They can check it offline "
                                  "with %s/.well-known/lowtides-pass-keys.json, or online (including revocation) with "
                                  "POST %s/v1/pass/verify or the page %s/pass/." % ((state["issuer"],) * 3)})
        h._send(200, out)
        return True
    m = re.fullmatch(r"/api/agents/(\d+)/pass(/revoke)?", path)
    if m and method in ("GET", "POST"):
        if not h._api_allowed(write=method == "POST"):
            return True
        aid = int(m.group(1))
        agent = app.store.agent(aid)
        if not agent:
            h._send(404, {"error": "No such agent."})
            return True
        b = book(app)
        if method == "POST":
            h._json_in()
        if m.group(2):
            if method != "POST":
                h._send(405, {"error": "Use POST to revoke."})
                return True
            n = b.revoke(aid)
            h._send(200, {"ok": True, "revoked": n, "agent_status": agent["status"],
                          "pass": _agent_view(b.current(agent, issuer_url(h, app), issue=False))})
            return True
        try:
            if method == "POST":
                b.issue(agent, issuer_url(h, app))
            state = b.current(agent, issuer_url(h, app))
        except PassError as e:
            h._send(e.http, {"error": str(e)})
            return True
        h._send(200, {"pass": _agent_view(state), "agent_status": agent["status"]})
        return True
    return False
