"""Price tag before the work starts, like a contractor's estimate.

Agents can't predict their own token bills (Stanford Digital Economy Lab, 2026).
CostCop can, from history: it looks at what similar finished tasks for this agent
actually cost. With no history yet, it gives a clearly labeled rough estimate.
"""
from . import pricing


def _pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def quote(store, config, agent_id, label="", description="", model=None, steps=None):
    closed = [t for t in store.tasks(agent_id, limit=500) if t["status"] in ("done", "settled")]
    same = [t for t in closed if label and t["label"].strip().lower() == label.strip().lower()]
    pool = same if len(same) >= 3 else (closed if len(closed) >= 5 else [])
    if pool:
        spends = [store.task_spend(t["id"]) for t in pool]
        low, mid, high = _pct(spends, 0.25), _pct(spends, 0.5), _pct(spends, 0.9) * 1.2
        basis = "%d finished %s tasks" % (len(pool), "'" + label + "'" if pool is same else "similar")
        return {"low": round(low, 4), "likely": round(mid, 4), "high": round(max(high, mid), 4),
                "method": "history", "basis": basis}
    # Rough estimate: size of the request x expected steps x a mid-tier price.
    ladder_models = [m for m in config.models.values() if m.get("family") != "simulator"] or list(config.models.values())
    spec = config.model(model) if model else None
    if not spec:
        mids = sorted(ladder_models, key=lambda m: m.get("tier", 2))
        spec = mids[len(mids) // 2] if mids else {"input": 2.0, "output": 10.0}
    n_steps = int(steps or 6)
    per_step_in = max(800, pricing.estimate_tokens(description) * 3)
    growth = sum(per_step_in * (1 + 0.5 * i) for i in range(n_steps))
    out = 400 * n_steps
    likely = pricing.cost(spec, input_tokens=int(growth), output_tokens=out)
    return {"low": round(likely * 0.4, 4), "likely": round(likely, 4), "high": round(likely * 2.5, 4),
            "method": "rough", "basis": "rough estimate: no finished tasks yet, assumes ~%d steps" % n_steps}
