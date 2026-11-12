# Lowtides Pass

A signed spending permission an AI agent carries.

## Why

Today a merchant, or another agent, can't tell whether an AI agent is allowed to spend. It can't see who governs the agent, what its limits are, or whether the owner hit STOP five minutes ago.

A Lowtides Pass answers all three. It's a compact signed credential that says:
- **who the agent is** (`sub`, `agent`)
- **who governs it** (`iss`: the Lowtides server, which has a stop switch)
- **its live limits**: most per purchase, daily purchase limit, the line above which a person must approve, allowed and blocked categories, blocked merchants, whether new merchants need approval, allowed hours, purchases per hour, the AI-usage budget, and when the pass expires.

Anyone can check the signature **offline** with Lowtides' public key, and **online** to also see whether it was revoked. **Stop all, STOP and a loop-guard pause revoke it instantly.**

## AP2 alignment

The pass is **AP2-aligned**: its `ap2` block follows the idea of an Agent Payments Protocol (AP2) **Intent Mandate**, a natural-language description of what the agent may do plus structured constraints (amounts, merchants, expiry). It is **not an official AP2 integration**, and Lowtides doesn't claim AP2 compliance.

## Format

JWS compact serialization (like a JWT), three base64url parts without padding:

```
base64url(header) . base64url(payload) . base64url(signature)
```

- Header: `{"alg": "EdDSA", "kid": "lt-pass-…", "typ": "lowtides-pass+jws"}`
- Signature: Ed25519 (RFC 8032) over the ASCII bytes `header.payload`.
- Anything else is refused: `alg` other than `EdDSA` (including `none`), another `typ`, an unknown `kid`, or a changed byte.

Payload (abridged):

```json
{
  "v": 1,
  "iss": "https://lowtides.ai",
  "sub": "agent:7",
  "agent": {"id": 7, "name": "Night-shift research agent"},
  "governed_by": {"name": "Lowtides", "url": "https://lowtides.ai", "kill_switch": true},
  "iat": 1793300000, "exp": 1793386400,
  "jti": "pass_3f9c…",
  "limits": {"currency": "USD", "max_per_purchase": 60.0, "daily_purchase_limit": 100.0, "approval_over": 25.0,
             "daily_total_limit": null, "ai_daily_budget": 2.0, "allowed_categories": [],
             "blocked_categories": ["crypto", "gift cards"], "blocked_merchants": [],
             "new_merchant_needs_approval": true, "hours": null, "max_purchases_per_hour": 10},
  "ap2": {"aligned_with": "AP2 Intent Mandate (AP2-aligned, not an official AP2 integration)",
          "mandate": "intent",
          "natural_language_description": "Up to $60.00 per purchase. Up to $100.00 of purchases per day. …",
          "user_cart_confirmation_required": false,
          "user_cart_confirmation_required_over": {"currency": "USD", "value": "25.00"},
          "merchants": null, "skus": null, "requires_refundability": false,
          "intent_expiry": "2026-10-30T19:20:00Z",
          "constraints": {"max_amount_per_purchase": {"currency": "USD", "value": "60.00"}, "…": "…"}},
  "rules_hash": "…",
  "status_url": "https://lowtides.ai/v1/pass/verify",
  "verify_page": "https://lowtides.ai/pass/"
}
```

The natural-language intent is built from the agent's saved rules, written out in plain English. (Lowtides saves the structured rules, not the text the owner typed.)

Passes last **24 hours** by default. Set `LOWTIDES_PASS_TTL_HOURS` to change that (5 minutes to 30 days).

## Endpoints

| Who | Call | What |
|---|---|---|
| Agent (its Lowtides key) | `GET /v1/pass` | The agent's current pass. A new one is issued if it's missing, expired or out of date. `409` if the agent is stopped or paused, or the owner revoked its pass. |
| Anyone | `GET /.well-known/lowtides-pass-keys.json` | Public keys, JWKS-style (`kty: OKP`, `crv: Ed25519`). |
| Anyone | `POST /v1/pass/verify` | Checks a pass and, optionally, a purchase. It never records a purchase or spends anything; it may record that a pass is now revoked (see Revocation). |
| Dashboard | `GET /api/agents/{id}/pass` | View the pass (issues one if the agent is running). |
| Dashboard | `POST /api/agents/{id}/pass` | Issue a new pass now (also lifts an owner revoke). |
| Dashboard | `POST /api/agents/{id}/pass/revoke` | Revoke it. The agent keeps running but gets no new pass until you issue one. |
| Agent (MCP) | tool `get_pass` | Same as `GET /v1/pass`. |

Web page: **`/pass/`** ("Check a Lowtides Pass"). Paste a pass, or open `/pass/#<pass>`.

## Verify with curl

