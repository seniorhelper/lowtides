"""Answer memory: if an agent asks the exact same thing again, the repeat is free.

Rules that keep it safe (based on known semantic-cache failure modes):
  * Scoped to one agent. One agent never sees another agent's answers.
  * Only plain-text answers are remembered. Responses that call tools are never
    reused, because replaying a tool call could repeat a real action.
  * Exact matches by default. Near-duplicate matching only applies to short,
    single-question requests (300 characters or less) with the exact same model,
    system prompt and settings. It compares only the question itself, keeps
    numbers and symbols (so "5 > 3" never matches "5 < 3"), never matches two
    questions with different numbers, uses a strict threshold, and can be switched off.
  * Everything expires (default 24 hours) and can be cleared from the dashboard.
"""
import hashlib
import json
import re
import time

from . import pricing


def canonical(body):
    keep = {k: body.get(k) for k in ("system", "messages", "tools", "tool_choice", "temperature",
                                     "response_format", "max_tokens", "top_p") if k in body}
    return json.dumps(keep, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def key_for(agent_id, api, requested_model, body):
    raw = "%s|%s|%s|%s" % (agent_id, api, requested_model, canonical(body))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


NEAR_MAX_CHARS = 300


def _words(text):
    # Keep symbols and punctuation attached, so "5 > 3?" and "5 < 3?" stay different.
    # Sentence punctuation at the end of a word is ignored ("today?" == "today").
    words = [w.rstrip(".,!?;:\"')") .lstrip("\"'(") for w in re.findall(r"\S+", text.lower())]
    return [w for w in words if w]


def _shingles(text, n=3):
    words = _words(text)
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def similarity(a, b):
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def short_question(body):
    msgs = [m for m in (body.get("messages") or []) if m.get("role") not in ("system", "developer")]
    if not (len(msgs) == 1 and msgs[0].get("role") == "user" and not body.get("tools")):
        return False
    return 0 < len(question_text(body)) <= NEAR_MAX_CHARS


def question_text(body):
    """Only the user's question. The system prompt is matched exactly via context_key."""
    msgs = body.get("messages") or []
    return pricing.text_of(msgs[-1:]).strip() if msgs else ""


def context_key(requested_model, body):
    """Everything except the question must be identical for a near-duplicate match."""
    keep = {k: v for k, v in body.items() if k not in ("messages", "stream", "stream_options", "metadata", "user")}
    # OpenAI-style system/developer messages are part of the context, matched exactly.
    keep["_system_messages"] = [m for m in (body.get("messages") or []) if m.get("role") in ("system", "developer")]
    raw = requested_model + "|" + json.dumps(keep, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _numbers(text):
    return re.findall(r"\d+(?:[.,]\d+)*", text)


class AnswerMemory:
    def __init__(self, store, policy):
        self.store = store
        self.policy = policy

    def lookup(self, agent_id, api, requested_model, body):
        if not self.policy.get("answer_memory", True):
            return None
        ttl = float(self.policy.get("answer_memory_ttl_hours", 24)) * 3600
        k = key_for(agent_id, api, requested_model, body)
        row = self.store.memory_get(k)
        if row and row["created"] >= time.time() - ttl:
            self.store.memory_hit(k)
            return {"row": row, "match": "exact", "similarity": 1.0}
        thr = float(self.policy.get("near_duplicate_threshold", 0.92))
        if thr <= 0 or thr > 1 or not short_question(body):
            return None
        q = question_text(body)
        prefix = context_key(requested_model, body) + "|"
        best, best_s = None, 0.0
        for cand in self.store.memory_candidates(agent_id, api, time.time() - ttl, prefix):
            cq = cand["text"][len(prefix):]
            if _numbers(cq) != _numbers(q):
                continue  # "order 1234" is never "order 1235"
            s = similarity(q, cq)
            if s > best_s:
                best, best_s = cand, s
        if best and best_s >= thr:
            self.store.memory_hit(best["key"])
            return {"row": best, "match": "near", "similarity": round(best_s, 3)}
        return None

    def remember(self, agent_id, api, requested_model, body, result, baseline_cost):
        if not self.policy.get("answer_memory", True):
            return
        if result.get("tool_calls") or not (result.get("text") or "").strip():
            return
        k = key_for(agent_id, api, requested_model, body)
        text = (context_key(requested_model, body) + "|" + question_text(body)) if short_question(body) else ""
        self.store.memory_put(k, agent_id, api, text, result["response"], baseline_cost)
