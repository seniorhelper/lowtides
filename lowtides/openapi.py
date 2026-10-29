"""OpenAPI 3.1 description of the Lowtides API, from one source.

This dict is the single source of truth. The server serves it as JSON at /openapi.json,
and docs/openapi.yaml is generated from it:

    python3 -m lowtides.openapi --write      # rewrites docs/openapi.yaml

A test fails if docs/openapi.yaml drifts from this file. Feed either one to APIMatic
(SDKs, docs, a context plugin) or import it into Postman.
"""
import json
import os
import re
import sys

from . import __version__

ERR = {"$ref": "#/components/schemas/Error"}


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


def _json(schema, description="OK", example=None):
    content = {"schema": schema}
    if example is not None:
        content["example"] = example
    return {"description": description, "content": {"application/json": content}}


def _body(schema, example=None, required=True):
    content = {"schema": schema}
    if example is not None:
        content["example"] = example
    return {"required": required, "content": {"application/json": content}}


def _errors(*codes):
    text = {400: "Bad request: the reason is in `error`.", 401: "Missing or unknown key (or admin password).",
            403: "Refused (cross-site request, or turned off in the public demo).", 404: "Not found.",
            409: "Conflict: the request is no longer in a state that allows this.",
            415: "Send JSON with Content-Type: application/json."}
    return {str(c): _json(ERR, text[c]) for c in codes}


