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
from .guard import Blocked, Guard, leash_rules, start_of_today
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


def cap_reply(work, api, global_cap, leash_cap, notes):
    """Clamp the reply allowance to the smaller of the global cap and the agent's leash.
    The global cap never touches extended thinking/reasoning requests (the output budget
    must stay above the thinking budget). The leash is the owner's explicit rule, so it
    always applies, and an Anthropic thinking budget is shrunk to fit under it."""
    thinking = bool(work.get("thinking")) or bool(work.get("reasoning_effort"))
    caps = [c for c in ((global_cap if not thinking else 0), leash_cap or 0) if c and c > 0]
    if not caps:
        return work
    limit = min(caps)
    by_leash = bool(leash_cap) and leash_cap == limit
    label = ("reply capped at %d tokens by leash" if by_leash else "output capped at %d tokens") % limit
    keys = [k for k in ("max_tokens", "max_completion_tokens") if isinstance(work.get(k), int)]
    if not keys and by_leash:
        key = "max_completion_tokens" if (api != "anthropic" and work.get("reasoning_effort")) else "max_tokens"
        work = dict(work)
        work[key] = limit
        notes.append("reply limit set to %d tokens by leash" % limit)
    for k in keys:
        if work[k] > limit:
            work = dict(work)
            work[k] = limit
            if label not in notes:
                notes.append(label)
    th = work.get("thinking")
    if by_leash and isinstance(th, dict) and isinstance(th.get("budget_tokens"), int):
        mx = work.get("max_tokens") or limit
        if th["budget_tokens"] >= mx:
            work = dict(work)
            if mx > 1024:  # Anthropic's minimum thinking budget is 1024
                work["thinking"] = dict(th, budget_tokens=mx - 1)
                notes.append("thinking budget cut to %d to fit the reply leash" % (mx - 1))
            else:
                work.pop("thinking")
                notes.append("thinking turned off to fit the reply leash")
    return work


MIN_REPLY_TOKENS = 16        # below this a reply is useless; refuse the call instead
DEFAULT_REPLY_CEILING = 8192  # limit injected when the cap allows more and the request named none


def fit_cost_cap(work, api, per_call, est_in, spec, notes):
    """Make the request carry an explicit reply limit so the worst case
    (est_in x input price + limit x output price) stays within the per-call cap.
    Only ever lowers a limit (the reply leash and global cap still win when smaller).
    Returns (work, too_pricey): too_pricey means not even a MIN_REPLY_TOKENS reply fits."""
    out_price = float(spec.get("output", 0) or 0)
    room = per_call * 1_000_000.0 - est_in * float(spec.get("input", 0) or 0)
    if out_price > 0:
        if room < MIN_REPLY_TOKENS * out_price:
            return work, True
        afford = int(room // out_price)
    elif room < 0:
        return work, True
    else:
        afford = None  # free output: any reply fits
    current = [work[k] for k in ("max_tokens", "max_completion_tokens") if isinstance(work.get(k), int) and work[k] > 0]
    if current:
        if afford is None or min(current) <= afford:
            return work, False
        limit = afford
    else:
        ceiling = int(spec.get("max_output") or DEFAULT_REPLY_CEILING)
        limit = ceiling if afford is None else min(afford, ceiling)
    # Drop any non-int or non-positive limit so cap_reply sets a clean one.
    work = {k: v for k, v in work.items()
            if not (k in ("max_tokens", "max_completion_tokens") and not (isinstance(v, int) and v > 0))}
    extra = []
    work = cap_reply(work, api, 0, limit, extra)
    notes.extend(n for n in extra if not n.startswith(("reply limit set", "reply capped")))
    notes.append("reply limit %d tokens to fit $%.4f per-call leash" % (limit, per_call))
    return work, False


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
        leash = leash_rules(agent)
        notes = []
        work = cap_reply(work, api, int(policy.get("max_output_tokens_cap", 0) or 0), leash["max_reply_tokens"], notes)

        # 5) plan
        forced = headers.get("x-lowtides-tier")
        order, info = self.router.plan(requested, work, forced_tier=forced if forced in ("1", "2", "3") else None)

        # 6) guard. Estimate with the priciest model the cascade could reach, so a climb
        # can't blow through a budget.
        est_in = pricing.estimate_request_tokens(work)
        per_call = leash["max_cost_per_call"]
        too_pricey = False
        if per_call is not None:
            # A per-call cap needs a hard reply limit: without one the reply can run to the
            # model's max and the pre-flight estimate means nothing.
            work, too_pricey = fit_cost_cap(work, api, per_call, est_in,
                                            self.config.model(order[0]) or req_spec, notes)
        limits = [work[k] for k in ("max_tokens", "max_completion_tokens") if isinstance(work.get(k), int) and work[k] > 0]
        out_max = MIN_REPLY_TOKENS if too_pricey else (min(limits) if limits else 1024)
        est_out = min(out_max, 1024)

        # 6a) the leash: prompt size and per-call cost, priced on the model routing picked
        # (the one that answers first), with the full reply allowance.
        def call_cost(m):
            return pricing.cost(self.config.model(m) or req_spec, input_tokens=est_in, output_tokens=out_max)
        try:
            self.guard.check_leash(agent, leash, est_in, order[0], call_cost(order[0]))
        except Blocked as b:
            self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                              used_model="blocked", cost=0.0, baseline_cost=0.0, source="blocked", note=b.code)
            return b.http, blocked_body(api, b.reason, b.code), {}
        if per_call is not None:
            # Don't let a verify-and-climb step land on a model that would break the per-call cap.
            over = [m for m in order[1:] if call_cost(m) > per_call]
            if over:
                order = [m for m in order if m not in over]
                notes.append("leash: won't climb to %s (over $%.4f per call)" % (", ".join(over), per_call))

        est = max(pricing.cost(self.config.model(m) or req_spec, input_tokens=est_in, output_tokens=est_out) for m in order)
        try:
            self.guard.check(agent, task, work, est)
        except Blocked as b:
            self.store.record(agent_id=agent["id"], task_id=task["id"] if task else None, requested_model=requested,
                              used_model="blocked", cost=0.0, baseline_cost=0.0, source="blocked", note=b.code)
            return b.http, blocked_body(api, b.reason, b.code), {}
        dump = self.guard.dump_alert(agent, leash, est_in)
        if dump:
            notes.append(dump)

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
