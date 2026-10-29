"""Outside spend guard: watches the provider's real bill, not just calls routed through Lowtides.

A script that calls Anthropic or OpenAI directly never touches the Lowtides proxy, so the
per-agent budgets cannot see it. This guard polls each provider's own cost report (the
billed spend of the whole organization) every few minutes, compares it to daily and
monthly caps, warns at a threshold, and at the cap switches off the API keys the owner
listed, writes a hash-chained audit entry and presses STOP ALL for Lowtides' own agents.

Limits, said plainly: billing data lags behind real use, so this caps the damage; it
cannot make it zero. Admin keys come only from environment variables and are never stored,
logged or shown. In the public demo only a simulated provider exists; no real API is called.
See docs/OUTSIDE-GUARD.md.
"""
import datetime
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = {
    "enabled": False,
    "poll_minutes": 5,
    "warn_percent": 80,
    "stop_agents": True,
    "combined": {"daily_cap": 0, "monthly_cap": 0},
    "providers": {
        "anthropic": {"enabled": False, "daily_cap": 0, "monthly_cap": 0, "protect_keys": []},
        "openai": {"enabled": False, "daily_cap": 0, "monthly_cap": 0, "protect_keys": []},
    },
}
# Fixed on purpose: the dashboard can never point a guard at a different secret or host.
ADMIN_KEY_ENV = {"anthropic": "ANTHROPIC_ADMIN_KEY", "openai": "OPENAI_ADMIN_KEY"}
BASE_URL = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
LABEL = {"anthropic": "Anthropic", "openai": "OpenAI", "simulated": "Simulated provider"}
# How far behind real use the bill can be. Anthropic documents "typically within 5 minutes";
# OpenAI publishes no figure, so 15 is our own conservative guess. See docs/OUTSIDE-GUARD.md.
LAG_MINUTES = {"anthropic": 5, "openai": 15, "simulated": 5}
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_CAP = 1000000.0
ERROR_ALERT_TTL = 3600  # an unchanged provider error is re-announced at most once an hour


class ProviderError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _periods(now):
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value, what):
    if isinstance(value, bool):
        raise ValueError("%s must be a number." % what)
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a number." % what)
    if not math.isfinite(x):
        raise ValueError("%s must be a real number, not infinity or NaN." % what)
    return x


def _cap(value, what):
    """0 (or empty) means no cap; otherwise more than $0 and at most $1,000,000."""
    x = 0.0 if value in (None, "") else _number(value, what)
    if x != 0 and not 0 < x <= MAX_CAP:
        raise ValueError("%s must be more than $0 and at most $1,000,000 (0 means no cap)." % what)
    return x


def _whole(value, what, lo, hi):
    x = _number(value, what)
    if not lo <= x <= hi:
        raise ValueError("%s must be between %d and %d." % (what, lo, hi))
    return int(x)


def _finite(x, default=0.0):
    try:
        return x if x is not None and math.isfinite(float(x)) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _amount(x):
    v = float(x or 0)
    if not math.isfinite(v):
        raise ProviderError("The provider's cost report held a number that is not a real amount.")
    return v


def _check_key_id(provider, ref):
    """Anthropic (and simulated) key IDs are one safe segment; OpenAI needs 'proj_id/key_id'."""
    if provider == "openai":
        parts = ref.split("/")
        if len(parts) != 2 or not all(KEY_ID_RE.match(p) for p in parts):
            raise ValueError("OpenAI keys must be listed as project_id/key_id (letters, digits, _ and - only): %s" % ref[:60])
    elif not KEY_ID_RE.match(ref):
        raise ValueError("A key ID looks wrong (letters, digits, _ and - only). List key IDs, not the secret keys: %s" % ref[:60])
    return ref


def _seg(x):
    return urllib.parse.quote(str(x), safe="")