PATH_ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Spend request ID (sp_...)."}
AGENT_ID = {"name": "agent_id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "Agent ID."}
HOOK_ID = {"name": "webhook_id", "in": "path", "required": True, "schema": {"type": "integer"}, "description": "Webhook ID."}
AGENT_AUTH = [{"bearerAuth": []}, {"apiKeyHeader": []}]
ADMIN_AUTH = [{"adminToken": []}, {"adminCookie": []}, {}]
BLOCKED_TEXT = ("Lowtides refused the call before it reached a model, so it cost nothing. 402: over budget; "
                "403: agent stopped or paused; 429: loop detected or rate limited. `error.code` says which.")

SPEND_EXAMPLE = {"amount": 12.50, "merchant": "Acme Data", "item": "1,000 enrichment credits", "category": "data",
                 "reason": "Needed to finish the lead list"}
DECISION_EXAMPLE = {
    "id": "sp_3f9a1c0b7d2e4a61", "ts": 1793301188.08, "agent_id": 1, "agent_name": "Research bot", "task_id": None,
    "amount": 12.5, "merchant": "Acme Data", "item": "1,000 enrichment credits", "category": "data",
    "reason": "Needed to finish the lead list", "decision": "approved", "status": "paid",
    "reasons": ["Within the $20.00 auto-approve limit and today's $200.00 budget.", "Acme Data is a known merchant."],
    "flags": [], "payee": "merchant-demo@example.com", "payout_batch_id": "5UXD2E8A7EBQJ", "payout_item_id": "8AELMXH8UB2P8",
    "payout_status": "UNCLAIMED", "payout_env": "sandbox", "simulated": False, "decided_by": "rules", "decided_ts": 1793301188.4,
    "note": "", "payout_proof": "PayPal sandbox payout batch 5UXD2E8A7EBQJ, item 8AELMXH8UB2P8 (UNCLAIMED)"}

SCHEMAS = {
    "Error": {"type": "object", "required": ["error"], "properties": {
        "error": {"type": "string", "description": "What went wrong, in plain English."},
        "issue": {"type": "string", "description": "PayPal issue code, when PayPal refused something."}}},
    "SpendRequest": {"type": "object", "required": ["amount", "merchant"], "additionalProperties": False, "properties": {
        "amount": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000, "description": "Dollars."},
        "merchant": {"type": "string", "maxLength": 120, "description": "Who is being paid. Must match the merchant list to auto-approve."},
        "item": {"type": "string", "maxLength": 300, "description": "What is being bought."},
        "category": {"type": "string", "maxLength": 60, "description": "For category rules, e.g. data, software, travel."},
        "reason": {"type": "string", "maxLength": 1000, "description": "Why the agent needs it. Checked for prompt-injection text."},
        "task_id": {"type": "string", "maxLength": 80, "description": "Optional task to charge it to."},
        "settle": {"type": "string", "enum": ["external"],
                   "description": "Set to external when the caller (e.g. PayPal's Agent Toolkit) moves the money itself."}}},
    "SpendDecision": {"type": "object", "required": ["id", "decision", "status", "amount", "merchant", "reasons", "flags"], "properties": {
        "id": {"type": "string", "pattern": "^sp_[0-9a-f]{16}$"},
        "ts": {"type": "number", "description": "Unix time the request was made."},
        "agent_id": {"type": "integer"}, "agent_name": {"type": ["string", "null"]},
        "task_id": {"type": ["string", "null"]},
        "amount": {"type": "number"}, "merchant": {"type": "string"}, "item": {"type": "string"},
        "category": {"type": "string"}, "reason": {"type": "string"},
        "decision": {"type": "string", "enum": ["approved", "needs_approval", "denied"],
                     "description": "What the rules decided. Code decides, never the AI."},
        "status": {"type": "string", "enum": ["approved", "pending", "paid", "denied", "cancelled", "failed"],
                   "description": "Where the money is now. pending = waiting for a person; failed = no money moved."},
        "reasons": {"type": "array", "items": {"type": "string"}, "description": "Plain-English reasons for the decision."},
        "flags": {"type": "array", "items": {"type": "string", "enum": [
            "new_merchant", "possible_injection", "duplicate", "unusual_amount", "velocity", "paid_by_caller"]}},
        "payee": {"type": "string", "description": "The PayPal email paid."},
        "payout_batch_id": {"type": ["string", "null"]}, "payout_item_id": {"type": ["string", "null"]},
        "payout_status": {"type": "string", "description": "PayPal payout item status (SUCCESS, UNCLAIMED, PENDING, RETURNED...), "
                                                           "SIMULATED without PayPal keys, or PAID_BY_CALLER."},
        "payout_env": {"type": "string", "enum": ["", "sandbox", "live"]},
        "payout_proof": {"type": "string", "description": "One line you can check against PayPal."},
        "simulated": {"type": "boolean"},
        "decided_by": {"type": ["string", "null"]}, "decided_ts": {"type": ["number", "null"]},
        "note": {"type": "string"}}},
    "Budget": {"type": "object", "required": ["agent", "status", "tokens", "purchases", "rules"], "properties": {
        "agent": {"type": "string"},
        "status": {"type": "string", "enum": ["on", "paused", "stopped"]},
        "tokens": {"type": "object", "properties": {"daily_budget": {"type": "number"}, "spent_today": {"type": "number"}}},
        "purchases": {"type": "object", "properties": {
            "daily_limit": {"type": "number"}, "spent_today": {"type": "number"}, "remaining_today": {"type": "number"},
            "auto_approve_up_to": {"type": "number"}, "max_per_purchase": {"type": "number"}}},
        "wallet_balance": {"type": ["number", "null"], "description": "Prepaid PayPal-funded wallet, when the agent uses one."},
        "rules": _ref("Rules"),
        "rules_in_plain_english": {"type": "array", "items": {"type": "string"}}}},
    "Rules": {"type": "object", "description": "Per-agent spending rules. Missing fields take their defaults.", "properties": {
        "max_per_purchase": {"type": "number", "default": 50},
        "daily_purchase_limit": {"type": "number", "default": 200},
        "approval_over": {"type": "number", "default": 20, "description": "Purchases above this wait for a person."},
        "daily_total_limit": {"type": ["number", "null"], "description": "AI usage plus purchases together. null = off."},
        "allowed_categories": {"type": "array", "items": {"type": "string"}, "description": "Empty = any category not blocked."},
        "blocked_categories": {"type": "array", "items": {"type": "string"}, "default": ["gift cards", "crypto"]},
        "blocked_merchants": {"type": "array", "items": {"type": "string"}},
        "new_merchant_needs_approval": {"type": "boolean", "default": True},
        "hours": {"type": ["object", "null"], "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}}},
        "max_purchases_per_hour": {"type": "integer", "default": 10},
        "max_reply_tokens": {"type": ["integer", "null"]}, "max_input_tokens": {"type": ["integer", "null"]},
        "max_cost_per_call": {"type": ["number", "null"]}, "dump_alert_multiple": {"type": ["number", "null"], "default": 5}}},
    "RulesCompileResult": {"type": "object", "properties": {
        "rules": _ref("Rules"), "set": {"type": "array", "items": {"type": "string"}},
        "source": {"type": "string", "enum": ["claude", "offline"]}, "model": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}}}},
    "Agent": {"type": "object", "properties": {
        "id": {"type": "integer"}, "name": {"type": "string"},
        "key": {"type": "string", "pattern": "^lt-", "description": "The agent's Lowtides key: use it as the API key."},
        "daily_budget": {"type": "number"}, "task_budget": {"type": "number"},
        "status": {"type": "string", "enum": ["on", "paused", "stopped"]}, "client_id": {"type": ["integer", "null"]},
        "use_wallet": {"type": "integer"}, "rules": {"type": ["string", "null"]}, "created": {"type": "number"}}},
    "AgentCreate": {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "daily_budget": {"type": "number", "exclusiveMinimum": 0, "description": "AI usage, dollars a day."},
        "task_budget": {"type": "number", "exclusiveMinimum": 0}, "client_id": {"type": ["integer", "null"]},
        "use_wallet": {"type": "boolean", "description": "Spend only from a prepaid PayPal-funded wallet."}}},
    "State": {"type": "object", "additionalProperties": True, "description": "Everything the dashboard shows.", "properties": {
        "version": {"type": "string"}, "mode": {"type": "string", "enum": ["home", "business"]},
        "agents": {"type": "array", "items": _ref("Agent")}, "events": {"type": "array", "items": {"type": "object"}},
        "public_demo": {"type": "boolean"}, "paypal_ready": {"type": "boolean"}}},
    "SpendList": {"type": "object", "properties": {"requests": {"type": "array", "items": _ref("SpendDecision")}}},
    "KillResult": {"type": "object", "properties": {
        "stopped": {"type": "boolean"}, "denied": {"type": "integer"}, "holds_voided": {"type": "integer"},
        "payouts_cancelled": {"type": "integer"}, "problems": {"type": "array", "items": {"type": "string"}}}},
    "StopAllResult": {"type": "object", "properties": {
        "ok": {"type": "boolean"}, "denied": {"type": "integer"}, "holds_voided": {"type": "integer"},
        "payouts_cancelled": {"type": "integer"}}},
    "Merchant": {"type": "object", "properties": {
        "id": {"type": "integer"}, "name": {"type": "string"}, "paypal_email": {"type": "string"},
        "category": {"type": "string"}, "created": {"type": "number"}}},
    "Forecast": {"type": "object", "properties": {"generated": {"type": "number"}, "agents": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"}, "status": {"type": "string"},
            "tokens_today": {"type": "number"}, "purchases_today": {"type": "number"}, "spent_today": {"type": "number"},
            "limit": {"type": ["number", "null"]}, "limit_kind": {"type": "string"}, "rate_per_hour": {"type": "number"},
            "projected_today": {"type": "number"}, "minutes_to_limit": {"type": ["number", "null"]}}}}}},
    "AuditVerify": {"type": "object", "properties": {
        "ok": {"type": "boolean"}, "count": {"type": "integer"}, "broken_at": {"type": ["integer", "null"]}}},
    "ModelList": {"type": "object", "properties": {"object": {"const": "list"}, "data": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "object": {"const": "model"}, "owned_by": {"type": ["string", "null"]}}}}}},
    "ChatCompletionRequest": {"type": "object", "required": ["model", "messages"], "additionalProperties": True,
                              "description": "OpenAI Chat Completions request. Other OpenAI fields pass through.", "properties": {
        "model": {"type": "string", "description": "The model asked for. Lowtides may route an easy step to a cheaper one."},
        "messages": {"type": "array", "items": {"type": "object", "properties": {
            "role": {"type": "string"}, "content": {}}, "required": ["role"]}},
        "max_tokens": {"type": "integer"}, "stream": {"type": "boolean"}, "tools": {"type": "array", "items": {"type": "object"}}}},
    "ChatCompletion": {"type": "object", "additionalProperties": True, "properties": {
        "id": {"type": "string"}, "object": {"type": "string"}, "model": {"type": "string"},
        "choices": {"type": "array", "items": {"type": "object"}}, "usage": {"type": "object"}}},
    "MessageRequest": {"type": "object", "required": ["model", "messages", "max_tokens"], "additionalProperties": True,
                       "description": "Anthropic Messages request. Other Anthropic fields pass through.", "properties": {
        "model": {"type": "string"}, "max_tokens": {"type": "integer"}, "system": {},
        "messages": {"type": "array", "items": {"type": "object"}}, "stream": {"type": "boolean"}}},
    "Message": {"type": "object", "additionalProperties": True, "properties": {
        "id": {"type": "string"}, "type": {"type": "string"}, "model": {"type": "string"},
        "content": {"type": "array", "items": {"type": "object"}}, "usage": {"type": "object"}}},
    "BlockedError": {"type": "object", "description": "OpenAI-style or Anthropic-style error body.", "properties": {
        "type": {"type": "string"}, "error": {"type": "object", "properties": {
            "type": {"type": "string", "examples": ["lowtides_blocked"]},
            "code": {"type": "string", "examples": ["over_daily_budget", "agent_stopped", "agent_paused", "loop_detected"]},
            "message": {"type": "string"}}}}},
    "WebhookCreate": {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string", "format": "uri", "description": "For example a Zapier Catch Hook URL."},
        "label": {"type": "string", "maxLength": 80},
        "events": {"type": "array", "items": _ref("EventType"), "description": "Empty or missing = every event."}}},
    "Webhook": {"type": "object", "properties": {
        "id": {"type": "integer"}, "url": {"type": "string"}, "label": {"type": "string"},
        "events": {"type": "array", "items": _ref("EventType")}, "active": {"type": "boolean"},
        "created": {"type": "number"}, "secret_hint": {"type": "string"}}},
    "WebhookCreated": {"type": "object", "properties": {
        "webhook": _ref("Webhook"),
        "secret": {"type": "string", "description": "Signing secret (whsec_...). Shown only once: save it now."}}},
    "WebhookDelivery": {"type": "object", "properties": {
        "id": {"type": "integer"}, "ts": {"type": "number"}, "webhook_id": {"type": "integer"}, "event_id": {"type": "string"},
        "event": {"type": "string"}, "attempt": {"type": "integer"}, "ok": {"type": "integer"},
        "status": {"type": ["integer", "null"]}, "error": {"type": "string"}, "ms": {"type": "integer"}}},
    "WebhookList": {"type": "object", "properties": {
        "webhooks": {"type": "array", "items": _ref("Webhook")},
        "deliveries": {"type": "array", "items": _ref("WebhookDelivery")},
        "events": {"type": "object", "additionalProperties": {"type": "string"}},
        "signature_header": {"type": "string"}}},
    "WebhookTestResult": {"type": "object", "properties": {
        "ok": {"type": "boolean"}, "status": {"type": ["integer", "null"]}, "error": {"type": ["string", "null"]},
        "event": _ref("Event")}},
    "EventType": {"type": "string", "enum": ["spend.needs_approval", "spend.paid", "spend.denied", "agent.killed",
                                             "stop_all", "loop_guard.paused"]},
    "Event": {"type": "object", "required": ["id", "type", "created", "data"],
              "description": "What Lowtides POSTs to your webhook. Signed with the X-Lowtides-Signature header: "
                             "t=<unix>,v1=<hex HMAC-SHA256 of \"<t>.<raw body>\" with your webhook secret>.", "properties": {
        "id": {"type": "string", "pattern": "^evt_"},
        "type": {"type": "string", "description": "An EventType, or test.ping from the Test button."},
        "created": {"type": "integer"}, "description": {"type": "string"},
        "dashboard": {"type": ["string", "null"]},
        "data": {"type": "object", "additionalProperties": True,
                 "description": "spend.* events carry the spend request (id, agent_name, amount, merchant, item, reasons, "
                                "status...); spend.needs_approval adds approve_url. agent.killed carries the kill result; "
                                "stop_all the totals; loop_guard.paused agent_id, agent_name and repeats."}}},
}


