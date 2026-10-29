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


# Non-text content (images, PDFs, audio, files). text_of() skips these, so count them
# separately and conservatively: base64 payloads at len/4 tokens, a bare image URL at a
# fixed allowance, anything unrecognised by its JSON size.
IMAGE_URL_TOKENS = 1600
FILE_REF_TOKENS = 1600  # a URL or file id we can't measure
MEDIA_TYPES = {"image", "document", "image_url", "input_image", "input_audio", "file", "input_file"}
TEXT_TYPES = {"text", "input_text", "output_text", "tool_use", "tool_result", "thinking", "function",
              "refusal", "message", "custom", "server_tool_use"}


def _len_tokens(s):
    return int(-(-len(s) // 4)) if s else 0  # ceil(len/4)


def _ref_tokens(x, fixed):
    """A data: URL or raw base64 string is measured; a plain URL/id gets a fixed allowance."""
    if isinstance(x, dict):
        x = x.get("url") or x.get("file_data") or x.get("data") or x.get("file_id") or ""
    if not isinstance(x, str) or not x:
        return 0
    if x.startswith("data:") or not x.startswith(("http://", "https://", "file-", "gs://", "s3://")):
        return max(_len_tokens(x), 1) if len(x) > 64 else fixed
    return fixed


def _source_tokens(src, fixed):
    if not isinstance(src, dict):
        return _ref_tokens(src, fixed)
    kind = src.get("type")
    if kind in ("base64", "text") and isinstance(src.get("data"), str):
        return _len_tokens(src["data"])
    if kind == "content":
        return estimate_tokens(src.get("content"))
    return fixed  # url / file id


def _media_block_tokens(b):
    t = b.get("type")
    if t in ("image", "document"):  # Anthropic
        fixed = IMAGE_URL_TOKENS if t == "image" else FILE_REF_TOKENS
        return _source_tokens(b.get("source"), fixed)
    if t in ("image_url", "input_image"):  # OpenAI
        return _ref_tokens(b.get("image_url") or b.get("file_id"), IMAGE_URL_TOKENS)
    if t == "input_audio":
        a = b.get("input_audio") or {}
        data = a.get("data") if isinstance(a, dict) else a
        return _len_tokens(data) if isinstance(data, str) else FILE_REF_TOKENS
    f = b.get("file") if isinstance(b.get("file"), dict) else b  # file / input_file
    data = f.get("file_data")
    if isinstance(data, str) and data:
        return _len_tokens(data)
    return _ref_tokens(f.get("file_url") or f.get("file_id"), FILE_REF_TOKENS) or FILE_REF_TOKENS


def media_tokens(obj):
    """Tokens for non-text content text_of() can't see."""
    if isinstance(obj, list):
        return sum(media_tokens(x) for x in obj)
    if not isinstance(obj, dict):
        return 0
    t = obj.get("type")
    if t in MEDIA_TYPES:
        return _media_block_tokens(obj)
    if isinstance(t, str) and t not in TEXT_TYPES:
        # Unknown block: count its whole serialized size (minus what text_of already counted).
        try:
            size = _len_tokens(json.dumps(obj, default=str))
        except (TypeError, ValueError):
            size = _len_tokens(str(obj))
        t_txt = text_of(obj)
        return max(0, size - (int(round(len(t_txt) / CHARS_PER_TOKEN)) if t_txt else 0))
    return media_tokens(obj.get("content"))


def estimate_tokens(obj):
    t = text_of(obj)
    n = max(1, int(round(len(t) / CHARS_PER_TOKEN))) if t else 0
    return n + media_tokens(obj)


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
