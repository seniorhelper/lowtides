"""HTTP server: the agent-facing API plus the dashboard API. Python standard library only.

Agent endpoints (drop-in):
  POST /v1/chat/completions   OpenAI-compatible
  POST /v1/messages           Anthropic-compatible
  GET  /v1/models
  POST /v1/spend, GET /v1/spend/{id}, GET /v1/budget   ask before buying (see docs/SPEND-API.md)
Point your agent's base URL at this server and use its Lowtides key as the API key.
"""
import collections
import datetime
import hmac
import copy
import json
import math
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import APP_NAME, __version__, notify
from .config import ROOT, Config
from .demo import DEMO_NAME, run_demo, run_loop_demo
from .engine import Engine
from .money import Money, MoneyError
from .openapi import SPEC as OPENAPI_SPEC
from .outside import OutsideGuard
from .paypal import PayPal, PayPalError
from .pricing import money
from .quote import quote as make_quote
from .guard import start_of_today
from .receipts import ledger_csv, receipt
from .spend import SpendError, SpendGovernor, agent_rules, compile_rules, describe_rules, validate_rules
from .sandbox import COOKIE as DEMO_COOKIE, INTERNAL_HEADER, TooManySandboxes
from .store import Store
from .vault import Vault, VaultError

UI_DIR = os.path.join(ROOT, "ui")
MAX_BODY = 20 * 1024 * 1024
DEMO_MAX_BODY = 1024 * 1024  # the public demo never needs more; keeps strangers from tying up memory
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")


class LengthRequired(Exception):
    pass


def _reject_constant(name):
    raise ValueError("Numbers must be real numbers (%s is not allowed)." % name)


def _finite_float(text):
    x = float(text)
    if not math.isfinite(x):
        raise ValueError("Numbers must be real numbers (%s is too large)." % text[:40])
    return x


def _no_nonfinite(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _no_nonfinite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_no_nonfinite(x) for x in v]
    return v


def _dumps_strict(body):
    """Strict JSON (no NaN/Infinity, which browsers cannot parse). Should a non-finite
    number ever slip into state, send it as null instead of breaking the page for everyone."""
    try:
        return json.dumps(body, allow_nan=False)
    except ValueError:
        sys.stderr.write("Lowtides: a response held a non-finite number; sent as null.\n")
        return json.dumps(_no_nonfinite(body), allow_nan=False)


def safe_equal(a, b):
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def key_env_ok(name):
    """A connection may only read API-key variables, never PayPal or Lowtides secrets."""
    import re
    return name == "" or bool(re.fullmatch(r"[A-Z][A-Z0-9_]*_API_KEY", name))