def _spend_paths():
    return {
        "/v1/spend": {"post": {
            "operationId": "requestSpend", "tags": ["Agent: purchases"], "security": AGENT_AUTH,
            "summary": "Ask before buying",
            "description": "Call before every purchase. The agent's rules approve it (and Lowtides pays the merchant through "
                           "PayPal Payouts), deny it, or hold it for a person. Always 200 for a decision: read `decision`.",
            "requestBody": _body(_ref("SpendRequest"), SPEND_EXAMPLE),
            "responses": dict({"200": _json(_ref("SpendDecision"), "The decision.", DECISION_EXAMPLE)}, **_errors(400, 401))}},
        "/v1/spend/{id}": {"get": {
            "operationId": "getSpend", "tags": ["Agent: purchases"], "security": AGENT_AUTH, "parameters": [PATH_ID],
            "summary": "Check a purchase",
            "description": "Poll this while a request is pending. It also refreshes the PayPal payout status.",
            "responses": dict({"200": _json(_ref("SpendDecision"), "Current status.")}, **_errors(401, 404))}},
        "/v1/budget": {"get": {
            "operationId": "getBudget", "tags": ["Agent: purchases"], "security": AGENT_AUTH,
            "summary": "What this agent may still spend today",
            "responses": dict({"200": _json(_ref("Budget"), "Budget and rules.")}, **_errors(401))}},
    }


