"""Optional sign-in accounts (off unless configured). The no-login visitor demo never depends on this.

With ACCOUNTS_ENABLED=1 and SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY set, a person
can make an account as a shopper or a store. Supabase Auth keeps the email and the password hash;
Lowtides never stores a password. Signing in swaps the browser's private demo sandbox for a durable
one that belongs to the account: its whole state (rules, Passes, store policy, receipts, purchases)
is saved to one Supabase row after every change and loaded back on the next sign-in, on any device.

Safety rules this file keeps:
  * the service-role key is used only from this server, never sent to a browser;
  * a saved snapshot never contains the server's Pass signing key (pass_keys is emptied first);
  * every failure (Supabase down, bad row, timeout) falls back to the ordinary visitor demo;
  * sign-in and sign-up are rate limited per client address here, on top of Supabase's own limits.
"""
import base64
import collections
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

PREFIX = "acct-"
TABLE = "lowtides_accounts"
ROLES = ("shopper", "store")
MAX_SNAPSHOT = 12 * 1024 * 1024        # characters of base64 accepted from or sent to storage
SAVE_EVERY = 4.0                       # seconds between background saves of changed accounts
CONFIG_KEYS = ("policy", "mode", "outside")
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,180}\.[^@\s]{2,24}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_lock = threading.Lock()
_known = {}                            # sid -> {"email", "role"} for signed-in sandboxes
_dirty = set()                         # sids changed since their last save
_nosave = set()                        # sids whose saved state could not be read: never overwrite it with a blank one
_tries = collections.defaultdict(collections.deque)   # ip -> sign-in / sign-up attempt times
_thread = None


