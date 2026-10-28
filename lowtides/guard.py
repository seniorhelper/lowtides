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