def _proxy_paths():
    lt_headers = {
        "X-Lowtides-Model-Used": {"schema": {"type": "string"}, "description": "The model that actually answered."},
        "X-Lowtides-Cost": {"schema": {"type": "string"}, "description": "Dollars this call cost."},
        "X-Lowtides-Saved": {"schema": {"type": "string"}, "description": "Dollars saved against the model asked for."},
        "X-Lowtides-Budget-Left-Today": {"schema": {"type": "string"}, "description": "Dollars left in today's AI budget."},
    }
    blocked = {code: _json(_ref("BlockedError"), BLOCKED_TEXT) for code in ("402", "403", "429")}
    stream = {"text/event-stream": {"schema": {"type": "string"}, "description": "Sent when the request sets stream: true."}}

    def ok(schema):
        r = _json(_ref(schema), "The model's answer, plus Lowtides cost headers.")
        r["headers"] = lt_headers
        r["content"].update(stream)
        return r
    return {
        "/v1/models": {"get": {
            "operationId": "listModels", "tags": ["Agent: AI proxy"], "summary": "Models this Lowtides knows",
            "responses": {"200": _json(_ref("ModelList"))}}},
        "/v1/chat/completions": {"post": {
            "operationId": "createChatCompletion", "tags": ["Agent: AI proxy"], "security": AGENT_AUTH,
            "summary": "OpenAI-compatible chat (budgeted)",
            "description": "Drop-in for OpenAI: point base_url at Lowtides and use the agent's Lowtides key. Budgets, "
                           "the loop guard and the leash run before the call reaches a model.",
            "requestBody": _body(_ref("ChatCompletionRequest"),
                                 {"model": "sim-large", "messages": [{"role": "user", "content": "Translate: hello"}]}),
            "responses": dict({"200": ok("ChatCompletion")}, **blocked, **_errors(400, 401))}},
        "/v1/messages": {"post": {
            "operationId": "createMessage", "tags": ["Agent: AI proxy"], "security": AGENT_AUTH,
            "summary": "Anthropic-compatible messages (budgeted)",
            "description": "Drop-in for the Anthropic API: point base_url at Lowtides and use the agent's Lowtides key.",
            "requestBody": _body(_ref("MessageRequest"), {"model": "sim-large", "max_tokens": 300,
                                                          "messages": [{"role": "user", "content": "Translate: hello"}]}),
            "responses": dict({"200": ok("Message")}, **blocked, **_errors(400, 401))}},
        "/health": {"get": {
            "operationId": "health", "tags": ["Agent: AI proxy"], "summary": "Health check",
            "responses": {"200": _json({"type": "object", "properties": {"ok": {"type": "boolean"}, "version": {"type": "string"}}})}}},
    }


