"""The governor pipeline. Every agent call goes through these steps:

  1. Who is calling?        (agent key)
  2. Seen this before?      (answer memory: repeats are free)
  3. Trim the snowball      (old tool output)
  4. Cache the stable part  (Anthropic prompt caching, added automatically)
  5. Pick the cheapest fit  (grade the step, never above the model asked for)
  6. Check the governor          (STOP button, loops, budgets, wallet)
  7. Call, check, climb     (verify the answer, escalate only if weak)
  8. Write it down          (actual cost vs. what it would have cost)

"Baseline" everywhere means: the same tokens on the model your agent asked for,
with no trimming, no Lowtides-added caching and no answer memory. Savings are
baseline minus what you actually paid, never less than zero, and failed cheap
attempts are counted in what you actually paid.
"""
import copy
import json
import random
import threading
import time

from . import memory as memory_mod
from . import pricing
from .guard import Blocked, Guard, start_of_today
from .providers import ProviderError, Providers
from .router import Router, weak_answer


def has_cache_control(obj):
    if isinstance(obj, dict):
        if "cache_control" in obj:
            return True
        return any(has_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_cache_control(v) for v in obj)
    return False


def add_prompt_cache(body, min_tokens):
    """Mark the system prompt and tool list as cacheable (Anthropic API).
    Returns (new_body, added). Never touches a request that already uses caching."""
    if has_cache_control(body):
        return body, False
    stable = pricing.estimate_tokens(body.get("system")) + pricing.estimate_tokens(body.get("tools"))
    if stable < int(min_tokens):
        return body, False
    new = copy.deepcopy(body)
    added = False
    tools = new.get("tools")
    if isinstance(tools, list) and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        added = True
    sysp = new.get("system")
    if isinstance(sysp, str) and sysp.strip():
        new["system"] = [{"type": "text", "text": sysp, "cache_control": {"type": "ephemeral"}}]
        added = True
    elif isinstance(sysp, list) and sysp:
        sysp[-1]["cache_control"] = {"type": "ephemeral"}
        added = True
    return new, added


def blocked_body(api, reason, code):
    if api == "anthropic":
        return {"type": "error", "error": {"type": "lowtides_blocked", "code": code, "message": reason}}
    return {"error": {"message": reason, "type": "lowtides_blocked", "code": code}}


def error_body(api, message, status=502):
    if api == "anthropic":
        return {"type": "error", "error": {"type": "api_error", "message": message}}
    return {"error": {"message": message, "type": "upstream_error", "code": status}}


