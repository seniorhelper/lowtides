"""Outbound webhooks: "Connect Zapier, Make or any webhook".

The owner registers webhook URLs. Lowtides POSTs a signed JSON event to each one when
something needs a person (a purchase waiting for approval) or when money or agents change
state (paid, denied, killed, STOP ALL, loop guard). Zapier's "Catch Hook" can then text,
email or Slack the owner.

Rules this module keeps:
  * Never block or slow a spend decision. emit() only puts the event on a queue and returns;
    worker threads do the network calls. A full queue drops the event (and logs it) rather
    than wait. emit() never raises.
  * Every delivery is signed: X-Lowtides-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">
    with the webhook's own secret, so a receiver can prove the event came from Lowtides.
  * Failed deliveries are retried with backoff, and every attempt is logged.
  * No server-side request forgery: the host is resolved at send time and refused if any
    address is private, loopback, link-local, multicast, reserved or otherwise not public
    (IPv4 and IPv6, in every mode), and the connection goes to the address that was
    checked, so a DNS answer that changes in between (rebinding) can't slip through.
    Redirects are never followed: a 3xx answer is a failed delivery. A private install
    that really wants a webhook on its own network sets LOWTIDES_WEBHOOK_ALLOW_PRIVATE=1
    (ignored in the public demo).

Python standard library only.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse

from . import __version__

EVENTS = {
    "spend.needs_approval": "A purchase is waiting for a person to approve or deny it.",
    "spend.paid": "A purchase was paid (PayPal Payouts, or simulated without PayPal keys).",
    "spend.denied": "A purchase was blocked by the rules or denied by a person.",
    "agent.killed": "An agent was killed: stopped, pending buys denied, holds voided, unclaimed payouts cancelled.",
    "stop_all": "STOP ALL was pressed (by a person or the outside spend guard).",
    "loop_guard.paused": "The loop guard paused an agent that kept repeating itself.",
}
TEST_EVENT = "test.ping"
SIGNATURE_HEADER = "X-Lowtides-Signature"
MAX_HOOKS = 20
DEMO_MAX_HOOKS = 3  # the shared public demo: strangers can't fill it with hooks (reset clears them)
# Hosts a stranger may point the shared public demo at. A private install allows any URL.
DEMO_HOSTS = ("hooks.zapier.com", "hook.us1.make.com", "hook.us2.make.com", "hook.eu1.make.com", "hook.eu2.make.com",
              "webhook.site", "m.pipedream.net", "hooks.slack.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  label TEXT DEFAULT '',
  secret TEXT NOT NULL,
  events TEXT DEFAULT '[]',
  active INTEGER DEFAULT 1,
  created REAL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  webhook_id INTEGER,
  event_id TEXT,
  event TEXT,
  attempt INTEGER,
  ok INTEGER,
  status INTEGER,
  error TEXT,
  ms INTEGER
);
"""


class NotifyError(Exception):
    def __init__(self, message, http=400):
        super().__init__(message)
        self.http = http


def sign(secret, body, ts=None):
    """The signature header value for a raw JSON body (bytes)."""
    ts = int(ts if ts is not None else time.time())
    mac = hmac.new(secret.encode("utf-8"), b"%d." % ts + body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, mac)


def verify(secret, body, header, tolerance=300, now=None):
    """Receiver-side check (also used by tests): True when the header matches the body and
    is no older than `tolerance` seconds."""
    try:
        parts = dict(p.split("=", 1) for p in (header or "").split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, ts), "t=%d,v1=%s" % (ts, parts.get("v1", "")))


def _clean_row(r):
    r = dict(r)
    r["events"] = json.loads(r.get("events") or "[]")
    r["active"] = bool(r.get("active"))
    s = r.pop("secret", "")
    r["secret_hint"] = s[:10] + "..." if s else ""
    return r


def _blocked_ip(ip):
    """True for any address a webhook must never reach: anything that isn't public unicast."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None and _blocked_ip(inner):
            return True
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def resolve_public(host, port):
    """Resolve `host` once and return a (family, sockaddr) to connect to. Raises NotifyError
    when the name doesn't resolve or ANY of its addresses is not a public one."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise NotifyError("Refused: the webhook host name does not resolve.")
    if not infos:
        raise NotifyError("Refused: the webhook host name does not resolve.")
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            raise NotifyError("Refused: the webhook host resolved to an unreadable address.")
        if _blocked_ip(ip):
            raise NotifyError("Refused: that host points at a private, loopback or reserved network address (%s)." % ip)
    return infos[0][0], infos[0][4]