def _dash(op, summary, responses, tags="Dashboard: approvals", body=None, params=None, description=None, codes=(401, 403)):
    o = {"operationId": op, "tags": [tags], "security": ADMIN_AUTH, "summary": summary,
         "responses": dict(responses, **_errors(*codes))}
    if body is not None:
        o["requestBody"] = body
        o["responses"].update(_errors(415))
    if params:
        o["parameters"] = params
    if description:
        o["description"] = description
    return o


def _dashboard_paths():
    status_q = {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["pending", "all", "paid", "denied",
                                                                                       "approved", "cancelled", "failed"],
                                                            "default": "all"}}
    limit_q = {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100, "maximum": 1000}}
    rules_body = _body({"type": "object", "required": ["rules"], "properties": {"rules": _ref("Rules")}},
                       {"rules": {"max_per_purchase": 60, "daily_purchase_limit": 100, "approval_over": 25}})
    rules_out = {"type": "object", "properties": {"rules": _ref("Rules"), "plain": {"type": "array", "items": {"type": "string"}}}}
    return {
        "/api/state": {"get": _dash("getState", "Dashboard state (agents with their keys, events, totals)",
                                    {"200": _json(_ref("State"))}, tags="Dashboard: agents")},
        "/api/agents": {"post": _dash("createAgent", "Create an agent (returns its Lowtides key)",
                                      {"200": _json({"type": "object", "properties": {"agent": _ref("Agent")}})},
                                      tags="Dashboard: agents", codes=(400, 401, 403),
                                      body=_body(_ref("AgentCreate"), {"name": "Research bot", "daily_budget": 5}))},
        "/api/spend": {"get": _dash("listSpend", "List purchase requests", {"200": _json(_ref("SpendList"))},
                                    params=[status_q, limit_q])},
        "/api/spend/{id}/approve": {"post": _dash(
            "approveSpend", "Approve a held purchase (pays it)", {"200": _json(_ref("SpendDecision"))},
            body=_body({"type": "object"}, {}, required=False), params=[PATH_ID], codes=(401, 403, 404, 409))},
        "/api/spend/{id}/deny": {"post": _dash(
            "denySpend", "Deny a held purchase", {"200": _json(_ref("SpendDecision"))},
            body=_body({"type": "object", "properties": {"note": {"type": "string", "maxLength": 300}}}, {"note": "Not needed"},
                       required=False), params=[PATH_ID], codes=(401, 403, 404, 409))},
        "/api/spend/{id}/evidence": {"get": _dash(
            "getEvidence", "Download the dispute evidence pack", {"200": _json({"type": "object"}, "Evidence pack (JSON).")},
            params=[PATH_ID], codes=(401, 403, 404))},
        "/api/agents/{agent_id}/kill": {"post": _dash(
            "killAgent", "KILL one agent", {"200": _json(_ref("KillResult"))}, tags="Dashboard: kill switch",
            body=_body({"type": "object"}, {}, required=False), params=[AGENT_ID], codes=(401, 403, 404),
            description="Stops the agent, denies its pending purchases, voids its open PayPal holds and cancels its unclaimed payouts.")},
        "/api/stop-all": {"post": _dash(
            "stopAll", "STOP ALL", {"200": _json(_ref("StopAllResult"))}, tags="Dashboard: kill switch",
            body=_body({"type": "object"}, {}, required=False), description="The kill, for every agent.")},
        "/api/agents/{agent_id}/rules": {
            "get": _dash("getRules", "Read an agent's rules", {"200": _json(rules_out)}, tags="Dashboard: rules",
                         params=[AGENT_ID], codes=(401, 403, 404)),
            "post": _dash("saveRules", "Save an agent's rules", {"200": _json(rules_out)}, tags="Dashboard: rules",
                          body=rules_body, params=[AGENT_ID], codes=(400, 401, 403, 404))},
        "/api/rules/compile": {"post": _dash(
            "compileRules", "Turn plain English into rules (does not save)", {"200": _json(_ref("RulesCompileResult"))},
            tags="Dashboard: rules", codes=(400, 401, 403),
            body=_body({"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
                       {"text": "Max $100 a day. No more than $60 per purchase. Ask me over $25. Never buy gift cards or crypto."}),
            description="Claude translates the text when ANTHROPIC_API_KEY is set; otherwise an offline parser does. "
                        "A person reviews the result before saving it. The AI never decides a payment.")},
        "/api/merchants": {
            "get": _dash("listMerchants", "List merchants (payee allowlist)",
                         {"200": _json({"type": "object", "properties": {"merchants": {"type": "array", "items": _ref("Merchant")}}})},
                         tags="Dashboard: rules"),
            "post": _dash("saveMerchant", "Add or update a merchant",
                          {"200": _json({"type": "object", "properties": {"merchant": _ref("Merchant")}})}, tags="Dashboard: rules",
                          body=_body({"type": "object", "required": ["name"], "properties": {
                              "name": {"type": "string"}, "paypal_email": {"type": "string", "format": "email"},
                              "category": {"type": "string"}}},
                              {"name": "Acme Data", "paypal_email": "merchant-demo@example.com", "category": "data"}),
                          codes=(400, 401, 403))},
        "/api/forecast": {"get": _dash("getForecast", "When each agent will hit its limit", {"200": _json(_ref("Forecast"))},
                                       tags="Dashboard: rules")},
        "/api/audit/verify": {"get": _dash("verifyAudit", "Check the tamper-evident audit chain",
                                           {"200": _json(_ref("AuditVerify"))}, tags="Dashboard: rules")},
        "/api/webhooks": {
            "get": _dash("listWebhooks", "List outbound webhooks and recent deliveries", {"200": _json(_ref("WebhookList"))},
                         tags="Dashboard: webhooks"),
            "post": _dash("addWebhook", "Add a webhook (Zapier, Make or any URL)", {"200": _json(_ref("WebhookCreated"))},
                          tags="Dashboard: webhooks", codes=(400, 401, 403),
                          body=_body(_ref("WebhookCreate"), {"url": "https://hooks.zapier.com/hooks/catch/123456/abcdef/",
                                                              "label": "Text me approvals", "events": ["spend.needs_approval"]}))},
        "/api/webhooks/{webhook_id}": {"delete": _dash(
            "deleteWebhook", "Delete a webhook", {"200": _json({"type": "object", "properties": {"ok": {"type": "boolean"}}})},
            tags="Dashboard: webhooks", params=[HOOK_ID], codes=(401, 403, 404))},
        "/api/webhooks/{webhook_id}/test": {"post": _dash(
            "testWebhook", "Send a signed test.ping now", {"200": _json(_ref("WebhookTestResult"))}, tags="Dashboard: webhooks",
            body=_body({"type": "object"}, {}, required=False), params=[HOOK_ID], codes=(401, 403, 404))},
    }


def _webhooks():
    from .notify import EVENTS
    out = {}
    for name, text in EVENTS.items():
        out[name] = {"post": {
            "operationId": "on_" + re.sub(r"\W", "_", name), "summary": text,
            "parameters": [{"name": "X-Lowtides-Signature", "in": "header", "required": True, "schema": {"type": "string"},
                            "description": "t=<unix>,v1=<hex HMAC-SHA256 of \"<t>.<raw body>\">"},
                           {"name": "X-Lowtides-Event", "in": "header", "required": True, "schema": {"type": "string"}}],
            "requestBody": _body(_ref("Event")),
            "responses": {"200": {"description": "Any 2xx means received. Anything else is retried with backoff."}}}}
    return out


def build():
    paths = {}
    for part in (_spend_paths(), _proxy_paths(), _dashboard_paths()):
        paths.update(part)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Lowtides API", "version": __version__,
            "summary": "One budget and a STOP button between AI agents and money.",
            "description": "Agents ask Lowtides before they buy (`POST /v1/spend`) and send model calls through it "
                           "(OpenAI- and Anthropic-compatible). Approved purchases are paid with PayPal Payouts. "
                           "Plain-English rules, approvals and a kill switch are on the dashboard API. "
                           "The public demo at https://lowtides.ai needs no password: get an agent key from `GET /api/state`.",
            "license": {"name": "AGPL-3.0-only", "identifier": "AGPL-3.0-only"},
            "contact": {"name": "Lowtides", "url": "https://github.com/seniorhelper/lowtides"}},
        "servers": [{"url": "https://lowtides.ai", "description": "Public demo (PayPal sandbox, simulated models)"},
                    {"url": "http://127.0.0.1:8787", "description": "Your own copy"}],
        "tags": [{"name": "Agent: purchases", "description": "Ask before buying. Auth: the agent's Lowtides key."},
                 {"name": "Agent: AI proxy", "description": "Drop-in OpenAI and Anthropic endpoints with budgets."},
                 {"name": "Dashboard: agents", "description": "Create agents and read their keys."},
                 {"name": "Dashboard: approvals", "description": "A person approves or denies held purchases."},
                 {"name": "Dashboard: kill switch", "description": "KILL one agent or STOP ALL."},
                 {"name": "Dashboard: rules", "description": "Rules, merchants, forecast and audit."},
                 {"name": "Dashboard: webhooks", "description": "Send events to Zapier, Make or any URL."}],
        "paths": paths,
        "webhooks": _webhooks(),
        "components": {
            "schemas": SCHEMAS,
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "lt-...",
                               "description": "The agent's Lowtides key (from the dashboard)."},
                "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "x-api-key",
                                 "description": "The same agent key, Anthropic-style."},
                "adminToken": {"type": "apiKey", "in": "header", "name": "X-Admin-Token",
                               "description": "Dashboard password (LOWTIDES_ADMIN_TOKEN). Not needed in the public demo."},
                "adminCookie": {"type": "apiKey", "in": "cookie", "name": "lt_admin",
                                "description": "Set by POST /api/login."}}},
    }


