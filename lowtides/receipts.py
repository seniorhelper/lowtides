"""Plain-English savings receipts and CSV export."""
import csv
import datetime
import io

from .pricing import money


def _n(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def receipt(store, agent_id=None, since=None, until=None, client_id=None, title="Your AI agents"):
    t = store.totals(agent_id=agent_id, since=since, until=until, client_id=client_id)
    pct = (t["saved"] / t["baseline"] * 100.0) if t["baseline"] > 0 else 0.0
    lines = [
        "%s made %d call%s%s." % (title, t["calls"], "" if t["calls"] == 1 else "s",
                                  (" across %d job%s" % (t["tasks"], "" if t["tasks"] == 1 else "s")) if t["tasks"] else ""),
        "Unmanaged, that would have cost %s. You paid %s. Saved: %s (%.0f%%)." % (
            money(t["baseline"]), money(t["cost"]), money(t["saved"]), pct),
    ]
    if t["memory_hits"]:
        lines.append("%s answered from memory for free." % _n(t["memory_hits"], "repeat question was", "repeat questions were"))
    if t["rerouted"]:
        lines.append("%s on a cheaper model that handled %s." % (_n(t["rerouted"], "step ran", "steps ran"), "it" if t["rerouted"] == 1 else "them"))
    if t["escalations"]:
        lines.append("%d times a cheap answer looked weak, so Lowtides moved up a model. Those attempts are included in what you paid."
                     % t["escalations"])
    if t["trimmed_tokens"]:
        lines.append("About %s tokens of old, repeated context were trimmed." % "{:,}".format(t["trimmed_tokens"]))
    if t["blocked"]:
        lines.append("%s stopped by your rules (budgets, the STOP button or the loop guard) and cost nothing." % _n(t["blocked"], "call was", "calls were"))
    if t["estimated_rows"]:
        lines.append("Note: %d calls came from a server that didn't report token counts, so their costs are estimates."
                     % t["estimated_rows"])
    return {"totals": t, "percent_saved": round(pct, 1), "lines": lines, "text": "\n".join(lines)}


def ledger_csv(rows):
    buf = io.StringIO()
    cols = ["id", "time", "agent", "task", "requested_model", "used_model", "source", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "trimmed_tokens", "cost_usd", "baseline_usd", "saved_usd",
            "escalations", "usage_estimated", "note"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r["id"], datetime.datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"), r.get("agent_name", ""),
                    r.get("task_id") or "", r["requested_model"], r["used_model"], r["source"], r["input_tokens"],
                    r["output_tokens"], r["cache_read_tokens"], r["cache_write_tokens"], r["trimmed_tokens"],
                    "%.6f" % r["cost"], "%.6f" % r["baseline_cost"], "%.6f" % (r["baseline_cost"] - r["cost"]),
                    r["escalations"], r["usage_estimated"], r["note"]])
    return buf.getvalue()
