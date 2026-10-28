"""Token counting and dollar math.

Real token counts come from the provider's usage report whenever it gives one.
When it doesn't (some local servers), we estimate at about 4 characters per token
and mark that ledger row as estimated, so the dashboard never presents a guess as a fact.
"""
import json

CHARS_PER_TOKEN = 4.0


def text_of(obj):
    """Flatten any message structure into plain text for size estimates."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        return "\n".join(text_of(x) for x in obj)
    if isinstance(obj, dict):
        parts = []
        for k in ("text", "content", "input", "arguments", "name", "description", "thinking"):
            if k in obj:
                parts.append(text_of(obj[k]))
        if "function" in obj:
            parts.append(text_of(obj["function"]))
        if "parameters" in obj or "input_schema" in obj:
            parts.append(json.dumps(obj.get("parameters") or obj.get("input_schema")))
        return "\n".join(p for p in parts if p)
    return str(obj)


def estimate_tokens(obj):
    t = text_of(obj)
    if not t:
        return 0
    return max(1, int(round(len(t) / CHARS_PER_TOKEN)))


def estimate_request_tokens(body):
    """Estimated input tokens for an OpenAI- or Anthropic-style request body."""
    total = 0
    total += estimate_tokens(body.get("system"))
    total += estimate_tokens(body.get("messages"))
    total += estimate_tokens(body.get("tools"))
    return total


def cost(model_spec, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
    """Dollar cost. input_tokens means uncached input only."""
    if not model_spec:
        return 0.0
    per = 1_000_000.0
    cr = model_spec.get("cache_read", model_spec.get("input", 0))
    cw = model_spec.get("cache_write", model_spec.get("input", 0))
    return round(
        (input_tokens * model_spec.get("input", 0)
         + output_tokens * model_spec.get("output", 0)
         + cache_read * cr
         + cache_write * cw) / per, 8)


def money(x):
    """Human money: cents for small amounts, never fake precision."""
    if x is None:
        return "$0"
    if abs(x) < 0.01 and x != 0:
        return "<$0.01"
    return "${:,.2f}".format(x)
