"""Configuration: a JSON file plus environment variables for secrets."""
import copy
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_PATHS = [
    os.environ.get("LOWTIDES_CONFIG", ""),
    os.path.join(ROOT, "config.json"),
    os.path.join(ROOT, "config.example.json"),
]


def _deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Holds settings. Secrets never live in the file: only env var names do."""

    def __init__(self, data):
        self.data = data

    # ---- loading -------------------------------------------------------
    @classmethod
    def load(cls, path=None, overrides=None):
        paths = [path] if path else [p for p in DEFAULT_PATHS if p]
        data = None
        for p in paths:
            if p and os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                break
        if data is None:
            raise FileNotFoundError("No config file found (looked for config.json and config.example.json)")
        return cls(_deep_merge(data, overrides or {}))

    # ---- accessors -----------------------------------------------------
    @property
    def policy(self):
        return self.data.get("policy", {})

    @property
    def models(self):
        return self.data.get("models", {})

    @property
    def providers(self):
        return self.data.get("providers", {})

    @property
    def paypal(self):
        return self.data.get("paypal", {})

    @property
    def business(self):
        return self.data.get("business", {})

    def model(self, name):
        return self.models.get(name)

    def secret(self, env_name):
        if not env_name:
            return ""
        return os.environ.get(env_name, "")

    def ladder(self, family):
        """Models in one API family, cheapest tier first."""
        items = [(n, m) for n, m in self.models.items() if m.get("family") == family]
        items.sort(key=lambda nm: (nm[1].get("tier", 9), nm[1].get("input", 0)))
        return [n for n, _ in items]

    def update_policy(self, changes):
        allowed = set(self.policy.keys())
        for k, v in changes.items():
            if k in allowed:
                self.data["policy"][k] = v

    def upsert_model(self, name, spec):
        required = {"provider", "tier", "family", "input", "output"}
        missing = required - set(spec)
        if missing:
            raise ValueError("Model is missing: " + ", ".join(sorted(missing)))
        if spec["provider"] not in self.providers:
            raise ValueError("Unknown provider: " + spec["provider"])
        for key in ("input", "output", "cache_write", "cache_read"):
            if key in spec and float(spec[key]) < 0:
                raise ValueError(key + " price cannot be negative")
        self.data["models"][name] = spec

    def public(self):
        """Settings safe to show in the dashboard (no secrets, only whether keys are set)."""
        out = copy.deepcopy(self.data)
        out.pop("_readme", None)
        for name, p in out.get("providers", {}).items():
            env = p.get("api_key_env")
            p["key_set"] = bool(env and os.environ.get(env)) or p.get("type") in ("simulator",) or not env
        pp = out.get("paypal", {})
        pp["configured"] = bool(os.environ.get(pp.get("client_id_env", "")) and os.environ.get(pp.get("client_secret_env", "")))
        return out
