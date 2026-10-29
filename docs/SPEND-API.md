# Spend requests API (agentic commerce)

Agents ask Lowtides before they buy anything. Lowtides checks the request against that agent's rules and returns one of three decisions:

- **approved**: Lowtides pays the merchant through PayPal Payouts. Without PayPal keys, the payout is simulated and labeled as simulated.
- **needs_approval**: a person approves or denies it on the dashboard.
- **denied**

Every decision comes with plain-English reasons and is written to a tamper-evident audit log.

The LLM never makes payment decisions. Claude only turns plain-English rules into the JSON rules below, which a person reviews before saving. Every check is deterministic code.

## Agent-facing (auth: `Authorization: Bearer lt-...` or `x-api-key`)

### `POST /v1/spend`
Request:
```json
{"amount": 12.50, "merchant": "Acme Data", "item": "1,000 enrichment credits",
 "category": "data", "reason": "Needed to finish the lead list", "task_id": "optional"}
```
Response (200 for every decision; 401 for a bad key):
```json
{"id": "sp_...", "decision": "approved|needs_approval|denied", "status": "paid|pending|denied|approved|cancelled",
 "amount": 12.5, "merchant": "Acme Data", "reasons": ["Within the $20.00 auto-approve limit.", "..."],
 "flags": ["new_merchant", "possible_injection", "duplicate", "unusual_amount"],
 "payout": {"id": "...", "status": "SUCCESS|PENDING|UNCLAIMED|SIMULATED", "simulated": true},
 "remaining_today": 37.5}
```

### `GET /v1/spend/{id}`
Returns the same object with the current status. Agents poll this while a request is waiting for approval.

### `GET /v1/budget`
```json
{"agent": "Research bot", "status": "on|paused|stopped",
 "tokens": {"daily_budget": 5.0, "spent_today": 0.12},
 "purchases": {"daily_limit": 200, "spent_today": 12.5, "remaining_today": 187.5, "auto_approve_up_to": 20, "max_per_purchase": 50},
 "rules": { ... }}
```

## Rules (per agent; JSON)
```json
{"max_per_purchase": 50, "daily_purchase_limit": 200, "approval_over": 20,
 "allowed_categories": [], "blocked_categories": ["gift cards", "crypto"],
 "blocked_merchants": [], "new_merchant_needs_approval": true,
 "hours": null, "max_purchases_per_hour": 10}
```
- An empty `allowed_categories` list means any category not in `blocked_categories` is allowed.
- `hours` is either null or `{"start": 9, "end": 18}`, in local server time.

## Merchants (allowlist with PayPal payee)
A merchant has `name`, `paypal_email` (the payee), `category` and `trusted`. A known merchant is one in this list. A purchase from an unknown merchant needs approval when `new_merchant_needs_approval` is on. This stops a prompt-injected agent from paying an attacker's account.

## Dashboard API (same admin auth and same-site rules as the other /api routes)
- `GET  /api/spend?status=pending|all&limit=100` → `{"requests": [...]}`, each with `agent_name`, `ts`, `decision`, `status`, `reasons`, `flags`
- `POST /api/spend/{id}/approve` → pays it and returns the request
- `POST /api/spend/{id}/deny` with `{"note": "..."}` → returns the request
- `GET  /api/agents/{id}/rules` → `{"rules": {...}}`
- `POST /api/agents/{id}/rules` with `{"rules": {...}}` → validated and saved; returns `{"rules": {...}}`
- `POST /api/rules/compile` with `{"text": "research agents max $50/day, ask me over $20, never buy gift cards"}` → `{"rules": {...}, "source": "claude|offline", "notes": ["..."]}` (does not save)
- `GET  /api/merchants`, `POST /api/merchants` with `{"name", "paypal_email", "category"}`
- `GET  /api/forecast` → `{"agents": [{"id", "name", "spent_today", "limit", "projected_today", "minutes_to_limit"}]}`, covering both token spend and purchases
- `GET  /api/audit/verify` → `{"ok": true, "count": 42, "broken_at": null}`
- `POST /api/agents/{id}/kill` → stops the agent, denies its pending requests, voids its open PayPal holds and cancels its unclaimed payouts. Returns `{"stopped": true, "denied": n, "holds_voided": n, "payouts_cancelled": n}`
- `POST /api/stop-all` (existing) now does the kill for every agent.