def _private_host(host):
    """True when a host name resolves to a non-public address (kept for callers and tests)."""
    try:
        resolve_public(host, 443)
        return False
    except NotifyError:
        return True


def _connect(family, sockaddr, timeout):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to an address checked beforehand, not to whatever DNS says a moment later."""

    def __init__(self, host, port, addr, timeout):
        super().__init__(host, port, timeout=timeout)
        self._addr = addr

    def connect(self):
        self.sock = _connect(self._addr[0], self._addr[1], self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Same, over TLS: the certificate is still checked against the host NAME."""

    def __init__(self, host, port, addr, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addr = addr

    def connect(self):
        sock = _connect(self._addr[0], self._addr[1], self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Notifier:
    retry_delays = (2, 10, 60)  # seconds to wait before attempts 2, 3 and 4
    timeout = 5                 # seconds per attempt
    workers = 3
    queue_size = 1000

    def __init__(self, store, public_demo=False, base_url=""):
        self.store = store
        self.public_demo = public_demo
        # Only for a private install that wants hooks on its own network; never in the demo.
        self.allow_private = os.environ.get("LOWTIDES_WEBHOOK_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")
        # Where "Approve or deny" links point. A localhost default is useless on a phone, so prefer
        # LOWTIDES_PUBLIC_URL, then the URL Render gives the service.
        local = not base_url or "localhost" in base_url or "127.0.0.1" in base_url
        self.base_url = os.environ.get("LOWTIDES_PUBLIC_URL") or (
            os.environ.get("RENDER_EXTERNAL_URL", base_url) if local else base_url)
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()
        self.q = queue.Queue(maxsize=self.queue_size)
        self._threads = []
        self._lock = threading.Lock()
        self.dropped = 0

    # -- owner settings ------------------------------------------------------------------
    def check_url(self, url):
        url = (url or "").strip()
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("https", "http") or not u.hostname:
            raise NotifyError("The webhook URL must start with https:// (for example a Zapier Catch Hook URL).")
        if len(url) > 1000:
            raise NotifyError("That webhook URL is too long.")
        if self.public_demo:
            host = u.hostname.lower()
            if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in DEMO_HOSTS):
                raise NotifyError("The public demo only sends to Zapier, Make, Pipedream, Slack or webhook.site URLs over https. "
                                  "Run your own copy of Lowtides to use any URL.")
        return url

    def hooks(self):
        rows = [_clean_row(r) for r in self.store.q("SELECT * FROM outbound_webhooks ORDER BY id")]
        if self.public_demo:
            # The demo is shared by strangers: a Catch Hook URL works like a password, so show only its end.
            for r in rows:
                u = urllib.parse.urlparse(r["url"])
                r["url"] = "%s://%s/...%s" % (u.scheme, u.hostname, r["url"][-6:])
        return rows

    def add(self, url, events=None, label=""):
        url = self.check_url(url)
        events = events or []
        if not isinstance(events, list) or any(e not in EVENTS for e in events):
            raise NotifyError("Events must be a list of: %s (or empty for all)." % ", ".join(EVENTS))
        limit = DEMO_MAX_HOOKS if self.public_demo else MAX_HOOKS
        if len(self.hooks()) >= limit:
            raise NotifyError("You can save up to %d webhooks%s." % (
                limit, " in the public demo (Reset demo clears them)" if self.public_demo else ""))
        secret = "whsec_" + secrets.token_hex(24)
        hid = self.store.run("INSERT INTO outbound_webhooks(url,label,secret,events,active,created) VALUES(?,?,?,?,1,?)",
                             (url, str(label or "")[:80], secret, json.dumps(sorted(set(events))), time.time()))
        self.store.audit("webhook.added", str(hid), {"url": url, "events": events})
        hook = self.hook(hid)
        return {"webhook": hook, "secret": secret}

    def hook(self, hid):
        r = self.store.one("SELECT * FROM outbound_webhooks WHERE id=?", (int(hid),))
        return _clean_row(r) if r else None

    def delete(self, hid):
        if not self.hook(hid):
            raise NotifyError("No such webhook.", 404)
        self.store.run("DELETE FROM outbound_webhooks WHERE id=?", (int(hid),))
        self.store.audit("webhook.deleted", str(hid), {})
        return {"ok": True}

    def deliveries(self, limit=50):
        return self.store.q("SELECT * FROM webhook_deliveries ORDER BY id DESC LIMIT ?", (int(limit),))

    def test(self, hid):
        """Send one test.ping right now (the owner pressed Test) and report what happened."""
        row = self.store.one("SELECT * FROM outbound_webhooks WHERE id=?", (int(hid),))
        if not row:
            raise NotifyError("No such webhook.", 404)
        ev = self._envelope(TEST_EVENT, {"message": "Lowtides can reach this webhook."})
        ok, status, err = self._attempt(row, ev, 1)
        return {"ok": ok, "status": status, "error": err, "event": ev}

    # -- sending -------------------------------------------------------------------------
    def _envelope(self, kind, data):
        return {"id": "evt_" + secrets.token_hex(8), "type": kind, "created": int(time.time()),
                "description": EVENTS.get(kind, "Test event"), "dashboard": self.base_url or None, "data": data}

    def emit(self, kind, data):
        """Queue an event for every matching webhook. Returns at once; never raises."""
        try:
            hooks = [r for r in self.store.q("SELECT * FROM outbound_webhooks WHERE active=1")
                     if not json.loads(r["events"] or "[]") or kind in json.loads(r["events"] or "[]")]
            if not hooks:
                return 0
            ev = self._envelope(kind, _jsonable(data))
            self._start()
            n = 0
            for h in hooks:
                try:
                    self.q.put_nowait((h["id"], ev, 1, 0.0))
                    n += 1
                except queue.Full:
                    self.dropped += 1
                    self._log(h["id"], ev, 1, False, None, "Dropped: the delivery queue is full.", 0)
            return n
        except Exception as e:  # a webhook problem must never break a spend decision
            sys.stderr.write("Lowtides webhooks: could not queue %s: %s\n" % (kind, e))
            return 0

    def _start(self):
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            while len(self._threads) < self.workers:
                t = threading.Thread(target=self._work, name="lowtides-webhooks", daemon=True)
                t.start()
                self._threads.append(t)

    def stop(self):
        """Stop the delivery workers (a demo sandbox being thrown away). Queued events are dropped."""
        self._stopped = True
        with self._lock:
            n = len([t for t in self._threads if t.is_alive()])
            self._threads = []
        for _ in range(n):
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass

    def _work(self):
        while True:
            item = self.q.get()
            if item is None or getattr(self, "_stopped", False):
                self.q.task_done()
                return
            hid, ev, attempt, not_before = item
            try:
                wait = not_before - time.time()
                if wait > 0:
                    # Not due yet: put it back for later and look at other work meanwhile.
                    time.sleep(min(wait, 0.5))
                    self._requeue(hid, ev, attempt, not_before)
                    continue
                row = self.store.one("SELECT * FROM outbound_webhooks WHERE id=? AND active=1", (hid,))
                if not row:
                    continue  # deleted while queued
                ok, _status, _err = self._attempt(row, ev, attempt)
                if not ok and attempt <= len(self.retry_delays):
                    self._requeue(hid, ev, attempt + 1, time.time() + self.retry_delays[attempt - 1])
            except Exception as e:
                sys.stderr.write("Lowtides webhooks: worker error: %s\n" % e)
            finally:
                self.q.task_done()

    def _requeue(self, hid, ev, attempt, not_before):
        try:
            self.q.put_nowait((hid, ev, attempt, not_before))
        except queue.Full:
            self.dropped += 1
            self._log(hid, ev, attempt, False, None, "Dropped retry: the delivery queue is full.", 0)

    def _attempt(self, row, ev, attempt):
        body = json.dumps(ev, separators=(",", ":"), sort_keys=True).encode("utf-8")
        start = time.time()
        status, err, ok = None, None, False
        try:
            status = self._post(row["url"], body, {
                "Content-Type": "application/json",
                "User-Agent": "Lowtides-Webhooks/" + __version__,
                "X-Lowtides-Event": ev["type"],
                "X-Lowtides-Delivery": "%s-%d" % (ev["id"], attempt),
                SIGNATURE_HEADER: sign(row["secret"], body)})
            ok = 200 <= status < 300
            if 300 <= status < 400:
                err = ("HTTP %d: the webhook answered with a redirect. Lowtides never follows redirects; "
                       "use the final URL." % status)
            elif not ok:
                err = "HTTP %d" % status
        except Exception as e:
            err = str(e)[:300] or e.__class__.__name__
        self._log(row["id"], ev, attempt, ok, status, err, int((time.time() - start) * 1000))
        return ok, status, err

    def _post(self, url, body, headers):
        """One POST with no redirects followed and no private addresses reached. Returns the status."""
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise NotifyError("The webhook URL must start with https://.")
        port = u.port or (443 if u.scheme == "https" else 80)
        if self.allow_private and not self.public_demo:
            infos = socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)
            addr = (infos[0][0], infos[0][4])
        else:
            addr = resolve_public(u.hostname, port)
        cls = _PinnedHTTPS if u.scheme == "https" else _PinnedHTTP
        conn = cls(u.hostname, port, addr, self.timeout)
        try:
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            r.read(4096)
            return r.status
        finally:
            conn.close()

    def _log(self, hid, ev, attempt, ok, status, err, ms):
        try:
            self.store.run("INSERT INTO webhook_deliveries(ts,webhook_id,event_id,event,attempt,ok,status,error,ms) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), hid, ev["id"], ev["type"], attempt, 1 if ok else 0,
                                                         status, (err or "")[:300], ms))
            self.store.run("DELETE FROM webhook_deliveries WHERE id <= (SELECT MAX(id) FROM webhook_deliveries) - 500")
        except Exception:
            pass

    def drain(self, timeout=10):
        """Tests: wait until the queue is empty (including scheduled retries)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # -- dashboard API ---------------------------------------------------------------------
    def api(self, method, path, data=None):
        """Handles /api/webhooks[...] for the server. Returns (status, body)."""
        parts = path.strip("/").split("/")
        try:
            if method == "GET" and parts == ["api", "webhooks"]:
                return 200, {"webhooks": self.hooks(), "deliveries": self.deliveries(),
                             "events": EVENTS, "signature_header": SIGNATURE_HEADER}
            if method == "POST" and parts == ["api", "webhooks"]:
                d = data or {}
                return 200, self.add(d.get("url"), d.get("events") or [], d.get("label", ""))
            if len(parts) >= 3 and parts[2].isdigit():
                hid = int(parts[2])
                if method == "DELETE" and len(parts) == 3:
                    return 200, self.delete(hid)
                if method == "POST" and parts[3:] == ["test"]:
                    return 200, self.test(hid)
        except NotifyError as e:
            return e.http, {"error": str(e)}
        return 404, {"error": "Unknown endpoint"}


def _jsonable(v):
    try:
        return json.loads(json.dumps(v, default=str))
    except (TypeError, ValueError):
        return {"value": str(v)}


def _summary(row):
    """The fields a person needs to act on a purchase, from a spend row."""
    if not isinstance(row, dict):
        return row
    keep = ("id", "agent_id", "agent_name", "amount", "merchant", "item", "category", "reason", "decision", "status",
            "reasons", "flags", "payout_status", "payout_batch_id", "payout_proof", "simulated", "decided_by", "note")
    return {k: row.get(k) for k in keep if k in row}


def attach(store, public_demo=False, base_url=""):
    """Give a store its notifier (one per store) and return it."""
    n = getattr(store, "notifier", None)
    if n is None:
        n = Notifier(store, public_demo=public_demo, base_url=base_url)
        store.notifier = n
    else:
        n.public_demo = public_demo
    return n


def emit(store, kind, data, agent=None):
    """One-line hook for event points: no-op unless the store has a notifier. Never raises."""
    try:
        n = getattr(store, "notifier", None)
        if n is None:
            return 0
        if kind.startswith("spend."):
            data = _summary(data)
            if isinstance(data, dict) and agent and not data.get("agent_name"):
                data["agent_name"] = agent.get("name")
        if kind == "spend.needs_approval" and isinstance(data, dict) and data.get("id") and n.base_url:
            data["approve_url"] = n.base_url.rstrip("/") + "/#approvals"
        return n.emit(kind, data)
    except Exception:
        return 0
