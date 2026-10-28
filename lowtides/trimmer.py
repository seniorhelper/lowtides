"""Context trimming: stop paying to re-read the same old tool output.

Safe by design:
  * The system prompt and the most recent turns are never touched.
  * No message or block is ever removed, so tool calls and tool results stay paired.
  * Only the text inside old tool results is shortened, and every cut is marked
    so the model knows something was trimmed.
"""
import copy
import hashlib

from . import pricing

DUP_NOTE = "[Lowtides: identical to a later tool result, removed to save tokens]"


def _cut(text, max_chars):
    if len(text) <= max_chars:
        return text, 0
    head = int(max_chars * 0.6)
    tail = max_chars - head
    removed = len(text) - head - tail
    return text[:head] + "\n[... Lowtides trimmed %d characters of old tool output ...]\n" % removed + text[-tail:], removed


def _tool_slots(body, api):
    """Yield (message_index, getter, setter) for every tool-result text in the request."""
    msgs = body.get("messages") or []
    for i, m in enumerate(msgs):
        if api == "openai":
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                def get(m=m):
                    return m["content"]

                def put(v, m=m):
                    m["content"] = v
                yield i, get, put
        else:
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list):
                for b in content:
                    if b.get("type") != "tool_result":
                        continue
                    c = b.get("content")
                    if isinstance(c, str):
                        def get(b=b):
                            return b["content"]

                        def put(v, b=b):
                            b["content"] = v
                        yield i, get, put
                    elif isinstance(c, list):
                        for sub in c:
                            if sub.get("type") == "text" and isinstance(sub.get("text"), str):
                                def get(s=sub):
                                    return s["text"]

                                def put(v, s=sub):
                                    s["text"] = v
                                yield i, get, put


def trim(body, api, keep_recent=6, max_old_chars=1500):
    """Return (new_body, tokens_saved_estimate). The input body is not modified."""
    new = copy.deepcopy(body)
    msgs = new.get("messages") or []
    cutoff = max(0, len(msgs) - int(keep_recent))
    slots = list(_tool_slots(new, api))
    removed_chars = 0

    # 1) Old duplicates: keep the newest copy, mark older copies.
    seen_later = {}
    for idx, get, put in reversed(slots):
        text = get()
        if len(text) < 200:
            continue
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if h in seen_later and idx < cutoff:
            removed_chars += max(0, len(text) - len(DUP_NOTE))
            put(DUP_NOTE)
        else:
            seen_later[h] = idx

    # 2) Old long outputs: keep the head and tail.
    for idx, get, put in slots:
        if idx >= cutoff:
            continue
        text = get()
        if text == DUP_NOTE:
            continue
        cut, n = _cut(text, int(max_old_chars))
        if n:
            put(cut)
            removed_chars += n

    saved = int(round(removed_chars / pricing.CHARS_PER_TOKEN))
    return new, saved