class Engine:
    def __init__(self, config, store, providers=None):
        self.config = config
        self.store = store
        self.providers = providers or Providers(config)
        self.router = Router(config)
        self.guard = Guard(store, config.policy)
        self.memory = memory_mod.AnswerMemory(store, config.policy)

    # ------------------------------------------------------------------
    def _task_for(self, agent, headers):
        tid = (headers.get("x-lowtides-task") or "").strip()[:64]
        if not tid:
            return None
        t = self.store.task(tid)
        if t and t["agent_id"] != agent["id"]:
            raise Blocked("That task belongs to a different agent.", "task_forbidden", 403)
        if not t:
            label = (headers.get("x-lowtides-task-label") or "").strip()[:80]
            self.store.add_task(agent["id"], label, agent["task_budget"], tid=tid)
            t = self.store.task(tid)
        return t

    def _baseline(self, requested_spec, usage, trimmed, added_cache):
        if added_cache:
            # Without Lowtides's caching, cached tokens would have been billed as normal input.
            inp = usage["input"] + usage["cache_read"] + usage["cache_write"] + trimmed
            return pricing.cost(requested_spec, input_tokens=inp, output_tokens=usage["output"])
        if usage["cache_read"] > 0:
            # The provider is caching this conversation (e.g. OpenAI automatic prefix caching).
            # Old trimmed context would most likely have been a cheap cache read, so price it
            # that way. This keeps the savings claim conservative.
            return pricing.cost(requested_spec, input_tokens=usage["input"], output_tokens=usage["output"],
                                cache_read=usage["cache_read"] + trimmed, cache_write=usage["cache_write"])
        return pricing.cost(requested_spec, input_tokens=usage["input"] + trimmed, output_tokens=usage["output"],
                            cache_read=usage["cache_read"], cache_write=usage["cache_write"])

    # ------------------------------------------------------------------
    def handle(self, api, body, agent_key, headers=None):
        """Returns (http_status, response_body, extra_headers)."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        policy = self.config.policy
        agent = self.store.agent_by_key(agent_key)
        if not agent:
            return 401, blocked_body(api, "Unknown or missing Lowtides agent key.", "bad_key"), {}
        requested = body.get("model") or ""
        req_spec = self.config.model(requested)
        if not req_spec:
            msg = "Model '%s' is not in your Lowtides price list. Add it on the Models screen so its cost can be tracked." % requested
            return 400, blocked_body(api, msg, "unknown_model"), {}

        try:
            task = self._task_for(agent, headers)
        except Blocked as b:
            return b.http, blocked_body(api, b.reason, b.code), {}

        # STOP means stop: a stopped or paused agent gets nothing, not even free answers.
        try:
            self.guard.check_status(agent)
        except Blocked as b:
            self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                              used_model="blocked", cost=0.0, baseline_cost=0.0, source="blocked", note=b.code)
            return b.http, blocked_body(api, b.reason, b.code), {}

        # 2) answer memory
        no_memory = headers.get("x-lowtides-memory", "").lower() == "off"
        hit = None if no_memory else self.memory.lookup(agent["id"], api, requested, body)
        if hit:
            row = hit["row"]
            resp = json.loads(row["response"])
            self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                              used_model="answer memory", cost=0.0, baseline_cost=row["baseline_cost"], source="memory",
                              note="%s match (similarity %.2f)" % (hit["match"], hit["similarity"]))
            return 200, resp, self._headers(agent, task, "answer memory", 0.0, row["baseline_cost"])

        # 3) trim + 4) cache + cap
        work = body
        trimmed = 0
        # If the agent manages its own prompt cache, trimming would change the cached
        # prefix and cost it more, so leave those requests alone.
        if policy.get("trim_context", True) and not has_cache_control(body):
            work, trimmed = memory_mod_trim(work, api, policy)
        added_cache = False
        if api == "anthropic" and policy.get("auto_prompt_cache", True):
            work, added_cache = add_prompt_cache(work, policy.get("prompt_cache_min_tokens", 2048))
        cap = int(policy.get("max_output_tokens_cap", 0) or 0)
        notes = []
        thinking = bool(work.get("thinking")) or bool(work.get("reasoning_effort"))
        for key in ("max_tokens", "max_completion_tokens"):
            # Never cap a request that uses extended thinking/reasoning: the output budget
            # must stay above the thinking budget or the provider rejects the call.
            if cap and not thinking and isinstance(work.get(key), int) and work[key] > cap:
                work = dict(work)
                work[key] = cap
                notes.append("output capped at %d tokens" % cap)

        # 5) plan
        forced = headers.get("x-lowtides-tier")
        order, info = self.router.plan(requested, work, forced_tier=forced if forced in ("1", "2", "3") else None)

        # 6) guard. Estimate with the priciest model the cascade could reach, so a climb
        # can't blow through a budget.
        est_out = min(int(work.get("max_tokens") or work.get("max_completion_tokens") or 1024), 1024)
        est_in = pricing.estimate_request_tokens(work)
        est = max(pricing.cost(self.config.model(m) or req_spec, input_tokens=est_in, output_tokens=est_out) for m in order)
        try:
            self.guard.check(agent, task, work, est)
        except Blocked as b:
            self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                              used_model="blocked", cost=0.0, baseline_cost=0.0, source="blocked", note=b.code)
            return b.http, blocked_body(api, b.reason, b.code), {}

        # 7) call, verify, climb
        total_cost, escalations, attempts = 0.0, 0, []
        result, used = None, None
        estimated_any = False
        last_err = None
        passthru = {k: headers[k] for k in ("anthropic-beta",) if headers.get(k)}
        for i, model in enumerate(order):
            try:
                r = self.providers.call(model, work, api, passthru)
            except ProviderError as e:
                last_err = e
                attempts.append("%s failed (%s)" % (model, e))
                more = i < len(order) - 1
                # A cheaper model may reject something the requested model accepts
                # (thinking, images, a long context). Move up instead of failing the agent.
                if more and (e.retryable or e.status in (401, 403, 404) or (model != requested and e.status in (400, 413, 422))):
                    continue
                self.guard.forget(agent["id"], work)
                return (e.status or 502), (e.body or error_body(api, str(e), e.status or 502)), {}
            spec = self.config.model(model)
            c = pricing.cost(spec, r["usage"]["input"], r["usage"]["output"], r["usage"]["cache_read"], r["usage"]["cache_write"])
            total_cost += c
            estimated_any = estimated_any or r.get("usage_estimated", False)
            reason = weak_answer(work, r) if i < len(order) - 1 else ""
            if reason:
                escalations += 1
                attempts.append("%s: %s, moved up" % (model, reason))
                continue
            result, used = r, model
            break
        if result is None:
            self.guard.forget(agent["id"], work)
            msg = "All models failed. Last error: %s" % last_err if last_err else "No model produced an answer."
            return 502, error_body(api, msg), {}

        # 8) record
        baseline = self._baseline(req_spec, result["usage"], trimmed, added_cache)
        if total_cost > baseline:
            baseline_note = "cheap attempts cost more than going direct this time"
            notes.append(baseline_note)
        notes = attempts + notes + ["graded tier %s (%s)" % (info.get("tier"), ", ".join(info.get("reasons", [])))]
        if trimmed:
            notes.append("trimmed about %d tokens of old tool output" % trimmed)
        if added_cache:
            notes.append("prompt caching added")
        if result.get("simulated"):
            notes.append("simulated demo model")
        u = result["usage"]
        self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                          used_model=used, input_tokens=u["input"], output_tokens=u["output"],
                          cache_read_tokens=u["cache_read"], cache_write_tokens=u["cache_write"],
                          trimmed_tokens=trimmed, cost=round(total_cost, 8), baseline_cost=round(baseline, 8),
                          source="model", escalations=escalations, usage_estimated=1 if estimated_any else 0,
                          note="; ".join(notes)[:900])
        if agent["use_wallet"] and total_cost > 0:
            self.store.wallet_move(agent["id"], -total_cost, "ai usage")
        if not no_memory:
            self.memory.remember(agent["id"], api, requested, body, result, baseline)
        self._maybe_shadow(agent, api, work, requested, used, result, passthru)
        return 200, result["response"], self._headers(agent, task, used, total_cost, baseline)

    # ------------------------------------------------------------------
    def _headers(self, agent, task, used, cost, baseline):
        left = max(0.0, float(agent["daily_budget"]) - self.store.spend_since(agent["id"], start_of_today()))
        h = {
            "X-Lowtides-Model-Used": used,
            "X-Lowtides-Cost": "%.6f" % cost,
            "X-Lowtides-Saved": "%.6f" % max(0.0, baseline - cost),
            "X-Lowtides-Budget-Left-Today": "%.4f" % left,
        }
        if task:
            h["X-Lowtides-Task"] = task["id"]
            h["X-Lowtides-Task-Left"] = "%.4f" % max(0.0, float(task["budget"]) - self.store.task_spend(task["id"]))
        return h

    def _maybe_shadow(self, agent, api, work, requested, used, result, passthru=None):
        rate = float(self.config.policy.get("shadow_sample_rate", 0) or 0)
        if rate <= 0 or used == requested or random.random() >= rate:
            return

        def run():
            try:
                r = self.providers.call(requested, work, api, passthru)
            except ProviderError:
                return
            spec = self.config.model(requested)
            c = pricing.cost(spec, r["usage"]["input"], r["usage"]["output"], r["usage"]["cache_read"], r["usage"]["cache_write"])
            sim = memory_mod.similarity(result.get("text", ""), r.get("text", ""))
            self.store.add_shadow(agent_id=agent["id"], governed_model=used, baseline_model=requested,
                                  governed_text=(result.get("text") or "")[:4000], baseline_text=(r.get("text") or "")[:4000],
                                  similarity=round(sim, 3), shadow_cost=c)
            # The shadow check is extra spend for proof. It is recorded honestly, with no savings claimed.
            self.store.record(agent_id=agent["id"], requested_model=requested, used_model=requested, cost=c,
                              baseline_cost=c, source="shadow", note="quality check: ran the requested model for comparison")

        threading.Thread(target=run, daemon=True).start()


def memory_mod_trim(body, api, policy):
    from .trimmer import trim
    return trim(body, api, policy.get("trim_keep_recent_turns", 6), policy.get("trim_max_old_tool_chars", 1500))