SPEC = build()


# ---- YAML (a JSON-compatible subset; stdlib has no YAML writer) -------------------------------
_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9 _./{}()$#,'+-]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}


def _scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED and not s.endswith(" ") and ": " not in s and " #" not in s:
        return s
    return json.dumps(s, ensure_ascii=False)


def _key(k):
    return _scalar(str(k))


def to_yaml(v, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(v, dict):
        if not v:
            return pad + "{}"
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                lines.append("%s%s:" % (pad, _key(k)))
                lines.append(to_yaml(x, indent + 1))
            else:
                lines.append("%s%s: %s" % (pad, _key(k), _inline(x)))
        return "\n".join(lines)
    if isinstance(v, list):
        if not v:
            return pad + "[]"
        for x in v:
            if isinstance(x, (dict, list)) and x:
                inner = to_yaml(x, indent + 1).split("\n")
                lines.append("%s- %s" % (pad, inner[0].lstrip()))
                lines.extend(inner[1:])
            else:
                lines.append("%s- %s" % (pad, _inline(x)))
        return "\n".join(lines)
    return pad + _scalar(v)


def _inline(x):
    if isinstance(x, dict):
        return "{}"
    if isinstance(x, list):
        return "[]"
    return _scalar(x)


def yaml_text():
    return ("# Generated from lowtides/openapi.py. Do not edit by hand:\n"
            "#   python3 -m lowtides.openapi --write\n") + to_yaml(SPEC) + "\n"


YAML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "openapi.yaml")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--write" in argv:
        with open(YAML_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(yaml_text())
        print("Wrote " + YAML_PATH)
    elif "--json" in argv:
        print(json.dumps(SPEC, indent=2))
    else:
        sys.stdout.write(yaml_text())


if __name__ == "__main__":
    main()