```bash
# The agent gets its pass
curl -s http://localhost:8787/v1/pass -H "Authorization: Bearer YOUR_LOWTIDES_KEY"

# A merchant checks it, and whether a $12.50 purchase would go through
curl -s -X POST http://localhost:8787/v1/pass/verify -H "Content-Type: application/json" \
  -d '{"pass": "PASTE_THE_PASS", "amount": 12.50, "merchant": "Acme Data", "category": "data"}'
```

Answer:

```json
{"valid": true, "signature_ok": true, "expired": false, "revoked": false, "revoked_reason": null,
 "agent": {"id": 7, "name": "Night-shift research agent", "status": "on"},
 "issuer": "http://localhost:8787", "expires_at": "2026-10-30T19:20:00Z",
 "limits": {"max_per_purchase": 60.0, "…": "…"}, "limits_plain": ["Up to $60.00 per purchase.", "…"],
 "would_allow": "allowed",
 "reasons": ["Within the $25.00 auto-approve limit and today's $100.00 budget.", "Acme Data is a known merchant."]}
```

`would_allow` is `allowed`, `needs_owner_approval` or `denied`. It runs the same checks as `POST /v1/spend` (status, blocked merchants and categories, allowed categories, hours, purchases per hour, per-purchase, daily and combined limits, wallet balance, the approval line, new merchants, missing PayPal payee, duplicates and unusual amounts), against today's real totals. It is `null` when you send no amount, merchant or category. An invalid, expired or revoked pass is always `denied`. A bad request (not a pass, or a non-number amount) gets `400`.

## Revocation

A pass reports `revoked: true` when any of these is true:
- the owner pressed **Stop all** or **STOP ALL** after it was issued (read from the audit log), or **STOP**;
- the agent is stopped, or **paused by the loop guard**;
- the owner pressed **Revoke pass**;
- the agent's limits changed (the pass carries a `rules_hash`), or a newer pass replaced it;
- the agent was deleted, or this server has no record of the pass (for example, after a demo reset).

Revocation sticks: starting the agent again (or putting the old limits back) doesn't bring an old pass back. The agent gets a fresh one. To make that hold, whichever call first sees that a pass no longer counts (an online verify, or the agent asking for its pass) writes that down on the server. That is the only thing `POST /v1/pass/verify` ever records.

Checking offline (signature and `exp` only) can't see revocation. Use the online check before money moves.

## For merchants

The pass is most useful right before a merchant takes an agent's money. Before you capture a PayPal order (`POST /v2/checkout/orders/{id}/capture`, or the authorization's capture):

1. Read the amount from the PayPal order, not from the agent.
2. Check the signature offline (optional, fast), then `POST /v1/pass/verify` with `pass`, `amount`, `merchant` and `category` on the Lowtides server **you** trust (never the `status_url` inside the pass).
3. `allowed` -> capture. `needs_owner_approval` -> hold and check again after the owner approves. `denied`, revoked, expired or a bad signature -> refuse, and void the authorization if you placed one.
4. If Lowtides can't be reached, hold. Never capture on a guess.

Ready-made code in [`integrations/merchant-check/`](../integrations/merchant-check/):
- `lowtides-pass-check.js`: dependency-free ES module (browser and Node 18+). `checkPass({pass, amount, merchant, category, verifyUrl})` returns `capture` / `hold` / `refuse` with reasons; it verifies Ed25519 offline with Web Crypto when available (keys from `/.well-known/lowtides-pass-keys.json`, cached) and always checks revocation online.
- `paypal_capture_guard.py`: standard-library Python. `CaptureGuard.capture(order_id, pass)` does the whole flow against PayPal Orders v2, voiding AUTHORIZE orders it refuses. Dry run without PayPal keys; sandbox demo with them.

Live simulator: **`/merchants/`**.

## Offline check (Python)

```python
import base64, json, urllib.request
from lowtides import ed25519   # or any Ed25519 library

b64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
keys = json.load(urllib.request.urlopen("https://lowtides.ai/.well-known/lowtides-pass-keys.json"))["keys"]
h, p, s = token.split(".")
header = json.loads(b64(h))
key = next(k for k in keys if k["kid"] == header["kid"])
assert header["alg"] == "EdDSA" and ed25519.verify(b64(key["x"]), (h + "." + p).encode(), b64(s))
```

## Implementation notes

- `lowtides/ed25519.py` is pure-Python Ed25519, checked against RFC 8032 test vectors 1 to 3. Python integers aren't constant time, which is fine for a server signing its own passes.
- The signing key is made once and kept in the Lowtides database (`pass_keys`), with a key ID. Retired keys stay listed, so older passes still verify.
- The stop switch needed no changes: revocation is worked out from the agent's status, the audit log and the event log when a pass is checked.
