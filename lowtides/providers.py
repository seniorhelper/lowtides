"""Model providers: Anthropic (Claude), any OpenAI-compatible server, and a simulator.

Every provider returns the same shape:
    {"response": <body in the API format the agent asked for>,
     "text": <plain answer text>,
     "usage": {"input": n, "output": n, "cache_read": n, "cache_write": n},
     "usage_estimated": bool,
     "tool_calls": bool}
"""
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request

from . import pricing


class ProviderError(Exception):
    def __init__(self, message, status=0, retryable=False, body=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.body = body


def _post_json(url, payload, headers, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        if v:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error": {"message": raw[:500]}}
        retry = e.code == 429 or e.code >= 500
        detail = ""
        if isinstance(body, dict):
            err = body.get("error")
            detail = err.get("message", "") if isinstance(err, dict) else (err or "")
        hint = " Check the API key." if e.code in (401, 403) else ""
        raise ProviderError(("Provider returned HTTP %d: %s.%s" % (e.code, detail, hint)).replace(": .", "."), e.code, retry, body)
    except (socket.timeout, TimeoutError) as e:
        # The provider may already be generating (and billing) this answer, so don't
        # quietly retry it on another model. Report it instead.
        raise ProviderError("The provider took too long to answer (%s)." % e, 504, False)
    except (urllib.error.URLError, ConnectionError) as e:
        raise ProviderError("Could not reach provider: %s" % e, 0, True)


class AnthropicProvider:
    api = "anthropic"

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def call(self, body, model, extra_headers=None):
        if not self.api_key:
            raise ProviderError("No Anthropic API key set (ANTHROPIC_API_KEY).", 401, False)
        payload = dict(body)
        payload["model"] = model
        payload.pop("stream", None)
        payload.setdefault("max_tokens", 1024)
        hdrs = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        if extra_headers and extra_headers.get("anthropic-beta"):
            hdrs["anthropic-beta"] = extra_headers["anthropic-beta"]
        resp = _post_json(self.base_url + "/v1/messages", payload, hdrs)
        u = resp.get("usage") or {}
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        tools = any(b.get("type") == "tool_use" for b in resp.get("content", []))
        usage = {
            "input": int(u.get("input_tokens", 0)),
            "output": int(u.get("output_tokens", 0)),
            "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_write": int(u.get("cache_creation_input_tokens", 0) or 0),
        }
        estimated = not u
        if estimated:
            usage["input"] = pricing.estimate_request_tokens(payload)
            usage["output"] = pricing.estimate_tokens(text)
        return {"response": resp, "text": text, "usage": usage, "usage_estimated": estimated, "tool_calls": tools}


class OpenAICompatProvider:
    api = "openai"

    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def call(self, body, model, extra_headers=None):
        payload = dict(body)
        payload["model"] = model
        payload.pop("stream", None)
        payload.pop("stream_options", None)
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        resp = _post_json(self.base_url + "/chat/completions", payload, headers)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = pricing.text_of(text)
        tools = bool(msg.get("tool_calls"))
        u = resp.get("usage") or {}
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        usage = {
            "input": max(0, int(u.get("prompt_tokens", 0)) - cached),
            "output": int(u.get("completion_tokens", 0)),
            "cache_read": cached,
            "cache_write": 0,
        }
        estimated = not u
        if estimated:
            usage["input"] = pricing.estimate_request_tokens(payload)
            usage["output"] = pricing.estimate_tokens(text)
        return {"response": resp, "text": text, "usage": usage, "usage_estimated": estimated, "tool_calls": tools}


HARD_WORDS = ("prove", "architecture", "debug", "refactor", "legal", "strategy", "multi-step", "analyze", "derive")


class SimulatorProvider:
    """A fake model for demos and tests. It never pretends to be real:
    every answer starts with [simulated], and the dashboard labels its data as demo data.

    Small simulated models give a weak answer to clearly hard prompts so the
    verify-and-escalate path can be demonstrated honestly."""

    api = "any"

    def __init__(self, models):
        self.models = models
        self.calls = []

    def _answer(self, prompt_text, tier):
        low = prompt_text.lower()
        hard = any(w in low for w in HARD_WORDS)
        if hard and tier <= 1:
            return "I'm not sure."
        digest = hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()[:8]
        last = prompt_text.strip().splitlines()[-1] if prompt_text.strip() else ""
        detail = " with extra depth" if tier >= 3 else ""
        return "[simulated tier-%d answer %s%s] Here is a complete response to: %s" % (tier, digest, detail, last[:160])

    def call(self, body, model, api):
        spec = self.models.get(model, {})
        tier = int(spec.get("tier", 1))
        prompt = pricing.text_of(body.get("messages"))
        text = self._answer(prompt, tier)
        inp = pricing.estimate_request_tokens(body)
        out = pricing.estimate_tokens(text)
        self.calls.append({"model": model, "time": time.time(), "input": inp})
        if api == "anthropic":
            resp = {
                "id": "msg_sim_" + hashlib.sha1((model + prompt).encode()).hexdigest()[:12],
                "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": inp, "output_tokens": out},
            }
        else:
            resp = {
                "id": "chatcmpl_sim_" + hashlib.sha1((model + prompt).encode()).hexdigest()[:12],
                "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out},
            }
        return {"response": resp, "text": text,
                "usage": {"input": inp, "output": out, "cache_read": 0, "cache_write": 0},
                "usage_estimated": False, "tool_calls": False, "simulated": True}


class Providers:
    """Builds provider clients from config and dispatches calls by model name."""

    def __init__(self, config):
        self.config = config
        self.sim = SimulatorProvider(config.models)
        self._cache = {}

    def client(self, provider_name):
        if provider_name in self._cache:
            return self._cache[provider_name]
        spec = self.config.providers.get(provider_name)
        if not spec:
            raise ProviderError("Unknown provider: %s" % provider_name)
        kind = spec.get("type")
        key = self.config.secret(spec.get("api_key_env", ""))
        if kind == "anthropic":
            c = AnthropicProvider(spec.get("base_url", "https://api.anthropic.com"), key)
        elif kind == "openai":
            c = OpenAICompatProvider(spec.get("base_url", "https://api.openai.com/v1"), key)
        elif kind == "simulator":
            c = self.sim
        else:
            raise ProviderError("Unsupported provider type: %s" % kind)
        self._cache[provider_name] = c
        return c

    def call(self, model, body, api, extra_headers=None):
        spec = self.config.model(model)
        if not spec:
            raise ProviderError("Unknown model: %s (add it on the Models screen)" % model, 400)
        c = self.client(spec["provider"])
        if isinstance(c, SimulatorProvider):
            return c.call(body, model, api)
        if c.api != api:
            raise ProviderError("Model %s speaks the %s API, not %s" % (model, c.api, api), 400)
        return c.call(body, model, extra_headers)