class App:
    def __init__(self, config, store, paypal=None, providers=None, config_path=None, public_demo=None):
        self.config = config
        self.store = store
        self.engine = Engine(config, store, providers)
        pp_cfg = config.paypal
        self.paypal = paypal or PayPal(config.secret(pp_cfg.get("client_id_env")), config.secret(pp_cfg.get("client_secret_env")),
                                       env=pp_cfg.get("env", "sandbox"), currency=pp_cfg.get("currency", "USD"))
        self.money = Money(config, store, self.paypal)
        # Public demo: no password, PayPal sandbox only, simulated models only, resettable.
        self.public_demo = (os.environ.get("LOWTIDES_PUBLIC_DEMO", "").lower() in ("1", "true", "yes")
                            if public_demo is None else bool(public_demo))
        # PayPal on file per agent. Simulated (and labeled so) without keys or in the public
        # demo, where strangers shouldn't be sent through a real PayPal vault approval.
        self.vault = Vault(store, self.paypal, base_url=lambda: self.money.base_url, simulate=self.public_demo)
        self.spend = SpendGovernor(store, self.paypal, self.money, vault=self.vault)
        self.notify = notify.attach(store, public_demo=self.public_demo, base_url=pp_cfg.get("public_base_url", ""))  # outbound webhooks
        self.engine.sim_only = self.public_demo
        self.demo_policy = copy.deepcopy(config.data.get("policy", {})) if self.public_demo else None
        self.demo_mode = config.data.get("mode", "home")
        self.self_url = None
        self.claude_compiles = collections.deque()
        self.admin_token = os.environ.get("LOWTIDES_ADMIN_TOKEN", "")
        self.config_path = config_path or os.environ.get("LOWTIDES_CONFIG") or os.path.join(ROOT, "config.json")
        self.save_lock = threading.Lock()
        self.loopback = True  # set by serve(); a loopback server only answers to localhost names
        # Watches the provider's real bill, including scripts that never touch Lowtides.
        self.outside = OutsideGuard(config, store, stop_all=self.stop_all, demo=self.public_demo)
        # Public demo: each browser gets its own sandbox (see sandbox.py); this App is the
        # shared one that cookie-less callers (curl, the scenario CLI) still use.
        self.sandbox_id = None
        self.seed_headers = None
        self.on_reset = None
        self.sandboxes = self.limiter = None
        if self.public_demo and public_demo is None:
            from .sandbox import RateLimiter, Sandboxes
            self.sandboxes = Sandboxes(self)
            self.limiter = RateLimiter()

    def stop_all(self, by="STOP ALL"):
        totals = {"denied": 0, "holds_voided": 0, "payouts_cancelled": 0, "tokens_revoked": 0, "revoke_failed": 0}
        problems = []
        for a in self.store.agents():
            r = self.spend.kill(a["id"], by=by)
            for k in ("denied", "holds_voided", "payouts_cancelled", "tokens_revoked"):
                totals[k] += r[k]
            if r["paypal_on_file"].get("status") == "revoke_failed":
                totals["revoke_failed"] += 1
            problems += [p if a["name"] in p else "%s: %s" % (a["name"], p) for p in r["problems"]]
        who = "STOP ALL pressed" if by == "STOP ALL" else by + " pressed STOP ALL"
        self.store.event(None, "control", "%s. Every agent is stopped, %d pending purchases denied, %d holds voided, "
                                          "%d payouts cancelled, %d saved PayPal payment method%s revoked%s."
                         % (who, totals["denied"], totals["holds_voided"], totals["payouts_cancelled"], totals["tokens_revoked"],
                            "" if totals["tokens_revoked"] == 1 else "s",
                            (" (%d could not be revoked)" % totals["revoke_failed"]) if totals["revoke_failed"] else ""))
        totals["problems"] = problems
        notify.emit(self.store, "stop_all", dict(totals, by=by))
        return totals

    def reset_demo(self, seed_story=True):
        """Public demo only: wipe everything and replay the runaway-agent story, stopping
        before the KILL so visitors can approve, deny or kill for themselves. The in-page
        replay passes seed_story=False: it tells the story itself on a clean slate."""
        if not self.public_demo:
            raise ValueError("Reset is only available in the public demo.")
        with self.store.lock:
            for table in ("spend_requests", "merchants", "audit", "events", "ledger", "memory", "tasks",
                          "wallets", "wallet_tx", "topups", "shadow", "vault_tokens", "agents",
                          "outbound_webhooks", "webhook_deliveries"):
                self.store.db.execute("DELETE FROM " + table)
            self.store.db.commit()
        if self.on_reset:  # the deleted agents' keys no longer route here
            self.on_reset()
        if self.demo_policy is not None:  # undo any visitor's policy changes
            self.config.data["policy"] = copy.deepcopy(self.demo_policy)
            self.config.data["mode"] = self.demo_mode
        old, self.outside = self.outside, OutsideGuard(self.config, self.store, stop_all=self.stop_all, demo=True)
        if old._thread:  # fresh simulated bill; keep watching
            old.stop()
            self.outside.start()
        if self.self_url and seed_story:
            from .scenario import seed
            try:
                seed(self.self_url, headers=self.seed_headers)
            except Exception as e:  # the demo still works empty; say why
                self.store.event(None, "control", "Demo story could not load: %s" % e)
        self.store.event(None, "control", "Public demo reset. The runaway-agent story is loaded: try Approvals, Rules and KILL.")

    def save_config(self):
        with self.save_lock:
            tmp = self.config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config.data, f, indent=2)
            os.replace(tmp, self.config_path)

    # ---- dashboard state -------------------------------------------------
    def protected(self, since):
        """Money Lowtides kept from leaving since `since`, from real records only:
        purchases denied (by rules, a person or the kill switch), injection-flagged
        purchases still held, payouts pulled back, and loop-guard stops. Purchases
        waiting on a person are reported separately, not counted as stopped."""
        rows = self.store.q("SELECT amount, status, flags FROM spend_requests WHERE ts>=?", (since,))
        out = {"stopped": 0.0, "scam_held": 0, "blocked": 0, "cancelled": 0, "loops": 0,
               "waiting": 0, "waiting_amount": 0.0}
        for r in rows:
            amt = float(r["amount"] or 0)
            try:
                injected = "possible_injection" in json.loads(r["flags"] or "[]")
            except ValueError:
                injected = False
            status = r["status"]
            if status == "denied":
                out["stopped"] += amt
                out["scam_held" if injected else "blocked"] += 1
            elif status == "pending" and injected:
                out["stopped"] += amt
                out["scam_held"] += 1
            elif status == "pending":
                out["waiting"] += 1
                out["waiting_amount"] += amt
            elif status == "cancelled":
                out["stopped"] += amt
                out["cancelled"] += 1
        out["loops"] = int(self.store.one("SELECT COUNT(*) AS n FROM events WHERE kind='loop' AND ts>=?", (since,))["n"])
        out["stopped"] = round(out["stopped"], 2)
        out["waiting_amount"] = round(out["waiting_amount"], 2)
        return out

    def state(self):
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        agents = []
        for a in self.store.agents():
            spent = self.store.spend_since(a["id"], today)
            budget = float(a["daily_budget"])
            ratio = spent / budget if budget else 1
            light = "red" if a["status"] != "on" or ratio >= 1 else ("yellow" if ratio >= 0.8 else "green")
            agents.append(dict(a, spent_today=round(spent, 6), light=light, balance=round(self.store.balance(a["id"]), 6),
                               simulated=a["name"] == DEMO_NAME, paypal_on_file=self.vault.status(a["id"])))
        week = time.time() - 7 * 86400
        return {
            "app": self.config.data.get("app_name", APP_NAME),
            "version": __version__,
            "mode": self.config.data.get("mode", "home"),
            "agents": agents,
            "today": receipt(self.store, since=today),
            "protected": self.protected(today),
            "week": receipt(self.store, since=week),
            "events": self.store.events(30),
            "clients": self.store.clients(),
            "invoices": self.store.invoices(),
            "tasks": self.store.tasks(limit=50),
            "shadows": self.store.shadows(20),
            "config": self.config.public(),
            "admin_protected": bool(self.admin_token) and not self.public_demo,
            "public_demo": self.public_demo,
            "paypal_ready": self.paypal.configured,
            "vault_simulated": self.vault.simulated,
            "outside": self.outside.status(),
        }


AGENT_PATHS = ("/chat/completions", "/messages")