def _merge(base, extra):
    out = json.loads(json.dumps(base))
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# --------------------------------------------------------------------------
class _Http:
    def __init__(self, name, base_url, key):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._key = key

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        for k, v in self._headers().items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode() or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            retry = e.headers.get("Retry-After") if e.headers else None
            try:
                detail = json.loads(e.read().decode()).get("error", {})
                detail = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            except Exception:
                detail = ""
            msg = "%s answered %d%s" % (LABEL.get(self.name, self.name), e.code, (": " + detail[:200]) if detail else "")
            if e.code == 429:
                msg = "%s rate-limited the check (429); trying again later" % LABEL.get(self.name, self.name)
            err = ProviderError(msg, retry_after=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
            err.status = e.code
            raise err
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ProviderError("Could not reach %s: %s" % (LABEL.get(self.name, self.name), getattr(e, "reason", e)))


class AnthropicCosts(_Http):
    """Admin API: GET /v1/organizations/cost_report (daily buckets) and
    POST /v1/organizations/api_keys/{id} with status 'inactive'."""

    def _headers(self):
        return {"x-api-key": self._key, "anthropic-version": "2023-06-01"}

    def spend(self, now):
        day, month = _periods(now)
        today_total = month_total = 0.0
        page = None
        for _ in range(10):
            q = "starting_at=%s&bucket_width=1d&limit=31" % _iso(month)
            r = self._call("GET", "/v1/organizations/cost_report?" + q + ("&page=" + _seg(page) if page else ""))
            for b in r.get("data", []):
                # amount is a decimal string in the currency's lowest unit (cents for USD).
                amt = sum(_amount(x.get("amount")) for x in b.get("results", [])) / 100.0
                month_total += amt
                if str(b.get("starting_at", "")).startswith(day.strftime("%Y-%m-%d")):
                    today_total += amt
            page = r.get("next_page") if r.get("has_more") else None
            if not page:
                break
        return round(today_total, 4), round(month_total, 4)

    def disable(self, key_id):
        try:
            _check_key_id("anthropic", key_id)
        except ValueError as e:
            raise ProviderError(str(e))
        r = self._call("POST", "/v1/organizations/api_keys/" + _seg(key_id), {"status": "inactive"})
        return "disabled" if r.get("status") == "inactive" else "unknown"


class OpenAICosts(_Http):
    """Admin API: GET /v1/organization/costs (daily buckets). OpenAI has no 'disable' for a
    project key, so a listed key ('proj_id/key_id') is DELETED at the cap. That is permanent."""

    def _headers(self):
        return {"Authorization": "Bearer " + self._key}

    def spend(self, now):
        day, month = _periods(now)
        today_total = month_total = 0.0
        page = None
        for _ in range(10):
            q = "start_time=%d&bucket_width=1d&limit=31" % int(month.timestamp())
            r = self._call("GET", "/v1/organization/costs?" + q + ("&page=" + _seg(page) if page else ""))
            for b in r.get("data", []):
                amt = sum(_amount((x.get("amount") or {}).get("value")) for x in b.get("results", []))
                month_total += amt
                if int(b.get("start_time", 0)) >= int(day.timestamp()):
                    today_total += amt
            page = r.get("next_page") if r.get("has_more") else None
            if not page:
                break
        return round(today_total, 4), round(month_total, 4)

    def disable(self, key_ref):
        try:
            _check_key_id("openai", key_ref)
        except ValueError as e:
            raise ProviderError(str(e))
        project, key_id = key_ref.split("/")
        try:
            self._call("DELETE", "/v1/organization/projects/%s/api_keys/%s" % (_seg(project), _seg(key_id)))
        except ProviderError as e:
            # A 404 can mean a mistyped ID while the real key keeps spending: never call it done.
            if getattr(e, "status", None) == 404:
                raise ProviderError("not found — check the project/key ID (%s); this key is NOT protected" % key_ref)
            raise
        return "deleted"


class SimulatedCosts:
    """Public demo only: fake spend, fake keys. Never touches the network."""
    name = "simulated"

    def __init__(self):
        self.today, self.month = 6.40, 48.10
        self.disabled = set()

    def spend(self, now):
        return round(self.today, 4), round(self.month, 4)

    def runaway(self, amount):
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("The simulated amount must be a real number above $0.")
        self.today += amount
        self.month += amount

    def disable(self, key_id):
        self.disabled.add(key_id)
        return "disabled"


# --------------------------------------------------------------------------
class OutsideGuard:
    def __init__(self, config, store, stop_all=None, demo=False, clients=None, now=utc_now, env=None):
        """`clients` maps provider name -> client (tests pass fakes); otherwise real clients
        are built only for enabled providers whose admin key env var is set. `stop_all`
        is called with a reason when a cap trips."""
        self.config = config
        self.store = store
        self.stop_all = stop_all
        self.demo = demo
        self.now = now
        self.env = os.environ if env is None else env
        self.lock = threading.RLock()
        self._clients = clients
        self.state = {}       # provider -> {today, month, last_checked, last_ok, error, retry_at, keys}
        self.tripped = None   # {at, reason, scope, keys} while the red banner is up
        self.alerts = []      # recent warn / cap messages, newest first
        self._seen = {}       # alert key -> when it was last announced
        self._thread = None
        self._stop = threading.Event()
        if demo:
            self._sim = SimulatedCosts()
            self.demo_settings = _merge(DEFAULTS, {"enabled": True, "providers": {}})
            self.demo_settings["providers"] = {"simulated": {"enabled": True, "daily_cap": 20, "monthly_cap": 100,
                                                             "protect_keys": ["apikey_demo_runaway_script"]}}

    # ---- settings ---------------------------------------------------------
    @property
    def settings(self):
        if self.demo:
            return self.demo_settings
        return _merge(DEFAULTS, self.config.data.get("outside"))

    def update_settings(self, d):
        """Owner edits caps, threshold, poll interval and the key list. Admin keys and
        hosts are not settable here."""
        if not isinstance(d, dict):
            raise ValueError("Settings must be a JSON object.")
        s = _merge(self.settings, {})  # work on a copy: a rejected request changes nothing
        for k in ("enabled", "stop_agents"):
            if k in d:
                s[k] = bool(d[k])
        if "poll_minutes" in d:
            s["poll_minutes"] = _whole(d["poll_minutes"], "The check interval (minutes)", 1, 1440)
        if "warn_percent" in d:
            s["warn_percent"] = _whole(d["warn_percent"], "The warning threshold (%)", 1, 100)
        combined = d.get("combined") or {}
        if not isinstance(combined, dict):
            raise ValueError("combined must be an object.")
        for k in ("daily_cap", "monthly_cap"):
            if k in combined:
                s["combined"][k] = _cap(combined[k], "The combined %s cap" % k.split("_")[0])
        providers = d.get("providers") or {}
        if not isinstance(providers, dict):
            raise ValueError("providers must be an object.")
        for name, p in providers.items():
            if name not in s["providers"]:
                raise ValueError("Unknown provider: %s" % name)
            if not isinstance(p, dict):
                raise ValueError("Settings for %s must be an object." % name)
            cur = s["providers"][name]
            if "enabled" in p:
                cur["enabled"] = bool(p["enabled"])
            for k in ("daily_cap", "monthly_cap"):
                if k in p:
                    cur[k] = _cap(p[k], "The %s %s cap" % (LABEL.get(name, name), k.split("_")[0]))
            if "protect_keys" in p:
                keys = p["protect_keys"]
                if isinstance(keys, str):
                    keys = keys.replace(",", "\n").split("\n")
                if not isinstance(keys, list):
                    raise ValueError("protect_keys must be a list of key IDs.")
                keys = [str(x).strip() for x in keys if str(x).strip()]
                if len(keys) > 100 or any(len(x) > 200 for x in keys):
                    raise ValueError("A key ID looks wrong. List key IDs (not the secret keys), one per line.")
                if any(k.startswith(("sk-", "sk_")) for k in keys):
                    raise ValueError("That looks like a secret key. List the key's ID instead, never the key itself.")
                cur["protect_keys"] = [_check_key_id(name, k) for k in keys]
        if self.demo:
            self.demo_settings = s
        else:
            self.config.data["outside"] = s
        return s

    def admin_key_set(self, name):
        return bool(self.env.get(ADMIN_KEY_ENV.get(name, ""), ""))

    def client(self, name):
        if self.demo:
            return self._sim if name == "simulated" else None
        if self._clients is not None:
            return self._clients.get(name)
        key = self.env.get(ADMIN_KEY_ENV.get(name, ""), "")
        if not key:
            return None
        cls = {"anthropic": AnthropicCosts, "openai": OpenAICosts}.get(name)
        return cls(name, BASE_URL[name], key) if cls else None

    # ---- polling ----------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="outside-guard", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.settings["enabled"]:
                    self.check()
            except Exception as e:  # the watcher must never die quietly
                self.store.event(None, "outside", "Outside watch check failed: %s" % e)
            self._stop.wait(max(60, float(self.settings["poll_minutes"]) * 60))

    def check(self):
        """Fetch real billed spend for every enabled provider, then apply the caps."""
        # The lock is held only to read config and write state, never across a network
        # call, so /api/state (which reads status()) never waits on a slow provider.
        now = self.now()
        todo = []
        with self.lock:
            s = self.settings
            for name, p in s["providers"].items():
                if not p.get("enabled"):
                    continue
                st = self._st(name)
                st["last_checked"] = now.timestamp()
                c = self.client(name)
                if not c:
                    st["error"] = "Admin key not set (%s)." % ADMIN_KEY_ENV.get(name, "")
                    continue
                if st["retry_at"] > now.timestamp():
                    st["error"] = "Waiting for the provider's rate limit to clear."
                    continue
                todo.append((name, c))
        results = []
        for name, c in todo:
            try:
                results.append((name, c.spend(now), None))
            except ProviderError as e:
                results.append((name, None, e))
        with self.lock:
            for name, spent, e in results:
                st = self._st(name)
                if e is None:
                    st["today"], st["month"] = spent
                    st["last_ok"] = now.timestamp()
                    st["error"] = None
                    for k in [k for k in self._seen if k[:2] == ("error", name)]:
                        del self._seen[k]  # the error cleared: announce it again if it comes back
                else:
                    st["error"] = str(e)
                    if e.retry_after:
                        st["retry_at"] = now.timestamp() + e.retry_after
                    self._alert(("error", name, str(e)), "Outside watch: %s" % e, ttl=ERROR_ALERT_TTL)
            pending = self._apply_caps(s, now)
        if pending:
            self._run_trip(*pending)
        return self.status()

    def _st(self, name):
        return self.state.setdefault(name, {"today": None, "month": None, "last_checked": None, "last_ok": None,
                                            "error": None, "retry_at": 0, "keys": {}, "key_errors": {}})

    @staticmethod
    def _fresh(st, now):
        """Today's/this month's figures, zeroed when the last good read was from an earlier
        UTC day/month: a stale 'today' must not count against today's cap."""
        today, month, last_ok = st.get("today"), st.get("month"), st.get("last_ok")
        if not last_ok:
            return today, month
        then = datetime.datetime.fromtimestamp(last_ok, datetime.timezone.utc)
        if then.date() != now.date() and today is not None:
            today = 0.0
        if (then.year, then.month) != (now.year, now.month) and month is not None:
            month = 0.0
        return today, month

    def _level(self, today, month, daily_cap, monthly_cap, warn):
        level = "ok"
        for spent, cap in ((today, daily_cap), (month, monthly_cap)):
            if not cap or spent is None:
                continue
            if spent >= cap:
                return "over"
            if spent >= cap * warn / 100.0:
                level = "warn"
        return level

    def _apply_caps(self, s, now):
        warn = float(s["warn_percent"])
        day_key = now.strftime("%Y-%m-%d")
        """Called with the lock held. Returns the trip to run (after the lock is released) or None."""
        scopes = []
        for name, p in s["providers"].items():
            st = self.state.get(name)
            if p.get("enabled") and st and st["last_ok"]:
                today, month = self._fresh(st, now)
                scopes.append((name, [name], today, month, p.get("daily_cap"), p.get("monthly_cap")))
        known = [n for n, *_ in scopes]
        if known:
            tot_d = sum(x[2] or 0 for x in scopes)
            tot_m = sum(x[3] or 0 for x in scopes)
            scopes.append(("combined", known, tot_d, tot_m, s["combined"].get("daily_cap"), s["combined"].get("monthly_cap")))
        pending = None
        for scope, names, today, month, dcap, mcap in scopes:
            level = self._level(today, month, dcap, mcap, warn)
            label = LABEL.get(scope, "All providers")
            if level == "warn":
                self._alert(("warn", scope, day_key), "Outside watch: %s is at $%.2f today / $%.2f this month, past %d%% of its cap."
                            % (label, today, month, warn))
            elif level == "over" and not self.tripped:
                # While the banner is up the keys are already off. Once the owner clears it,
                # spend still over the cap trips again: raise the cap to resume.
                reason = "%s billed spend reached its cap ($%.2f today, $%.2f this month; caps $%s/day, $%s/month)." % (
                    label, today, month, dcap or "none", mcap or "none")
                # Claim the banner now, under the lock, so a parallel check cannot trip twice.
                self.tripped = {"at": time.time(), "reason": reason, "scope": scope, "keys": [], "in_progress": True}
                pending = (names, reason, scope, {"today": today, "month": month, "daily_cap": dcap, "monthly_cap": mcap},
                           "outside watch")
        return pending

    def _alert(self, key, message, ttl=None):
        now = time.time()
        last = self._seen.get(key)
        if last is not None and (ttl is None or now - last < ttl):
            return
        self._seen[key] = now
        if len(self._seen) > 500:  # bounded: drop the oldest
            for k in sorted(self._seen, key=self._seen.get)[:250]:
                del self._seen[k]
        self.alerts.insert(0, {"ts": now, "message": message})
        del self.alerts[20:]
        self.store.event(None, "outside", message)

    # ---- the brake --------------------------------------------------------
    def trip(self, names, reason, scope="manual", spend=None, by="outside watch"):
        """Disable only the keys the owner listed for `names`, audit it, raise the banner,
        and stop Lowtides' own agents."""
        with self.lock:
            self.tripped = {"at": time.time(), "reason": reason, "scope": scope, "keys": [], "in_progress": True}
        return self._run_trip(names, reason, scope, spend, by)

    def _run_trip(self, names, reason, scope, spend, by):
        with self.lock:
            s = self.settings
            plan = [(name, self.client(name), list(s["providers"].get(name, {}).get("protect_keys", []))) for name in names]
        # Stop Lowtides' own agents first: that is instant, the provider calls below are not.
        if self.stop_all and s.get("stop_agents", True):
            try:
                self.stop_all("OUTSIDE CAP")
            except Exception as e:
                self.store.event(None, "outside", "STOP ALL after the outside cap failed: %s" % e)
        results = []
        for name, c, keys in plan:  # network calls: never under the lock
            for key_id in keys:
                if not c:
                    outcome, err = "failed", "Admin key not set (%s)." % ADMIN_KEY_ENV.get(name, "")
                else:
                    try:
                        outcome, err = c.disable(key_id), None
                    except ProviderError as e:
                        outcome, err = "failed", str(e)
                results.append({"provider": name, "key_id": key_id, "result": outcome, "error": err})
        with self.lock:
            for r in results:
                st = self._st(r["provider"])
                st["keys"][r["key_id"]] = r["result"]
                if r["error"]:
                    st["key_errors"][r["key_id"]] = r["error"]
                else:
                    st["key_errors"].pop(r["key_id"], None)
            self.tripped = {"at": time.time(), "reason": reason, "scope": scope, "keys": results,
                            "problems": [r for r in results if r["result"] == "failed"]}
            tripped = self.tripped
        self.store.audit("outside.cap", scope, {"reason": reason, "by": by, "spend": spend, "keys": results})
        done = [r for r in results if r["result"] in ("disabled", "deleted")]
        failed = [r for r in results if r["result"] == "failed"]
        msg = "OUTSIDE CAP: %s %d key(s) switched off" % (reason, len(done))
        if failed:
            msg += "; %d NOT protected, could not be switched off (%s) - turn them off by hand now" % (
                len(failed), "; ".join("%s: %s" % (r["key_id"], r["error"]) for r in failed))
        if not results:
            msg += " (no keys listed to switch off)"
        with self.lock:
            self.alerts.insert(0, {"ts": time.time(), "message": msg})
            del self.alerts[20:]
        self.store.event(None, "outside", msg + ".")
        return tripped

    def disable_now(self, by="owner"):
        names = [n for n, p in self.settings["providers"].items() if p.get("enabled")]
        return self.trip(names, "Owner pressed Disable keys now.", scope="manual", by=by)

    def clear_banner(self):
        with self.lock:
            self.tripped = None

    def simulate_runaway(self, amount=25.0):
        if not self.demo:
            raise ValueError("The runaway simulation only runs in the public demo.")
        amount = _number(amount, "The simulated amount")
        self._sim.runaway(amount)
        self.store.event(None, "outside", "Demo: a script outside Lowtides just spent $%.2f on the simulated provider." % amount)
        return self.check()

    # ---- what the dashboard shows ----------------------------------------
    def status(self):
        now_dt = self.now()
        now = now_dt.timestamp()
        provs, tot_d, tot_m, any_ok = [], 0.0, 0.0, False
        with self.lock:
            s = self.settings
            poll = float(_finite(s["poll_minutes"], 5))
            warn = float(_finite(s["warn_percent"], 80))
            for name, p in s["providers"].items():
                st = self.state.get(name, {})
                today, month = self._fresh(st, now_dt)
                today, month = (None if x is None else _finite(x) for x in (today, month))
                key_errors = st.get("key_errors", {})
                problems = ["Key %s is NOT protected: %s" % (k, key_errors[k]) for k in p.get("protect_keys", [])
                            if st.get("keys", {}).get(k) == "failed" and k in key_errors]
                error = "; ".join([e for e in [st.get("error")] if e] + problems) or None
                if p.get("enabled") and st.get("last_ok"):
                    tot_d += today or 0
                    tot_m += month or 0
                    any_ok = True
                last_ok = st.get("last_ok")
                provs.append({
                    "name": name, "label": LABEL.get(name, name), "enabled": bool(p.get("enabled")),
                    "admin_key_env": ADMIN_KEY_ENV.get(name, ""),
                    "admin_key_set": True if self.demo else self.admin_key_set(name),
                    "today": today, "month": month,
                    "daily_cap": _finite(p.get("daily_cap")), "monthly_cap": _finite(p.get("monthly_cap")),
                    "level": self._level(today, month, p.get("daily_cap"), p.get("monthly_cap"), warn) if last_ok else "unknown",
                    "last_checked": st.get("last_checked"), "last_ok": last_ok, "error": error,
                    "stale": bool(last_ok and now - last_ok > 3 * poll * 60),
                    "lag_minutes": LAG_MINUTES.get(name, 15),
                    "key_action": "delete" if name == "openai" else "disable",
                    "keys": [{"id": k, "state": st.get("keys", {}).get(k, "watching"),
                              "protected": st.get("keys", {}).get(k) != "failed",
                              "error": key_errors.get(k) if st.get("keys", {}).get(k) == "failed" else None}
                             for k in p.get("protect_keys", [])],
                    "problems": problems,
                })
            c = s["combined"]
            return {
                "enabled": bool(s["enabled"]), "demo": self.demo, "poll_minutes": poll, "warn_percent": warn,
                "stop_agents": bool(s.get("stop_agents", True)),
                "combined": {"daily_cap": _finite(c.get("daily_cap")), "monthly_cap": _finite(c.get("monthly_cap")),
                             "today": round(tot_d, 4) if any_ok else None, "month": round(tot_m, 4) if any_ok else None,
                             "level": self._level(tot_d, tot_m, c.get("daily_cap"), c.get("monthly_cap"), warn) if any_ok else "unknown"},
                "providers": provs, "tripped": self.tripped, "alerts": list(self.alerts[:10]),
            }
