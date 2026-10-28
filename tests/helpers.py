import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("COSTCOP_QUIET", "1")

from costcop.config import Config  # noqa: E402
from costcop.engine import Engine  # noqa: E402
from costcop.store import Store  # noqa: E402


def config(llm_url=None, **policy):
    over = {"policy": policy} if policy else {}
    if llm_url:
        over["providers"] = {
            "anthropic": {"type": "anthropic", "base_url": llm_url, "api_key_env": "TEST_ANTHROPIC_KEY"},
            "openai": {"type": "openai", "base_url": llm_url + "/v1", "api_key_env": "TEST_OPENAI_KEY"},
        }
        over["models"] = {
            "gpt-small": {"provider": "openai", "tier": 1, "family": "oa", "input": 0.15, "output": 0.60},
            "gpt-big": {"provider": "openai", "tier": 3, "family": "oa", "input": 5.0, "output": 15.0},
        }
        os.environ["TEST_ANTHROPIC_KEY"] = "sk-ant-test"
        os.environ["TEST_OPENAI_KEY"] = "sk-test"
    return Config.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json"), over)


def engine(cfg=None):
    cfg = cfg or config()
    store = Store(":memory:")
    return Engine(cfg, store), store


def agent(store, name="Test agent", daily=5.0, task=1.0, wallet=False):
    aid = store.add_agent(name, daily, task, use_wallet=wallet)
    return store.agent(aid)


def chat(prompt, model="sim-large", **extra):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 300}
    body.update(extra)
    return body
