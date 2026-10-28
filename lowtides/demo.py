"""A demo that runs real Lowtides logic against the built-in simulator.

Nothing here is faked in the pipeline: requests go through the same trimming,
memory, routing, verification, budgets and ledger as real traffic. Only the
model at the end is a simulator, and every row it produces is labeled as such.
"""
from .engine import Engine

DEMO_NAME = "Demo Assistant (simulated)"

LONG_TOOL_OUTPUT = ("Row data: " + ", ".join("item-%03d price $%d.99 in stock" % (i, i % 40) for i in range(900)))

SCRIPT = [
    ("Classify this support email as billing, tech or sales: 'My card was charged twice.'", None),
    ("Summarize this in one line: The meeting moved from Tuesday to Thursday at 3pm.", None),
    ("Translate to Spanish: Your order has shipped.", None),
    ("Extract the phone number: Call Dana at 303-555-0148 after 5.", None),
    ("Classify this support email as billing, tech or sales: 'My card was charged twice.'", None),  # repeat -> memory
    ("Debug this: my Python loop never ends when the list is empty. Explain the architecture fix.", None),  # hard
    ("Write a full step by step plan to launch a bakery website.", None),
    ("Tag the sentiment: 'Absolutely love the new update!'", None),
    ("Rewrite politely: Send me the report now.", None),
    ("Summarize this in one line: The meeting moved from Tuesday to Thursday at 3pm.", None),  # repeat -> memory
]


def ensure_demo_agent(store):
    for a in store.agents():
        if a["name"] == DEMO_NAME:
            return a
    aid = store.add_agent(DEMO_NAME, daily_budget=2.00, task_budget=0.50)
    return store.agent(aid)


def run_demo(engine: Engine, model="sim-large"):
    store = engine.store
    agent = ensure_demo_agent(store)
    if agent["status"] != "on":
        store.update_agent(agent["id"], status="on")
        agent = store.agent(agent["id"])
    results = []
    task_headers = {"X-Lowtides-Task": "demo-" + str(int(__import__("time").time())), "X-Lowtides-Task-Label": "demo run"}
    for prompt, _ in SCRIPT:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 400}
        status, resp, headers = engine.handle("openai", body, agent["key"], task_headers)
        results.append({"prompt": prompt, "status": status, "model": headers.get("X-Lowtides-Model-Used"),
                        "cost": headers.get("X-Lowtides-Cost"), "saved": headers.get("X-Lowtides-Saved")})
    # An agent dragging a big old tool result through every turn (the "context snowball").
    history = [{"role": "system", "content": "You are an inventory agent."},
               {"role": "user", "content": "Check stock."},
               {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_stock", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "call_1", "content": LONG_TOOL_OUTPUT}]
    for i in range(12):
        history = history + [{"role": "assistant", "content": "Noted step %d." % i},
                             {"role": "user", "content": "Next: list item-%03d status only." % (i * 7)}]
        body = {"model": model, "messages": history, "max_tokens": 200}
        status, resp, headers = engine.handle("openai", body, agent["key"], task_headers)
        results.append({"prompt": "inventory step %d" % i, "status": status, "model": headers.get("X-Lowtides-Model-Used"),
                        "cost": headers.get("X-Lowtides-Cost"), "saved": headers.get("X-Lowtides-Saved")})
    return {"agent_id": agent["id"], "results": results}


def run_loop_demo(engine: Engine, model="sim-medium"):
    """Shows the loop guard: the same request over and over gets the agent paused."""
    store = engine.store
    agent = ensure_demo_agent(store)
    store.update_agent(agent["id"], status="on")
    agent = store.agent(agent["id"])
    body = {"model": model, "messages": [{"role": "user", "content": "Retry the failed upload step again."}],
            "max_tokens": 100}
    out = []
    for _ in range(6):
        status, resp, headers = engine.handle("openai", body, agent["key"], {"X-Lowtides-Memory": "off"})
        out.append(status)
        if status != 200:
            break
    return {"agent_id": agent["id"], "statuses": out, "agent_status": store.agent(agent["id"])["status"]}
