"""Routing: start each step on the cheapest model that can likely handle it,
check the answer, and climb the ladder only when needed.

One promise: Lowtides never moves you UP to a pricier model than the one your
agent asked for. The requested model is the ceiling, never the floor.
"""
import json
import re

from . import pricing

HARD = re.compile(r"\b(code|coding|debug|bug|refactor|architect\w*|prove|proof|derive|analy[sz]e|analysis|strategy|"
                  r"plan|legal|contract|math|equation|reason|why|compare|trade-?offs?|design|multi-step|research)\b", re.I)
EASY = re.compile(r"\b(classify|categori[sz]e|extract|summari[sz]e|translate|format|reformat|rewrite|rephrase|"
                  r"yes or no|true or false|tag|label|sentiment|spell|grammar|list the|convert)\b", re.I)
UNSURE = re.compile(r"^\s*(i'?m not sure|i am not sure|i don'?t know|i do not know|i can'?t (help|answer|determine)|"
                    r"i cannot (help|answer|determine)|unable to (help|answer|determine))", re.I)
WANTS_JSON = re.compile(r"\b(return|respond|reply|output)\b[^.\n]{0,40}\bjson\b", re.I)
WANTS_LONG = re.compile(r"\b(write|draft|explain in detail|step by step|full|complete|essay|article|report)\b", re.I)


def last_user_text(body):
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            return pricing.text_of(m.get("content"))
    return ""


def grade(body):
    """Return (tier 1-3, reasons). Free and instant: no model call."""
    text = last_user_text(body)
    score, why = 0, []
    if HARD.search(text):
        score += 2
        why.append("hard words")
    if EASY.search(text) and not HARD.search(text):
        score -= 1
        why.append("simple task words")
    if body.get("tools"):
        score += 1
        why.append("uses tools")
    if pricing.estimate_tokens(text) > 2000:
        score += 1
        why.append("long request")
    if pricing.estimate_request_tokens(body) > 20000:
        score += 1
        why.append("big context")
    tier = 1 if score <= 0 else (2 if score <= 2 else 3)
    return tier, why or ["short, plain request"]


def wants_json(body):
    rf = body.get("response_format") or {}
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        return True
    return bool(WANTS_JSON.search(last_user_text(body)))


def weak_answer(body, result):
    """Return a reason string if the answer looks weak, else ''."""
    if result.get("tool_calls"):
        return ""
    text = (result.get("text") or "").strip()
    if not text:
        return "empty answer"
    if UNSURE.search(text):
        return "model said it was unsure"
    if wants_json(body):
        candidate = text
        fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fence:
            candidate = fence.group(1)
        try:
            json.loads(candidate)
        except ValueError:
            return "asked for JSON but the answer is not valid JSON"
    if WANTS_LONG.search(last_user_text(body)) and len(text) < 40:
        return "answer far too short for the request"
    return ""


class Router:
    def __init__(self, config):
        self.config = config

    def plan(self, requested_model, body, forced_tier=None):
        """Return the ordered list of models to try (cheapest sensible first)."""
        policy = self.config.policy
        spec = self.config.model(requested_model)
        if not spec:
            return [requested_model], {"tier": None, "reasons": ["model not in your price list"]}
        mode = policy.get("routing", "cascade")
        if mode == "off" or not policy.get("allow_downgrade", True):
            return [requested_model], {"tier": spec.get("tier"), "reasons": ["routing is off"]}
        ladder = self.config.ladder(spec["family"])
        ceiling = spec.get("tier", 3)
        tier, reasons = grade(body)
        if forced_tier:
            tier, reasons = int(forced_tier), ["tier set by the agent"]
        tier = min(tier, ceiling)
        eligible = [m for m in ladder if self.config.model(m).get("tier", 9) <= ceiling]
        start = [m for m in eligible if self.config.model(m).get("tier", 9) >= tier]
        if not start:
            start = [requested_model]
        if mode == "route" or not policy.get("verify_and_escalate", True):
            return start[:1], {"tier": tier, "reasons": reasons}
        # cascade: start tier, then each higher tier up to the requested model
        seen, order = set(), []
        for m in start:
            t = self.config.model(m).get("tier")
            if t in seen:
                continue
            seen.add(t)
            order.append(m)
        if requested_model not in order:
            order = [m for m in order if self.config.model(m).get("tier", 9) < ceiling] + [requested_model]
        return order, {"tier": tier, "reasons": reasons}
