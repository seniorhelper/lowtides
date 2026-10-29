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
import os
import threading
import time
import urllib.error
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
            r = self._call("GET", "/v1/organizations/cost_report?" + q + ("&page=" + page if page else ""))
            for b in r.get("data", []):
                # amount is a decimal string in the currency's lowest unit (cents for USD).
                amt = sum(float(x.get("amount") or 0) for x in b.get("results", [])) / 100.0
                month_total += amt
                if str(b.get("starting_at", "")).startswith(day.strftime("%Y-%m-%d")):
                    today_total += amt
            page = r.get("next_page") if r.get("has_more") else None
            if not page:
                break
        return round(today_total, 4), round(month_total, 4)

    def disable(self, key_id):
        r = self._call("POST", "/v1/organizations/api_keys/" + key_id, {"status": "inactive"})
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
            r = self._call("GET", "/v1/organization/costs?" + q + ("&page=" + page if page else ""))
            for b in r.get("data", []):
                amt = sum(float((x.get("amount") or {}).get("value") or 0) for x in b.get("results", []))
                month_total += amt
                if int(b.get("start_time", 0)) >= int(day.timestamp()):
                    today_total += amt
            page = r.get("next_page") if r.get("has_more") else None
            if not page:
                break
        return round(today_total, 4), round(month_total, 4)

    def disable(self, key_ref):
        project, _, key_id = key_ref.partition("/")
        if not project or not key_id:
            raise ProviderError("OpenAI keys must be listed as project_id/key_id")
        try:
            self._call("DELETE", "/v1/organization/projects/%s/api_keys/%s" % (project, key_id))
        except ProviderError as e:
            if getattr(e, "status", None) == 404:  # already gone: nothing left to stop
                return "deleted"
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
        self._seen = set()    # (scope, level, period) already alerted
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
        s = self.settings
        for k in ("enabled", "stop_agents"):
            if k in d:
                s[k] = bool(d[k])
        if "poll_minutes" in d:
            s["poll_minutes"] = max(1, min(1440, int(d["poll_minutes"])))
        if "warn_percent" in d:
            s["warn_percent"] = max(1, min(100, int(d["warn_percent"])))
        for k in ("daily_cap", "monthly_cap"):
            if k in (d.get("combined") or {}):
                s["combined"][k] = max(0.0, float(d["combined"][k] or 0))
        for name, p in (d.get("providers") or {}).items():
            if name not in s["providers"]:
                raise ValueError("Unknown provider: %s" % name)
            cur = s["providers"][name]
            if "enabled" in p:
                cur["enabled"] = bool(p["enabled"])
            for k in ("daily_cap", "monthly_cap"):
                if k in p:
                    cur[k] = max(0.0, float(p[k] or 0))
            if "protect_keys" in p:
                keys = p["protect_keys"]
                if isinstance(keys, str):
                    keys = keys.replace(",", "\n").split("\n")
                keys = [str(x).strip() for x in keys if str(x).strip()]
                if any(len(x) > 200 or any(c in x for c in " ?&#") for x in keys):
                    raise ValueError("A key ID looks wrong. List key IDs (not the secret keys), one per line.")
                if any(k.startswith(("sk-", "sk_")) for k in keys):
                    raise ValueError("That looks like a secret key. List the key's ID instead, never the key itself.")
                cur["protect_keys"] = keys
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
        s = self.settings
        now = self.now()
        with self.lock:
            for name, p in s["providers"].items():
                if not p.get("enabled"):
                    continue
                st = self.state.setdefault(name, {"today": None, "month": None, "last_checked": None,
                                                  "last_ok": None, "error": None, "retry_at": 0, "keys": {}})
                st["last_checked"] = now.timestamp()
                c = self.client(name)
                if not c:
                    st["error"] = "Admin key not set (%s)." % ADMIN_KEY_ENV.get(name, "")
                    continue
                if st["retry_at"] > now.timestamp():
                    st["error"] = "Waiting for the provider's rate limit to clear."
                    continue
                try:
                    st["today"], st["month"] = c.spend(now)
                    st["last_ok"] = now.timestamp()
                    st["error"] = None
                except ProviderError as e:
                    st["error"] = str(e)
                    if e.retry_after:
                        st["retry_at"] = now.timestamp() + e.retry_after
                    self._alert(("error", name, str(e)), "Outside watch: %s" % e)
            self._apply_caps(s, now)
        return self.status()

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
        scopes = []
        for name, p in s["providers"].items():
            st = self.state.get(name)
            if p.get("enabled") and st and st["last_ok"]:
                scopes.append((name, [name], st["today"], st["month"], p.get("daily_cap"), p.get("monthly_cap")))
        known = [n for n, *_ in scopes]
        if known:
            tot_d = sum(self.state[n]["today"] for n in known)
            tot_m = sum(self.state[n]["month"] for n in known)
            scopes.append(("combined", known, tot_d, tot_m, s["combined"].get("daily_cap"), s["combined"].get("monthly_cap")))
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
                self.trip(names, reason, scope=scope, spend={"today": today, "month": month,
                                                             "daily_cap": dcap, "monthly_cap": mcap})

    def _alert(self, key, message):
        if key in self._seen:
            return
        self._seen.add(key)
        self.alerts.insert(0, {"ts": time.time(), "message": message})
        del self.alerts[20:]
        self.store.event(None, "outside", message)

    # ---- the brake --------------------------------------------------------
    def trip(self, names, reason, scope="manual", spend=None, by="outside watch"):
        """Disable only the keys the owner listed for `names`, audit it, raise the banner,
        and stop Lowtides' own agents."""
        s = self.settings
        results = []
        with self.lock:
            for name in names:
                p = s["providers"].get(name, {})
                c = self.client(name)
                st = self.state.setdefault(name, {"today": None, "month": None, "last_checked": None,
                                                  "last_ok": None, "error": None, "retry_at": 0, "keys": {}})
                for key_id in p.get("protect_keys", []):
                    if not c:
                        outcome, err = "failed", "Admin key not set (%s)." % ADMIN_KEY_ENV.get(name, "")
                    else:
                        try:
                            outcome, err = c.disable(key_id), None
                        except ProviderError as e:
                            outcome, err = "failed", str(e)
                    st["keys"][key_id] = outcome
                    results.append({"provider": name, "key_id": key_id, "result": outcome, "error": err})
            self.tripped = {"at": time.time(), "reason": reason, "scope": scope, "keys": results}
        self.store.audit("outside.cap", scope, {"reason": reason, "by": by, "spend": spend, "keys": results})
        done = [r for r in results if r["result"] in ("disabled", "deleted")]
        failed = [r for r in results if r["result"] == "failed"]
        msg = "OUTSIDE CAP: %s %d key(s) switched off" % (reason, len(done))
        if failed:
            msg += "; %d could not be switched off (%s) - turn them off by hand now" % (
                len(failed), "; ".join("%s: %s" % (r["key_id"], r["error"]) for r in failed))
        if not results:
            msg += " (no keys listed to switch off)"
        self.alerts.insert(0, {"ts": time.time(), "message": msg})
        del self.alerts[20:]
        self.store.event(None, "outside", msg + ".")
        if self.stop_all and s.get("stop_agents", True):
            try:
                self.stop_all("OUTSIDE CAP")
            except Exception as e:
                self.store.event(None, "outside", "STOP ALL after the outside cap failed: %s" % e)
        return self.tripped

    def disable_now(self, by="owner"):
        names = [n for n, p in self.settings["providers"].items() if p.get("enabled")]
        return self.trip(names, "Owner pressed Disable keys now.", scope="manual", by=by)

    def clear_banner(self):
        with self.lock:
            self.tripped = None

    def simulate_runaway(self, amount=25.0):
        if not self.demo:
            raise ValueError("The runaway simulation only runs in the public demo.")
        self._sim.runaway(float(amount))
        self.store.event(None, "outside", "Demo: a script outside Lowtides just spent $%.2f on the simulated provider." % amount)
        return self.check()

    # ---- what the dashboard shows ----------------------------------------
    def status(self):
        s = self.settings
        now = self.now().timestamp()
        poll = float(s["poll_minutes"])
        warn = float(s["warn_percent"])
        provs, tot_d, tot_m, any_ok = [], 0.0, 0.0, False
        with self.lock:
            for name, p in s["providers"].items():
                st = self.state.get(name, {})
                today, month = st.get("today"), st.get("month")
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
                    "daily_cap": p.get("daily_cap") or 0, "monthly_cap": p.get("monthly_cap") or 0,
                    "level": self._level(today, month, p.get("daily_cap"), p.get("monthly_cap"), warn) if last_ok else "unknown",
                    "last_checked": st.get("last_checked"), "last_ok": last_ok, "error": st.get("error"),
                    "stale": bool(last_ok and now - last_ok > 3 * poll * 60),
                    "lag_minutes": LAG_MINUTES.get(name, 15),
                    "key_action": "delete" if name == "openai" else "disable",
                    "keys": [{"id": k, "state": st.get("keys", {}).get(k, "watching")} for k in p.get("protect_keys", [])],
                })
            c = s["combined"]
            return {
                "enabled": bool(s["enabled"]), "demo": self.demo, "poll_minutes": poll, "warn_percent": warn,
                "stop_agents": bool(s.get("stop_agents", True)),
                "combined": {"daily_cap": c.get("daily_cap") or 0, "monthly_cap": c.get("monthly_cap") or 0,
                             "today": round(tot_d, 4) if any_ok else None, "month": round(tot_m, 4) if any_ok else None,
                             "level": self._level(tot_d, tot_m, c.get("daily_cap"), c.get("monthly_cap"), warn) if any_ok else "unknown"},
                "providers": provs, "tripped": self.tripped, "alerts": list(self.alerts[:10]),
            }
