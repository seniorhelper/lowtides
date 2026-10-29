"""Lowtides MCP server (stdio, JSON-RPC 2.0, newline-delimited).

Run:  python -m lowtides.mcp
Env:  LOWTIDES_URL        base URL of Lowtides (default http://localhost:8787)
      LOWTIDES_AGENT_KEY  the agent's lt- key

Only protocol messages go to stdout. Logs go to stderr.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "lowtides", "version": "0.1.0"}
TIMEOUT = 30

TOOLS = [
    {
        "name": "request_spend",
        "description": (
            "Ask Lowtides for permission to buy something BEFORE you pay for it. Lowtides checks the "
            "purchase against this agent's spend rules and returns approved (Lowtides pays the merchant), "
            "needs_approval (a person must approve; do not proceed until they do) or denied. "
            "Never spend money any other way."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Amount in US dollars, e.g. 12.50."},
                "merchant": {"type": "string", "description": "Who gets paid, e.g. 'Acme Data'."},
                "item": {"type": "string", "description": "What is being bought."},
                "category": {"type": "string", "description": "Optional category, e.g. 'data' or 'software'."},
                "reason": {"type": "string", "description": "Why this purchase is needed for the task."},
                "task_id": {"type": "string", "description": "Optional id of the task this supports."},
            },
            "required": ["amount", "merchant", "item", "reason"],
        },
    },
    {
        "name": "check_spend",
        "description": (
            "Check the current status of a spend request made earlier with request_spend. "
            "Poll this while a request is waiting for approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "The request id, e.g. 'sp_...'."}},
            "required": ["id"],
        },
    },
    {
        "name": "get_budget",
        "description": (
            "Show this agent's budget: status, token spend today, purchase limits, how much it can still "
            "spend today, and its rules."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def log(*a):
    print("[lowtides-mcp]", *a, file=sys.stderr, flush=True)


# ---- HTTP ---------------------------------------------------------------

class ToolError(Exception):
    pass


def _call_api(method, path, body=None):
    base = os.environ.get("LOWTIDES_URL", "http://localhost:8787").rstrip("/")
    key = os.environ.get("LOWTIDES_AGENT_KEY", "").strip()
    if not key:
        raise ToolError("LOWTIDES_AGENT_KEY is not set. Set it to this agent's lt- key in the MCP server config.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        detail = _error_detail(raw)
        if e.code == 401:
            raise ToolError("Lowtides rejected the agent key (401). Check LOWTIDES_AGENT_KEY." + detail)
        if e.code == 404:
            raise ToolError("Not found (404)." + detail)
        raise ToolError("Lowtides returned HTTP %d.%s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise ToolError("Could not reach Lowtides at %s (%s). Is it running?" % (base, e.reason))
    except (OSError, ValueError) as e:  # timeouts, resets, bad URL
        raise ToolError("Could not reach Lowtides at %s (%s)." % (base, e))
    try:
        out = json.loads(raw)
    except ValueError:
        raise ToolError("Lowtides returned a response that is not JSON.")
    if not isinstance(out, dict):
        raise ToolError("Lowtides returned an unexpected response.")
    return out


def _error_detail(raw):
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            msg = j.get("error") or j.get("message")
            if isinstance(msg, dict):
                msg = msg.get("message")
            if msg:
                return " " + str(msg)
    except ValueError:
        pass
    return ""


# ---- Formatting ---------------------------------------------------------

def _money(v):
    try:
        return "${:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _format_spend(d):
    decision = d.get("decision", "unknown")
    status = d.get("status", "unknown")
    lines = ["Decision: %s (status: %s)" % (decision, status)]
    lines.append("Request id: %s" % d.get("id", "?"))
    if d.get("amount") is not None:
        lines.append("Amount: %s to %s" % (_money(d["amount"]), d.get("merchant", "?")))
    reasons = d.get("reasons") or []
    if reasons:
        lines.append("Reasons:")
        lines.extend("- " + str(r) for r in reasons)
    if d.get("flags"):
        lines.append("Flags: " + ", ".join(str(f) for f in d["flags"]))
    p = d.get("payout")
    if isinstance(p, dict) and p:
        lines.append("Payout: %s%s" % (p.get("status", "?"), " (simulated)" if p.get("simulated") else ""))
    if d.get("remaining_today") is not None:
        lines.append("Remaining today: " + _money(d["remaining_today"]))
    lines.append("")
    if decision == "approved":
        if status == "paid":
            lines.append("NEXT: Approved and paid by Lowtides. You may continue with this purchase.")
        else:
            lines.append("NEXT: Approved. You may continue with this purchase.")
    elif decision == "needs_approval":
        if status == "approved":
            lines.append("NEXT: A person has approved this. You may continue.")
        elif status in ("denied", "cancelled"):
            lines.append("NEXT: This request is %s. Do not proceed and do not retry the same purchase." % status)
        else:
            lines.append(
                "NEXT: A person must approve this first. Do NOT proceed with the purchase. "
                "Call check_spend with this id to poll until the status changes."
            )
    elif decision == "denied":
        lines.append("NEXT: Denied. Do not retry the same purchase. Do not look for another way to pay for it. "
                     "Tell the user it was denied and why.")
    return "\n".join(lines)


def _format_budget(d):
    lines = ["Agent: %s" % d.get("agent", "?"), "Status: %s" % d.get("status", "?")]
    t = d.get("tokens") or {}
    if t:
        lines.append("Tokens: %s spent today of %s daily budget" % (_money(t.get("spent_today", 0)), _money(t.get("daily_budget", 0))))
    p = d.get("purchases") or {}
    if p:
        lines.append("Purchases: %s spent today of %s daily limit; %s remaining today" % (
            _money(p.get("spent_today", 0)), _money(p.get("daily_limit", 0)), _money(p.get("remaining_today", 0))))
        lines.append("Auto-approved up to %s; max per purchase %s" % (_money(p.get("auto_approve_up_to", 0)), _money(p.get("max_per_purchase", 0))))
    if d.get("rules"):
        lines.append("Rules: " + json.dumps(d["rules"], sort_keys=True))
    return "\n".join(lines)


# ---- Tools --------------------------------------------------------------

def _tool_request_spend(args):
    for k in ("merchant", "item", "reason"):
        if not isinstance(args.get(k), str) or not args[k].strip():
            raise ToolError("'%s' is required and must be a non-empty string." % k)
    amt = args.get("amount")
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or amt <= 0:
        raise ToolError("'amount' is required and must be a positive number.")
    body = {"amount": amt, "merchant": args["merchant"], "item": args["item"], "reason": args["reason"]}
    for k in ("category", "task_id"):
        if isinstance(args.get(k), str) and args[k]:
            body[k] = args[k]
    return _format_spend(_call_api("POST", "/v1/spend", body))


def _tool_check_spend(args):
    sid = args.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise ToolError("'id' is required and must be a non-empty string.")
    return _format_spend(_call_api("GET", "/v1/spend/" + urllib.parse.quote(sid.strip(), safe="")))


def _tool_get_budget(args):
    return _format_budget(_call_api("GET", "/v1/budget"))



HANDLERS = {"request_spend": _tool_request_spend, "check_spend": _tool_check_spend, "get_budget": _tool_get_budget}


# ---- JSON-RPC -----------------------------------------------------------

def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def handle(msg):
    """Handle one decoded JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        mid = msg.get("id") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None  # a response to us; ignore
        return _err(mid, -32600, "Invalid Request")
    method = msg["method"]
    is_notification = "id" not in msg
    mid = msg.get("id")
    params = msg.get("params")
    if params is None:
        params = {}
    if is_notification:
        return None  # notifications/initialized, cancelled, etc.: no reply ever
    if not isinstance(params, dict):
        return _err(mid, -32602, "Invalid params")

    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Call request_spend before buying anything. Never proceed on needs_approval or denied.",
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name) if isinstance(name, str) else None
        if fn is None:
            return _err(mid, -32602, "Unknown tool: %s" % (name,))
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _err(mid, -32602, "Invalid params: arguments must be an object")
        try:
            text, is_error = fn(args), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # never crash the server
            log("tool crashed:", repr(e))
            text, is_error = "Unexpected error: %s" % e, True
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _err(mid, -32601, "Method not found: %s" % method)


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = _err(None, -32700, "Parse error")
        else:
            if isinstance(msg, list):
                resp = _err(None, -32600, "Batch requests are not supported")
            else:
                try:
                    resp = handle(msg)
                except Exception as e:
                    log("handler crashed:", repr(e))
                    resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
        if resp is not None:
            stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            stdout.flush()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log("started; server =", os.environ.get("LOWTIDES_URL", "http://localhost:8787"))
    serve()


if __name__ == "__main__":
    main()