def make_handler(app):
    root = app

    class Handler(BaseHTTPRequestHandler):
        server_version = "Lowtides/" + __version__
        protocol_version = "HTTP/1.1"
        app = root  # per request: the caller's demo sandbox in public-demo mode (see _route)
        _new_cookie = None

        def log_message(self, fmt, *args):
            if os.environ.get("LOWTIDES_QUIET"):
                return
            sys.stderr.write("%s %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), fmt % args))

        # ---- io helpers ----------------------------------------------------
        def _body(self):
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                raise LengthRequired()
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("Bad Content-Length")
            if n < 0:
                raise ValueError("Bad Content-Length")
            if n > (DEMO_MAX_BODY if self.app.public_demo else MAX_BODY):
                raise ValueError("Request too large")
            raw = self.rfile.read(n) if n else b""
            self._consumed = True
            return raw

        def _json_in(self):
            raw = self._body()
            if not raw:
                return {}
            # Python's json accepts NaN/Infinity and turns 1e999 into inf; refuse both so no
            # non-finite number can reach saved state (it would break JSON.parse in every browser).
            return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)

        def _send(self, status, body, headers=None, ctype="application/json"):
            data = body if isinstance(body, (bytes, bytearray)) else (
                _dumps_strict(body).encode("utf-8") if ctype == "application/json" else str(body).encode("utf-8"))
            if not getattr(self, "_consumed", True) and not self.headers.get("Transfer-Encoding"):
                # Read a small unread body before answering. Closing a socket with unread data makes
                # Windows send a reset, and the client can lose this response (e.g. a 403) entirely.
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if 0 < n <= 1024 * 1024:
                    self.rfile.read(n)
                    self._consumed = True
                elif n == 0:
                    self._consumed = True
            self.send_response(status)
            if not getattr(self, "_consumed", True) and (self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding")):
                # We answered without reading the request body, so this connection can't be reused safely.
                self.close_connection = True
                self.send_header("Connection", "close")
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("json") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, str(v))
            if self._new_cookie:
                self.send_header("Set-Cookie", self._demo_cookie(self._new_cookie))
            self.end_headers()
            self.wfile.write(data)

        # ---- public demo: which sandbox is this request for? -------------------
        def _cookie(self, name):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return urllib.parse.unquote(v)
            return ""

        def _client_ip(self):
            # Behind a proxy (Render) every request arrives from the proxy, so the visitor's
            # address comes from a header, and only where a proxy sets it. Render's edge puts
            # the visitor first in X-Forwarded-For and appends its own hops after it, and its
            # CDN sets CF-Connecting-IP / True-Client-IP. Only rate limits use this address.
            hdr = os.environ.get("LOWTIDES_CLIENT_IP_HEADER", "")
            if hdr and self.headers.get(hdr):
                return self.headers.get(hdr).split(",")[0].strip()
            if os.environ.get("RENDER") or os.environ.get("LOWTIDES_TRUST_PROXY"):
                for h in ("CF-Connecting-IP", "True-Client-IP"):
                    if self.headers.get(h):
                        return self.headers.get(h).strip()
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[0].strip()
            return self.client_address[0]

        def _demo_cookie(self, value):
            https = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
            return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400%s" % (DEMO_COOKIE, value, "; Secure" if https else "")

        def _too_many(self, msg):
            self._send(429, {"error": msg}, {"Retry-After": "5"})

        def _route(self):
            """Pick the App this request works on. Outside the public demo it is always the
            one App. In the demo: dashboard calls go to the cookie's sandbox, agent calls to
            the sandbox that owns the key, everything else (pages, /health) stays shared.
            Returns False if an error was already sent."""
            self.app, self._new_cookie = root, None
            sb = root.sandboxes
            if sb is None:
                return True
            path = self._path()[0]
            internal = (sb.internal_ok(self.headers.get(INTERNAL_HEADER))
                        and self.client_address[0] in ("127.0.0.1", "::1"))
            ip = self._client_ip()
            agentish = path.startswith("/v1/") or path in AGENT_PATHS
            if (self.command == "POST" and not internal and (agentish or path.startswith("/api/"))
                    and not root.limiter.allow(ip)):
                self._too_many("Easy there: that's a lot of requests at once. Wait a few seconds and try again.")
                return False
            if agentish:
                self.app = sb.by_key(self._agent_key()) or root
                return True
            sid = sb.parse(self._cookie(DEMO_COOKIE))
            if path.startswith("/api/") or path == "/paypal/return":
                if (not sid and not internal and path.startswith("/api/") and self._same_site()
                        and (self.headers.get("Sec-Fetch-Site") or "").lower() == "same-origin"):
                    # No (or a stale) cookie, say after a restart: give this browser its own
                    # sandbox now rather than drop it into the shared demo.
                    self._new_cookie = sb.new_id()
                    sid = sb.parse(self._new_cookie)
                if sid and (path == "/paypal/return" or self._same_site()):
                    try:
                        found = sb.get(sid, ip, wait=not internal, create=not internal)
                    except TooManySandboxes:
                        # Lots of new demos from one network (or a proxy hiding addresses):
                        # share the common demo rather than turn a visitor away.
                        found = root
                    if found is None:
                        self._send(410, {"error": "This demo sandbox is gone. Reload the page for a fresh one."})
                        return False
                    self.app = found
                return True
            if self.command == "GET" and not sid and (path.endswith("/") or path.endswith(".html")):
                self._new_cookie = sb.new_id()  # the sandbox itself is built on the first dashboard call
            return True

        def _redirect(self, where):
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _admin_ok(self):
            if not self.app.admin_token or self.app.public_demo:
                return True
            given = self.headers.get("X-Admin-Token", "")
            if not given:
                cookie = self.headers.get("Cookie", "")
                for part in cookie.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "lt_admin":
                        given = urllib.parse.unquote(v)
            return safe_equal(given, self.app.admin_token)

        def _same_site(self):
            """Blocks other websites (and DNS-rebinding tricks) from driving the dashboard API
            through your browser. The dashboard itself is same-origin, so it always passes."""
            host = (self.headers.get("Host") or "").strip().lower()
            if self.app.loopback and host.rsplit(":", 1)[0].strip("[]") not in LOCAL_NAMES:
                return False
            origin = (self.headers.get("Origin") or "").strip().lower()
            if origin and origin != "null":
                if urllib.parse.urlparse(origin).netloc != host:
                    return False
            elif origin == "null":
                return False
            return True

        def _api_allowed(self, write):
            if not self._same_site():
                self._send(403, {"error": "Cross-site request refused."})
                return False
            if write and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                self._send(415, {"error": "Send JSON with Content-Type: application/json."})
                return False
            if not self._admin_ok():
                self._send(401, {"error": "Admin password required."})
                return False
            return True

        def _agent_key(self):
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return self.headers.get("x-api-key", "").strip()

        # ---- streaming (SSE) ---------------------------------------------------
        def _sse(self, api, resp, extra, req=None):
            req = req or {}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            for k, v in extra.items():
                self.send_header(k, str(v))
            self.end_headers()
            self.close_connection = True

            def emit(event, data):
                chunk = ""
                if event:
                    chunk += "event: %s\n" % event
                chunk += "data: %s\n\n" % (data if isinstance(data, str) else json.dumps(data))
                self.wfile.write(chunk.encode("utf-8"))
            if api == "openai":
                choice = (resp.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                base = {"id": resp.get("id"), "object": "chat.completion.chunk", "created": resp.get("created", int(time.time())),
                        "model": resp.get("model")}
                delta = {"role": "assistant"}
                if msg.get("content"):
                    delta["content"] = msg["content"]
                if msg.get("tool_calls"):
                    delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(msg["tool_calls"])]
                emit(None, dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]))
                emit(None, dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]))
                # OpenAI only sends the usage chunk (with empty choices) when asked to.
                if resp.get("usage") and (req.get("stream_options") or {}).get("include_usage"):
                    emit(None, dict(base, choices=[], usage=resp["usage"]))
                emit(None, "[DONE]")
            else:
                usage = resp.get("usage") or {}
                start = dict(resp, content=[], stop_reason=None)
                start["usage"] = dict(usage, output_tokens=0)
                emit("message_start", {"type": "message_start", "message": start})
                for i, block in enumerate(resp.get("content", [])):
                    if block.get("type") == "text":
                        emit("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                        emit("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block.get("text", "")}})
                    elif block.get("type") == "tool_use":
                        emit("content_block_start", {"type": "content_block_start", "index": i, "content_block": dict(block, input={})})
                        emit("content_block_delta", {"type": "content_block_delta", "index": i,
                                                     "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}))}})
                    else:
                        emit("content_block_start", {"type": "content_block_start", "index": i, "content_block": block})
                    emit("content_block_stop", {"type": "content_block_stop", "index": i})
                emit("message_delta", {"type": "message_delta", "delta": {"stop_reason": resp.get("stop_reason"), "stop_sequence": None},
                                       "usage": {"output_tokens": usage.get("output_tokens", 0)}})
                emit("message_stop", {"type": "message_stop"})
            self.wfile.flush()

        # ---- routing ----------------------------------------------------------------
        def do_OPTIONS(self):
            # Browser-based agents may call the agent API (it needs a Lowtides key anyway).
            # The dashboard API never allows cross-site calls.
            path, _ = self._path()
            self.send_response(204)
            if not path.startswith("/api/"):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            try:
                if not self._route():
                    return
                self._get()
            except Exception as e:  # keep the server alive, report plainly
                self._send(500, {"error": "Server error: %s" % e})

        def do_POST(self):
            self._consumed = False
            try:
                if not self._route():
                    return
                self._post()
            except LengthRequired:
                self._send(411, {"error": "Send a Content-Length (chunked bodies are not supported)."})
            except json.JSONDecodeError:
                self._send(400, {"error": "Body must be valid JSON."})
            except (MoneyError, ValueError) as e:
                self._send(400, {"error": str(e)})
            except PayPalError as e:
                self._send(502, {"error": "PayPal: %s" % e, "issue": e.issue})
            except Exception as e:
                self._send(500, {"error": "Server error: %s" % e})

        def do_DELETE(self):
            self._consumed = False
            try:
                if not self._route():
                    return
                self._delete()
            except Exception as e:
                self._send(500, {"error": "Server error: %s" % e})

        def _path(self):
            u = urllib.parse.urlparse(self.path)
            return u.path, dict(urllib.parse.parse_qsl(u.query))

        def _get(self):
            path, qs = self._path()
            if path == "/health":
                return self._send(200, {"ok": True, "version": __version__})
            if path == "/openapi.json":
                return self._send(200, OPENAPI_SPEC, {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
            if path == "/v1/models":
                data = [{"id": n, "object": "model", "owned_by": m.get("provider")} for n, m in self.app.config.models.items()]
                return self._send(200, {"object": "list", "data": data})
            if path == "/v1/budget" or path.startswith("/v1/spend/"):
                return self._spend_api("GET", path)
            if path == "/paypal/return":
                order_id = qs.get("token", "")
                kind = qs.get("kind", "")
                try:
                    if kind == "topup":
                        self.app.money.topup_finish(order_id)
                        return self._redirect("/#wallet?paid=1")
                    if kind == "hold":
                        self.app.money.hold_finish(order_id)
                        return self._redirect("/#tasks?held=1")
                    if kind == "vault":
                        # PayPal sends the approved setup token back (approval_token_id; token on some flows).
                        self.app.vault.connect_finish(qs.get("approval_token_id") or order_id)
                        return self._redirect("/#wallet?vault=1")
                except (MoneyError, PayPalError, VaultError) as e:
                    return self._redirect("/#error?msg=" + urllib.parse.quote(str(e)))
                return self._redirect("/")
            if path == "/paypal/cancel":
                return self._redirect("/#home?canceled=1")
            if path.startswith("/api/"):
                if not self._api_allowed(write=False):
                    return
                return self._api_get(path, qs)
            return self._static(path)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                return self._send(404, {"error": "Not found"})
            full = os.path.join(UI_DIR, rel)
            if os.path.isdir(full):
                if not path.endswith("/"):
                    return self._redirect(path + "/")
                full = os.path.join(full, "index.html")
            if not os.path.isfile(full):
                return self._send(404, {"error": "Not found"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
            self._send(200, data, {"Cache-Control": "no-cache"}, ctype=ctype)

        def _api_get(self, path, qs):
            if path.startswith("/api/webhooks"):
                return self._send(*self.app.notify.api("GET", path))
            if path == "/api/state":
                return self._send(200, self.app.state())
            if path == "/api/ledger":
                aid = int(qs["agent"]) if qs.get("agent") else None
                return self._send(200, {"rows": self.app.store.ledger(aid, limit=int(qs.get("limit", 300)))})
            if path == "/api/ledger.csv":
                aid = int(qs["agent"]) if qs.get("agent") else None
                data = ledger_csv(self.app.store.ledger(aid, limit=100000)).encode("utf-8")
                return self._send(200, data, {"Content-Disposition": "attachment; filename=lowtides-ledger.csv"}, ctype="text/csv")
            if path == "/api/receipt":
                days = float(qs.get("days", 30))
                aid = int(qs["agent"]) if qs.get("agent") else None
                return self._send(200, receipt(self.app.store, agent_id=aid, since=time.time() - days * 86400))
            if path.startswith("/api/wallet/"):
                if not path.rsplit("/", 1)[1].isdigit():
                    return self._send(400, {"error": "Give an agent id, like /api/wallet/1."})
                aid = int(path.rsplit("/", 1)[1])
                return self._send(200, {"balance": self.app.store.balance(aid), "history": self.app.store.wallet_history(aid)})
            if path == "/api/quote":
                q = make_quote(self.app.store, self.app.config, int(qs.get("agent", 0)), label=qs.get("label", ""),
                               description=qs.get("description", ""), model=qs.get("model"))
                return self._send(200, q)
            if path == "/api/spend":
                status = qs.get("status", "all")
                return self._send(200, {"requests": self.app.store.spends(status=None if status == "all" else status,
                                                                      limit=min(int(qs.get("limit", 100)), 1000))})
            if path.startswith("/api/spend/") and path.endswith("/evidence"):
                try:
                    pack = self.app.spend.evidence(path.split("/")[3])
                except SpendError as e:
                    return self._send(e.http, {"error": str(e)})
                data = json.dumps(pack, indent=2).encode("utf-8")
                return self._send(200, data, {"Content-Disposition": "attachment; filename=lowtides-evidence-%s.json" % pack["request"]["id"]})
            if path == "/api/merchants":
                return self._send(200, {"merchants": self.app.store.merchants()})
            if path == "/api/forecast":
                return self._send(200, self.app.spend.forecast())
            if path == "/api/outside":
                return self._send(200, self.app.outside.status())
            if path == "/api/audit/verify":
                return self._send(200, self.app.store.audit_verify())
            if path.startswith("/api/agents/") and path.endswith("/vault"):
                aid = int(path.split("/")[3])
                if not self.app.store.agent(aid):
                    return self._send(404, {"error": "No such agent."})
                return self._send(200, self.app.vault.status(aid))
            if path.startswith("/api/agents/") and path.endswith("/rules"):
                a = self.app.store.agent(int(path.split("/")[3]))
                if not a:
                    return self._send(404, {"error": "No such agent."})
                r = agent_rules(a)
                return self._send(200, {"rules": r, "plain": describe_rules(r)})
            return self._send(404, {"error": "Unknown endpoint"})

        def _agent_or_401(self):
            agent = self.app.store.agent_by_key(self._agent_key()) if self._agent_key() else None
            if not agent:
                self._send(401, {"error": "Unknown or missing Lowtides agent key."})
            return agent

        def _spend_api(self, method, path):
            """Agent-facing purchase API: /v1/spend, /v1/spend/{id}, /v1/budget."""
            agent = self._agent_or_401()
            if not agent:
                return
            try:
                if method == "POST" and path == "/v1/spend":
                    return self._send(200, self.app.spend.request(agent, self._json_in()))
                if method == "GET" and path.startswith("/v1/spend/"):
                    row = self.app.store.spend(path.rsplit("/", 1)[1])
                    if not row or row["agent_id"] != agent["id"]:
                        return self._send(404, {"error": "No such spend request for this agent."})
                    return self._send(200, self.app.spend.refresh_payout(row["id"]))
                if method == "GET" and path == "/v1/budget":
                    r = agent_rules(agent)
                    today = start_of_today()
                    bought = self.app.store.purchases_since(agent["id"], today)
                    return self._send(200, {
                        "agent": agent["name"], "status": agent["status"],
                        "tokens": {"daily_budget": agent["daily_budget"], "spent_today": round(self.app.store.spend_since(agent["id"], today), 6)},
                        "purchases": {"daily_limit": r["daily_purchase_limit"], "spent_today": round(bought, 2),
                                      "remaining_today": round(max(0.0, r["daily_purchase_limit"] - bought), 2),
                                      "auto_approve_up_to": r["approval_over"], "max_per_purchase": r["max_per_purchase"]},
                        "wallet_balance": round(self.app.store.balance(agent["id"]), 2) if agent["use_wallet"] else None,
                        "rules": r, "rules_in_plain_english": describe_rules(r)})
            except SpendError as e:
                return self._send(e.http, {"error": str(e)})
            return self._send(404, {"error": "Unknown endpoint"})

        def _post(self):
            path, qs = self._path()
            if path == "/v1/spend":
                return self._spend_api("POST", path)
            if path in ("/v1/chat/completions", "/v1/messages", "/chat/completions", "/messages"):
                api = "openai" if path.endswith("chat/completions") else "anthropic"
                body = self._json_in()
                if self.app.public_demo and (self.app.config.model(body.get("model") or "") or {}).get("provider") != "simulator":
                    return self._send(403, {"error": {"message": "This public demo runs on the built-in simulated models only "
                                                                 "(sim-small, sim-medium, sim-large), so nobody can spend real AI credit.",
                                                      "type": "public_demo"}})
                hdrs = {k: v for k, v in self.headers.items()}
                status, resp, extra = self.app.engine.handle(api, body, self._agent_key(), hdrs)
                if status == 200 and body.get("stream"):
                    return self._sse(api, resp, extra, body)
                return self._send(status, resp, extra)
            if path == "/webhooks/paypal":
                raw = self._body()
                event = json.loads(raw.decode("utf-8") or "{}")
                wid = self.app.config.secret(self.app.config.paypal.get("webhook_id_env", ""))
                if not wid:
                    return self._send(400, {"error": "Set PAYPAL_WEBHOOK_ID to accept webhooks."})
                if not self.app.paypal.verify_webhook(dict(self.headers.items()), event, wid, raw=raw):
                    return self._send(400, {"error": "Webhook signature did not verify."})
                eid = event.get("id", "")
                if eid and self.app.store.webhook_seen(eid):
                    return self._send(200, {"ok": True, "duplicate": True})
                try:
                    result = self.app.money.on_webhook(event)
                except Exception as e:
                    # Not marked as seen, so PayPal retries it later.
                    return self._send(500, {"error": "Could not process event yet: %s" % e})
                if eid:
                    self.app.store.webhook_done(eid)
                return self._send(200, {"ok": True, "result": result})
            if path == "/api/login":
                if not self._same_site():
                    return self._send(403, {"error": "Cross-site request refused."})
                data = self._json_in()
                if self.app.admin_token and not safe_equal(str(data.get("token", "")), self.app.admin_token):
                    return self._send(401, {"error": "Wrong password."})
                cookie = "lt_admin=%s; Path=/; HttpOnly; SameSite=Strict" % urllib.parse.quote(str(data.get("token", "")))
                return self._send(200, {"ok": True}, {"Set-Cookie": cookie})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "Unknown endpoint"})
            if not self._api_allowed(write=True):
                return
            return self._api_post(path, self._json_in())

        def _api_post(self, path, d):
            s = self.app.store
            parts = path.strip("/").split("/")
            if path.startswith("/api/webhooks"):
                return self._send(*self.app.notify.api("POST", path, d))
            if self.app.public_demo:
                if path in ("/api/providers", "/api/models"):
                    return self._send(403, {"error": "Changing model connections is turned off in the public demo."})
                if path == "/api/demo/reset":
                    self.app.reset_demo(seed_story=not d.get("empty"))
                    return self._send(200, {"ok": True})
            if path == "/api/agents":
                name = (d.get("name") or "").strip()
                if not name:
                    raise ValueError("Give your agent a name.")
                daily = float(d.get("daily_budget", self.app.config.policy.get("default_daily_budget", 5)))
                task = float(d.get("task_budget", self.app.config.policy.get("default_task_budget", 1)))
                if daily <= 0 or task <= 0:
                    raise ValueError("Budgets must be more than $0.")
                aid = s.add_agent(name, daily, task, client_id=d.get("client_id") or None, use_wallet=bool(d.get("use_wallet")))
                return self._send(200, {"agent": s.agent(aid)})
            if len(parts) == 4 and parts[1] == "spend" and parts[3] in ("approve", "deny"):
                try:
                    if parts[3] == "approve":
                        return self._send(200, self.app.spend.approve(parts[2]))
                    return self._send(200, self.app.spend.deny(parts[2], note=str(d.get("note", ""))))
                except SpendError as e:
                    return self._send(e.http, {"error": str(e)})
            if path == "/api/rules/compile":
                key = os.environ.get("ANTHROPIC_API_KEY", "")
                if key and self.app.public_demo:
                    # Cap the owner's Claude spend from strangers: 30 translations an hour, then offline.
                    now = time.time()
                    while self.app.claude_compiles and self.app.claude_compiles[0] < now - 3600:
                        self.app.claude_compiles.popleft()
                    if len(self.app.claude_compiles) >= 30:
                        key = ""
                    else:
                        self.app.claude_compiles.append(now)
                try:
                    return self._send(200, compile_rules(d.get("text", ""), api_key=key,
                                                         model=self.app.config.policy.get("rules_model", "claude-haiku-5-5")))
                except SpendError as e:
                    return self._send(e.http, {"error": str(e)})
            if path == "/api/merchants":
                name = (d.get("name") or "").strip()
                email = (d.get("paypal_email") or "").strip()
                if not name:
                    raise ValueError("Give the merchant a name.")
                if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    raise ValueError("That PayPal email doesn't look right.")
                m = s.add_merchant(name[:120], email[:200], (d.get("category") or "")[:60])
                s.audit("merchant.saved", name[:120], {"paypal_email": email, "category": m["category"]})
                return self._send(200, {"merchant": m})
            if len(parts) == 5 and parts[1] == "agents" and parts[3] == "vault":
                return self._vault_post(int(parts[2]), parts[4], d)
            if len(parts) == 4 and parts[1] == "agents" and parts[3] in ("rules", "kill"):
                aid = int(parts[2])
                if not s.agent(aid):
                    return self._send(404, {"error": "No such agent."})
                try:
                    if parts[3] == "kill":
                        out = self.app.spend.kill(aid)
                        notify.emit(s, "agent.killed", dict(out, agent_id=aid, agent_name=s.agent(aid)["name"]))
                        return self._send(200, out)
                    rules = validate_rules(d.get("rules") if isinstance(d.get("rules"), dict) else {})
                except SpendError as e:
                    return self._send(e.http, {"error": str(e)})
                s.update_agent(aid, rules=json.dumps(rules))
                s.audit("rules.saved", str(aid), {"rules": rules})
                s.event(aid, "control", "Spending rules updated: " + " ".join(describe_rules(rules)))
                return self._send(200, {"rules": rules, "plain": describe_rules(rules)})
            if len(parts) == 4 and parts[1] == "agents":
                aid = int(parts[2])
                action = parts[3]
                if not s.agent(aid):
                    return self._send(404, {"error": "No such agent."})
                if action in ("stop", "start", "resume"):
                    s.update_agent(aid, status="stopped" if action == "stop" else "on")
                    s.event(aid, "control", "%s pressed %s." % ("STOP" if action == "stop" else "Owner", action.upper()))
                elif action == "update":
                    fields = {}
                    for k in ("name", "daily_budget", "task_budget", "client_id", "use_wallet"):
                        if k in d:
                            fields[k] = d[k]
                    for k in ("daily_budget", "task_budget"):
                        if k in fields and float(fields[k]) <= 0:
                            raise ValueError("Budgets must be more than $0.")
                    if "use_wallet" in fields:
                        fields["use_wallet"] = 1 if fields["use_wallet"] else 0
                    s.update_agent(aid, **fields)
                else:
                    return self._send(404, {"error": "Unknown action"})
                return self._send(200, {"agent": s.agent(aid)})
            if path == "/api/stop-all":
                return self._send(200, dict(self.app.stop_all(), ok=True))
            if path.startswith("/api/outside/"):
                return self._outside_post(parts[2] if len(parts) == 3 else "", d)
            if path == "/api/policy":
                if self.app.public_demo and any(k != "mode" for k in d):
                    return self._send(403, {"error": "Changing the governor's settings is turned off in the public demo. Run your own copy to tune them."})
                self.app.config.update_policy(d)
                if "mode" in d and d["mode"] in ("home", "business"):
                    self.app.config.data["mode"] = d["mode"]
                self.app.save_config()
                return self._send(200, {"policy": self.app.config.policy, "mode": self.app.config.data.get("mode")})
            if path == "/api/models":
                name = (d.pop("name", "") or "").strip()
                if not name:
                    raise ValueError("Model name is required.")
                spec = {k: d[k] for k in ("provider", "tier", "family", "input", "output", "cache_write", "cache_read", "note") if k in d}
                for k in ("input", "output", "cache_write", "cache_read"):
                    if k in spec:
                        spec[k] = float(spec[k])
                if "tier" in spec:
                    spec["tier"] = int(spec["tier"])
                self.app.config.upsert_model(name, spec)
                self.app.save_config()
                return self._send(200, {"models": self.app.config.models})
            if path == "/api/providers":
                name = (d.get("name") or "").strip()
                if not name or d.get("type") not in ("openai", "anthropic"):
                    raise ValueError("A provider needs a name and type 'openai' or 'anthropic'.")
                if not key_env_ok(d.get("api_key_env", "")):
                    raise ValueError("The key setting must name an environment variable ending in _API_KEY (for example GROQ_API_KEY), or be empty.")
                self.app.config.data["providers"][name] = {"type": d["type"], "base_url": d.get("base_url", ""),
                                                      "api_key_env": d.get("api_key_env", "")}
                self.app.engine.providers._cache.pop(name, None)
                self.app.save_config()
                return self._send(200, {"providers": self.app.config.public()["providers"]})
            if path == "/api/tasks":
                res = self.app.money.new_task(int(d["agent_id"]), d.get("label", ""), d.get("description", ""),
                                         budget=float(d["budget"]) if d.get("budget") else None, model=d.get("model"))
                return self._send(200, res)
            if len(parts) == 4 and parts[1] == "tasks":
                tid, action = parts[2], parts[3]
                if action == "hold":
                    return self._send(200, self.app.money.hold_start(tid))
                if action == "settle":
                    return self._send(200, self.app.money.settle(tid))
                if action == "cancel":
                    return self._send(200, self.app.money.cancel_hold(tid))
                if action == "done":
                    return self._send(200, self.app.money.finish_without_charge(tid))
                return self._send(404, {"error": "Unknown action"})
            if path == "/api/wallet/topup":
                return self._send(200, self.app.money.topup_start(int(d["agent_id"]), float(d.get("amount", 10))))
            if path == "/api/clients":
                name = (d.get("name") or "").strip()
                if not name:
                    raise ValueError("Client name is required.")
                cid = s.add_client(name, d.get("email", ""), float(d.get("markup_percent", self.app.config.business.get("default_markup_percent", 0))))
                return self._send(200, {"client": s.client(cid)})
            if len(parts) == 4 and parts[1] == "clients" and parts[3] == "invoice":
                cid = int(parts[2])
                start, end = self.app.money.invoice_period(cid, float(d.get("days", 30)))
                if d.get("preview"):
                    p = self.app.money.invoice_preview(cid, start, end)
                    return self._send(200, p)
                return self._send(200, self.app.money.invoice_client(cid, start, end, send=bool(d.get("send"))))
            if path == "/api/memory/clear":
                s.memory_clear(int(d["agent_id"]) if d.get("agent_id") else None)
                return self._send(200, {"ok": True})
            if path == "/api/demo":
                if d.get("loop"):
                    return self._send(200, run_loop_demo(self.app.engine))
                return self._send(200, run_demo(self.app.engine))
            if path == "/api/try":
                aid = int(d["agent_id"])
                a = s.agent(aid)
                if not a:
                    return self._send(404, {"error": "No such agent."})
                model = d.get("model") or next(iter(self.app.config.models))
                if self.app.public_demo and (self.app.config.providers.get((self.app.config.model(model) or {}).get("provider"), {}) or {}).get("type") != "simulator":
                    return self._send(403, {"error": "This public demo runs on the built-in simulated models only."})
                body = {"model": model, "messages": [{"role": "user", "content": str(d.get("prompt", ""))[:20000]}], "max_tokens": 600}
                spec = self.app.config.model(model) or {}
                prov = self.app.config.providers.get(spec.get("provider"), {})
                api = "anthropic" if prov.get("type") == "anthropic" else "openai"
                status, resp, extra = self.app.engine.handle(api, body, a["key"], {})
                if api == "anthropic":
                    text = "".join(b.get("text", "") for b in resp.get("content", []) if isinstance(b, dict))
                else:
                    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "") if status == 200 else ""
                if status != 200:
                    text = (resp.get("error") or {}).get("message", "") if isinstance(resp.get("error"), dict) else str(resp)
                return self._send(200, {"status": status, "text": text, "headers": extra})
            return self._send(404, {"error": "Unknown endpoint"})

        def _vault_post(self, aid, action, d):
            """PayPal on file: /api/agents/{id}/vault/connect | revoke | topup."""
            if not self.app.store.agent(aid):
                return self._send(404, {"error": "No such agent."})
            try:
                if action == "connect":
                    return self._send(200, self.app.vault.connect_start(aid))
                if action == "revoke":
                    r = self.app.vault.revoke(aid, by="owner")
                    if not r:
                        return self._send(409, {"error": "This agent has no saved PayPal payment method to revoke.",
                                                "paypal_on_file": self.app.vault.status(aid)})
                    return self._send(200, dict(r, paypal_on_file=self.app.vault.status(aid)))
                if action == "topup":
                    return self._send(200, self.app.vault.topup(aid, d.get("amount", 10)))
            except VaultError as e:
                return self._send(e.http, {"error": str(e)})
            return self._send(404, {"error": "Unknown action"})

        def _outside_post(self, action, d):
            g = self.app.outside
            if action == "check":
                return self._send(200, g.check())
            if action == "disable":
                return self._send(200, {"tripped": g.disable_now(), "outside": g.status()})
            if action == "clear":
                g.clear_banner()
                return self._send(200, g.status())
            if action == "settings":
                g.update_settings(d)
                if not self.app.public_demo:
                    self.app.save_config()
                return self._send(200, g.status())
            if action == "simulate":
                if not self.app.public_demo:
                    return self._send(403, {"error": "The runaway simulation only runs in the public demo."})
                amount = d.get("amount") or 25
                if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
                    raise ValueError("The simulated amount must be a number.")
                try:
                    amount = float(amount)
                except ValueError:
                    raise ValueError("The simulated amount must be a number.")
                if not math.isfinite(amount):
                    raise ValueError("The simulated amount must be a real number.")
                return self._send(200, g.simulate_runaway(min(500.0, max(1.0, amount))))
            return self._send(404, {"error": "Unknown endpoint"})

        def _delete(self):
            if self.app.public_demo and self._path()[0].startswith("/api/models/"):
                return self._send(403, {"error": "Changing models is turned off in the public demo."})
            path, _ = self._path()
            if not path.startswith("/api/"):
                return self._send(404, {"error": "Unknown endpoint"})
            if not self._api_allowed(write=False):
                return
            parts = path.strip("/").split("/")
            if path.startswith("/api/webhooks"):
                return self._send(*self.app.notify.api("DELETE", path))
            if len(parts) == 3 and parts[1] == "agents" and parts[2].isdigit():
                aid = int(parts[2])
                if self.app.store.agent(aid):
                    self.app.vault.revoke(aid, by="agent deleted")  # never leave a chargeable token behind
                self.app.store.delete_agent(aid)
                return self._send(200, {"ok": True})
            if len(parts) == 3 and parts[1] == "models":
                name = urllib.parse.unquote(parts[2])
                if name in self.app.config.models:
                    del self.app.config.data["models"][name]
                    self.app.save_config()
                return self._send(200, {"models": self.app.config.models})
            return self._send(404, {"error": "Unknown endpoint"})

    return Handler


def build_app(config_path=None, db_path=None, providers=None, paypal=None):
    cfg = Config.load(config_path)
    db = db_path or os.environ.get("LOWTIDES_DB") or os.path.join(ROOT, "data", "lowtides.db")
    store = Store(db)
    if os.environ.get("LOWTIDES_PUBLIC_DEMO", "").lower() in ("1", "true", "yes"):
        # The shared demo (callers without a cookie) gets a hard size cap too, like each sandbox.
        store.db.execute("PRAGMA max_page_count=%d" % (64 * 256))  # 64 MB of 4 KB pages
    save_to = config_path if (config_path and os.path.basename(config_path) != "config.example.json") else None
    return App(cfg, store, paypal=paypal, providers=providers, config_path=save_to)


def serve(host=None, port=None):
    app = build_app()
    host = host or os.environ.get("HOST", "127.0.0.1")
    port = int(port or os.environ.get("PORT", 8787))
    app.loopback = host in LOCAL_NAMES
    if app.public_demo and app.config.paypal.get("env", "sandbox") != "sandbox":
        print("Refusing to start: LOWTIDES_PUBLIC_DEMO only runs with PayPal in sandbox mode (no real money).")
        sys.exit(2)
    if not app.loopback and not app.admin_token and not app.public_demo and not os.environ.get("LOWTIDES_ALLOW_NO_PASSWORD"):
        print("Refusing to start: HOST=%s makes the dashboard reachable from other machines, and "
              "LOWTIDES_ADMIN_TOKEN is not set. Set a password with LOWTIDES_ADMIN_TOKEN, or use HOST=127.0.0.1." % host)
        sys.exit(2)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    app.outside.start()
    print("%s %s running at http://%s:%d  (dashboard + agent API)" % (APP_NAME, __version__, host, port))
    if app.public_demo:
        app.self_url = "http://127.0.0.1:%d" % port
        app.sandboxes.start()
        print("PUBLIC DEMO: no password, PayPal sandbox only, simulated models only. Loading the demo story...")
        if not app.store.agents():
            threading.Thread(target=app.reset_demo, daemon=True).start()
    if not app.admin_token:
        print("Tip: set LOWTIDES_ADMIN_TOKEN to password-protect the dashboard before putting it online.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return httpd


if __name__ == "__main__":
    serve()