class AccountError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def cfg():
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    anon = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    service = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()   # either name; server only
    on = (os.environ.get("ACCOUNTS_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
    ok = url.startswith("https://") or url.startswith("http://127.0.0.1:")    # plain http only for the local test stub
    return {"enabled": bool(on and ok and anon and service), "url": url, "anon": anon, "service": service}


def enabled():
    return cfg()["enabled"]


def is_account(sid):
    return isinstance(sid, str) and re.fullmatch(PREFIX + "[0-9a-f]{32}", sid) is not None


def sid_for(user_id):
    uid = str(user_id or "").strip().lower()
    if not _UUID.fullmatch(uid):
        raise AccountError("The sign-in service sent an account id Lowtides can't use.", 502)
    return PREFIX + uid.replace("-", "")


def _uid(sid):
    h = sid[len(PREFIX):]
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def _call(method, path, key, body=None, extra=None, timeout=8):
    c = cfg()
    req = urllib.request.Request(c["url"] + path, method=method,
                                 data=None if body is None else json.dumps(body).encode("utf-8"))
    req.add_header("apikey", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_SNAPSHOT + 65536)
            return r.status, (json.loads(raw.decode("utf-8")) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(65536).decode("utf-8"))
        except Exception:
            return e.code, None
    except Exception:
        raise AccountError("The sign-in service did not answer. Please try again in a moment.", 503)


def _limit(ip, most=8, window=600):
    now = time.time()
    with _lock:
        q = _tries[ip or "?"]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= most:
            raise AccountError("Too many sign-in tries from this network. Wait ten minutes and try again.", 429)
        q.append(now)
        if len(_tries) > 5000:
            for k in [k for k, v in _tries.items() if not v or v[-1] < now - window]:
                del _tries[k]


def _creds(d):
    if not isinstance(d, dict):
        raise AccountError("Send JSON like {\"email\": \"you@example.com\", \"password\": \"...\"}.")
    email = str(d.get("email") or "").strip().lower()
    password = d.get("password")
    if not _EMAIL.fullmatch(email):
        raise AccountError("Type a real email address.")
    if not isinstance(password, str) or not (8 <= len(password) <= 200):
        raise AccountError("Use a password of at least 8 characters.")
    return email, password


def _why(body, fallback):
    if isinstance(body, dict):
        for k in ("msg", "error_description", "message", "error"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    return fallback


def _session(body, email):
    """(sid, info) from a Supabase Auth answer that carries a session."""
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    role = (user.get("user_metadata") or {}).get("role")
    info = {"email": str(user.get("email") or email)[:200], "role": role if role in ROLES else "shopper"}
    sid = sid_for(user.get("id"))
    with _lock:
        _known[sid] = info
    return sid, info


def sign_up(d, ip=""):
    if not enabled():
        raise AccountError("Accounts are not turned on for this server.", 404)
    email, password = _creds(d)
    role = d.get("role") if d.get("role") in ROLES else "shopper"
    _limit(ip)
    status, body = _call("POST", "/auth/v1/signup", cfg()["anon"], {"email": email, "password": password, "data": {"role": role}})
    if status >= 400 or not isinstance(body, dict):
        raise AccountError(_why(body, "That account could not be made. If you already have one, sign in instead."), 400)
    if body.get("access_token") and isinstance(body.get("user"), dict):
        sid, info = _session(body, email)
        return {"signed_in": True, "sid": sid, **info}
    return {"signed_in": False, "confirm": True, "email": email, "role": role,
            "said": "Check your email for a confirmation link, then sign in here."}


def sign_in(d, ip=""):
    if not enabled():
        raise AccountError("Accounts are not turned on for this server.", 404)
    email, password = _creds(d)
    _limit(ip)
    status, body = _call("POST", "/auth/v1/token?grant_type=password", cfg()["anon"], {"email": email, "password": password})
    if status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        raise AccountError("That email and password did not match. (If you just signed up, confirm your email first.)", 401)
    sid, info = _session(body, email)
    return {"signed_in": True, "sid": sid, **info}


def who(sid):
    if not is_account(sid):
        return None
    with _lock:
        return dict(_known.get(sid) or {"email": "", "role": "shopper"})


# ---- the account's saved state --------------------------------------------------------------
def snapshot(app):
    """The account's whole sandbox as text: its database (without the server's signing key) and a few settings."""
    fd, path = tempfile.mkstemp(prefix="lt-acct-", suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(path)
        try:
            with app.store.lock:
                app.store.db.commit()
                app.store.db.backup(dest)
            try:
                dest.execute("DELETE FROM pass_keys")      # the server's private signing key never leaves the server
            except sqlite3.Error:
                pass
            dest.commit()
            dest.execute("VACUUM")
            dest.commit()
        finally:
            dest.close()
        with open(path, "rb") as f:
            raw = f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    conf = {k: app.config.data[k] for k in CONFIG_KEYS if k in app.config.data}
    return json.dumps({"v": 1, "db": base64.b64encode(zlib.compress(raw, 6)).decode("ascii"), "config": conf}, separators=(",", ":"))


def load_into(store, text):
    """Fill a brand-new sandbox store from a saved snapshot. Returns the saved settings, or None if unusable."""
    try:
        doc = json.loads(text)
        raw = zlib.decompressobj().decompress(base64.b64decode(doc["db"]), 64 * 1024 * 1024)
        if doc.get("v") != 1 or not raw.startswith(b"SQLite format 3\x00"):
            return None
    except Exception:
        return None
    fd, path = tempfile.mkstemp(prefix="lt-acct-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        src = sqlite3.connect(path)
        try:
            src.execute("SELECT count(*) FROM agents").fetchone()      # a real Lowtides database, or refuse it
            with store.lock:
                src.backup(store.db)
                store.db.commit()
        finally:
            src.close()
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    conf = doc.get("config")
    return conf if isinstance(conf, dict) else {}


def fetch2(sid):
    """(ok, snapshot). ok is False when storage could not be read. No side effects: the caller decides, under its own
    lock, whether this sandbox may be saved (see Sandboxes.get), so two requests at once can't disagree."""
    if not enabled() or not is_account(sid):
        return True, None
    try:
        status, body = _call("GET", "/rest/v1/%s?user_id=eq.%s&select=email,role,snapshot" % (TABLE, urllib.parse.quote(_uid(sid))), cfg()["service"])
    except AccountError:
        return False, None
    if status != 200 or not isinstance(body, list):
        return False, None
    if not body:
        return True, None
    row = body[0] if isinstance(body[0], dict) else {}
    with _lock:
        if sid not in _known:
            _known[sid] = {"email": str(row.get("email") or "")[:200], "role": row.get("role") if row.get("role") in ROLES else "shopper"}
    snap = row.get("snapshot")
    return True, (snap if isinstance(snap, str) and 0 < len(snap) <= MAX_SNAPSHOT else None)


def unblock(sid):
    with _lock:
        _nosave.discard(sid)


def fetch(sid):
    """The saved snapshot text for this account, or None (new account, accounts off, or storage unreachable)."""
    if not enabled() or not is_account(sid):
        return None
    try:
        status, body = _call("GET", "/rest/v1/%s?user_id=eq.%s&select=email,role,snapshot" % (TABLE, urllib.parse.quote(_uid(sid))), cfg()["service"])
    except AccountError:
        status, body = 0, None
    if status != 200 or not isinstance(body, list):
        block(sid)                     # storage did not answer: work in a temporary copy and leave the saved one alone
        return None
    with _lock:
        _nosave.discard(sid)
    if not body:
        return None
    row = body[0] if isinstance(body[0], dict) else {}
    with _lock:
        if sid not in _known:
            _known[sid] = {"email": str(row.get("email") or "")[:200], "role": row.get("role") if row.get("role") in ROLES else "shopper"}
    snap = row.get("snapshot")
    return snap if isinstance(snap, str) and 0 < len(snap) <= MAX_SNAPSHOT else None


def block(sid):
    with _lock:
        _nosave.add(sid)
        _dirty.discard(sid)


def save(sid, app):
    """Write this account's sandbox to storage now. True when stored."""
    if not enabled() or not is_account(sid) or app is None or sid in _nosave:
        return False
    try:
        snap = snapshot(app)
        if len(snap) > MAX_SNAPSHOT:
            return False
        info = who(sid) or {}
        row = {"user_id": _uid(sid), "email": info.get("email", ""), "role": info.get("role", "shopper"), "snapshot": snap,
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        status, _ = _call("POST", "/rest/v1/%s?on_conflict=user_id" % TABLE, cfg()["service"], row,
                          {"Prefer": "resolution=merge-duplicates,return=minimal"}, timeout=15)
        return 200 <= status < 300
    except Exception:
        return False


def touch(sid):
    if is_account(sid):
        with _lock:
            if sid not in _nosave:
                _dirty.add(sid)


def flush(sandboxes, only=None):
    """Save every changed account that is still live (or just `only`). Returns how many were stored."""
    with _lock:
        todo = [s for s in _dirty if only is None or s == only]
        for s in todo:
            _dirty.discard(s)
    done = 0
    for sid in todo:
        with sandboxes.lock:
            app = sandboxes.live.get(sid)
        if app is None:
            continue
        if save(sid, app):
            done += 1
        else:
            touch(sid)                 # try again on the next pass
    return done


def save_dropped(sid, app):
    """A signed-in sandbox is being dropped from memory (idle or crowded out): store it first, off the request path."""
    with _lock:
        was = sid in _dirty and sid not in _nosave
        _dirty.discard(sid)
        _nosave.discard(sid)           # the next sign-in reads storage again
    if was and enabled():
        threading.Thread(target=save, args=(sid, app), daemon=True).start()


def start(sandboxes):
    global _thread
    if _thread or not enabled() or sandboxes is None:
        return

    def loop():
        while True:
            time.sleep(SAVE_EVERY)
            try:
                flush(sandboxes)
            except Exception:
                pass
    _thread = threading.Thread(target=loop, daemon=True, name="lowtides-accounts")
    _thread.start()


# ---- HTTP -----------------------------------------------------------------------------------
def route(h, root, method, path):
    """/api/account/*. Returns True when it answered."""
    if not path.startswith("/api/account"):
        return False
    sb = root.sandboxes
    sid = getattr(h.app, "sandbox_id", None)
    if method == "GET" and path == "/api/account":
        on = enabled() and sb is not None
        me = who(sid) if on else None
        h._send(200, {"enabled": on, "signed_in": bool(me), "email": (me or {}).get("email", ""), "role": (me or {}).get("role", "")})
        return True
    if method != "POST":
        return False
    if not h._same_site():
        h._send(403, {"error": "Cross-site request refused."})
        return True
    if sb is None or not enabled():
        h._send(404, {"error": "Accounts are not turned on for this server."})
        return True
    try:
        if path in ("/api/account/signup", "/api/account/signin"):
            out = (sign_up if path.endswith("signup") else sign_in)(h._json_in(), h._client_ip())
            new = out.pop("sid", None)
            if new:
                h._new_cookie = new + "." + sb._sig(new)      # this browser now works in the account's own sandbox
                out["go"] = "/merchants/" if out.get("role") == "store" else "/shop/"
            h._send(200, out)
            return True
        if path == "/api/account/signout":
            h._json_in()
            if is_account(sid):
                touch(sid)
                flush(sb, only=sid)
                h._new_cookie = sb.new_id()                   # back to a fresh private visitor demo
            h._send(200, {"signed_in": False})
            return True
    except AccountError as e:
        h._send(e.status, {"error": str(e)})
        return True
    return False
