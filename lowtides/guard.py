"""The governor: budgets, the STOP button, rate limits and the loop detector.

Checks run before any money is spent. A blocked call costs nothing.
"""
import collections
import datetime
import hashlib
import json
import threading
import time


class Blocked(Exception):
    def __init__(self, reason, code, http=402):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.http = http


def start_of_today():
    now = datetime.datetime.now()
    return datetime.datetime(now.year, now.month, now.day).timestamp()


def fingerprint(body):
    msgs = body.get("messages") or []
    tail = msgs[-2:]
    raw = json.dumps(tail, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Guard:
    def __init__(self, store, policy):
        self.store = store
        self.policy = policy
        self.recent = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def _note(self, agent_id, fp):
        now = time.time()
        window = float(self.policy.get("loop_window_seconds", 120))
        with self.lock:
            dq = self.recent[agent_id]
            dq.append((now, fp))
            while dq and dq[0][0] < now - max(window, 60):
                dq.popleft()
            same = sum(1 for t, f in dq if f == fp and t >= now - window)
            per_min = sum(1 for t, _ in dq if t >= now - 60)
        return same, per_min

    def forget(self, agent_id, body):
        """Undo the loop count for a call that failed upstream, so SDK auto-retries
        after a provider outage don't get a healthy agent paused."""
        fp = fingerprint(body)
        with self.lock:
            dq = self.recent[agent_id]
            for i in range(len(dq) - 1, -1, -1):
                if dq[i][1] == fp:
                    del dq[i]
                    break

    def check_status(self, agent):
        if agent["status"] == "stopped":
            raise Blocked("This agent is stopped. Press Start on the dashboard to let it run again.", "agent_stopped", 403)
        if agent["status"] == "paused":
            raise Blocked("The loop guard paused this agent because it kept repeating itself. Review it, then press Resume.",
                          "agent_paused", 403)

    def check(self, agent, task, body, estimated_cost):
        aid = agent["id"]
        self.check_status(agent)
        if agent["status"] == "stopped":
            raise Blocked("This agent is stopped. Press Start on the dashboard to let it run again.", "agent_stopped", 403)
        if agent["status"] == "paused":
            raise Blocked("The loop guard paused this agent because it kept repeating itself. Review it, then press Resume.",
                          "agent_paused", 403)

        same, per_min = self._note(aid, fingerprint(body))
        max_rep = int(self.policy.get("loop_max_repeats", 4))
        if max_rep > 0 and same >= max_rep:
            self.store.update_agent(aid, status="paused")
            self.store.event(aid, "loop", "Loop guard: %s sent the same request %d times in %ds. Paused it before it burned more money."
                             % (agent["name"], same, int(self.policy.get("loop_window_seconds", 120))))
            raise Blocked("Loop detected: the same request repeated %d times. The agent is paused." % same, "loop_detected", 429)
        cap = int(self.policy.get("max_calls_per_minute", 60))
        if cap > 0 and per_min > cap:
            raise Blocked("Too many calls this minute (limit %d). Slow down and retry shortly." % cap, "rate_limited", 429)

        spent_today = self.store.spend_since(aid, start_of_today())
        if spent_today + estimated_cost > float(agent["daily_budget"]):
            self.store.event(aid, "budget", "%s hit its daily allowance of $%.2f." % (agent["name"], agent["daily_budget"]))
            raise Blocked("Daily allowance reached ($%.2f). It resets at midnight, or raise it on the dashboard." % agent["daily_budget"],
                          "daily_budget", 402)

        from .spend import agent_rules  # late import: spend imports this module
        total_cap = agent_rules(agent)["daily_total_limit"]
        if total_cap is not None:
            bought = self.store.purchases_since(aid, start_of_today())
            if spent_today + bought + estimated_cost > total_cap:
                self.store.event(aid, "budget", "%s hit its combined AI + purchases limit of $%.2f." % (agent["name"], total_cap))
                raise Blocked("Combined daily limit reached: AI usage plus purchases can't pass $%.2f today." % total_cap,
                              "daily_total_limit", 402)

        if task:
            spent = self.store.task_spend(task["id"])
            if task["status"] != "open":
                raise Blocked("Task %s is already closed." % task["id"], "task_closed", 409)
            if spent + estimated_cost > float(task["budget"]):
                self.store.event(aid, "budget", "Task '%s' reached its budget of $%.2f." % (task["label"] or task["id"], task["budget"]))
                raise Blocked("Task budget reached ($%.2f spent of $%.2f)." % (spent, task["budget"]), "task_budget", 402)

        if agent["use_wallet"]:
            bal = self.store.balance(aid)
            if bal < estimated_cost or bal <= 0:
                raise Blocked("Wallet is empty (balance $%.2f). Top it up with PayPal on the dashboard." % bal, "wallet_empty", 402)
        return True

    # ---- the Leash: per-agent limits on data dumps ------------------------------------
    def check_leash(self, agent, leash, input_tokens, model, call_cost):
        """Refuse a prompt that is too big, or a single call that would cost too much.
        `call_cost` is the pre-flight estimate on the model that will answer."""
        aid = agent["id"]
        cap = leash["max_input_tokens"]
        if cap is not None and input_tokens > cap:
            msg = ("This prompt is about %d tokens; this agent's leash allows %d. Summarize or split it first."
                   % (input_tokens, cap))
            self.store.event(aid, "leash", "Leash: blocked a %d-token prompt from %s (limit %d)." % (input_tokens, agent["name"], cap))
            self.store.audit("leash.blocked", str(aid), {"rule": "max_input_tokens", "input_tokens": input_tokens, "limit": cap})
            raise Blocked(msg, "leash_input_tokens", 413)
        cap = leash["max_cost_per_call"]
        if cap is not None and call_cost > cap:
            msg = ("This call would cost about $%.4f on %s; this agent's leash allows $%.4f per call. "
                   "Shorten the prompt or lower max_tokens first." % (call_cost, model, cap))
            self.store.event(aid, "leash", "Leash: blocked a $%.4f call from %s (limit $%.4f per call)." % (call_cost, agent["name"], cap))
            self.store.audit("leash.blocked", str(aid), {"rule": "max_cost_per_call", "estimated_cost": round(call_cost, 6),
                                                         "model": model, "limit": cap})
            raise Blocked(msg, "leash_cost_per_call", 402)

    def dump_alert(self, agent, leash, input_tokens):
        """Flag (never block) a prompt far bigger than this agent's usual. Returns a note or ''."""
        mult = leash["dump_alert_multiple"]
        if not mult or mult <= 0:
            return ""
        sizes = sorted(self.store.recent_input_sizes(agent["id"], DUMP_HISTORY))
        if len(sizes) < DUMP_MIN_HISTORY:
            return ""
        n = len(sizes)
        typical = sizes[n // 2] if n % 2 else (sizes[n // 2 - 1] + sizes[n // 2]) / 2.0
        if typical <= 0 or input_tokens <= mult * typical:
            return ""
        note = "dump alert: about %d input tokens, %.0fx this agent's typical %d" % (input_tokens, input_tokens / typical, typical)
        self.store.event(agent["id"], "leash", "Dump alert: %s sent about %d tokens, %.0fx its typical %d. Let through."
                         % (agent["name"], input_tokens, input_tokens / typical, typical))
        self.store.audit("leash.dump_alert", str(agent["id"]), {"input_tokens": input_tokens, "typical": typical, "multiple": mult})
        return note


DUMP_HISTORY = 20
DUMP_MIN_HISTORY = 5
LEASH_DEFAULTS = {"max_reply_tokens": None, "max_input_tokens": None, "max_cost_per_call": None, "dump_alert_multiple": 5.0}


def _num(v, kind):
    if v is None or isinstance(v, bool):
        return None
    try:
        v = kind(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def leash_rules(agent):
    """The agent's leash settings. Reads them through spend.agent_rules; falls back to the
    raw saved rules for any key that version of spend.py doesn't know yet."""
    from .spend import agent_rules  # late import: spend imports this module
    rules = agent_rules(agent)
    try:
        raw = json.loads(agent.get("rules") or "{}")
        raw = raw if isinstance(raw, dict) else {}
    except ValueError:
        raw = {}
    out = {}
    for k, d in LEASH_DEFAULTS.items():
        v = rules[k] if k in rules else raw.get(k, d)
        out[k] = v
    out["max_reply_tokens"] = _num(out["max_reply_tokens"], int)
    out["max_input_tokens"] = _num(out["max_input_tokens"], int)
    out["max_cost_per_call"] = _num(out["max_cost_per_call"], float)
    out["dump_alert_multiple"] = _num(out["dump_alert_multiple"], float) or 0.0  # null or 0 turns it off
    return out
