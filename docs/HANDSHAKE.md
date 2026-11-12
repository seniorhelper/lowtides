# The Handshake (Consent Shopping)

**Both sides say yes, or no money moves.**

An AI shopper carries a signed **Pass** (its limits, from `docs/LOWTIDES-PASS.md`). A store publishes a signed **Store Policy** (its rules for AI shoppers). **GuardPooch** checks the store is real. A **Handshake** puts the three side by side and checks both directions. PayPal (sandbox) settles. One press of Stop all revokes the Pass at every store at once.

AP2-aligned in spirit (a signed, limited intent on one side and a checked counterparty on the other). It is not an official AP2, Visa or Mastercard integration.

## The three signals

| Signal | Meaning | What happens |
|---|---|---|
| `match` (green) | Both said yes. | With `execute: true` it pays through the normal spend path. |
| `needs_yes` (yellow) | Nothing is forbidden, but a person has to say yes (over the shopper's ask-me line, over the store's no-human limit, or GuardPooch says "caution" and the shopper is on "balanced"). | A normal held approval is created. A person approves or denies it as usual. |
| `no_match` (red) | A rule on one side says no. | Nothing moves. |

## What is checked

Shopper side (from the Pass and the shopper's settings): Pass state (`VALID`, `HELD`, `REVOKED`), Pass not expired, per-item limit, daily limit remaining, allowed categories, never-buy list, GuardPooch verdict (set against `guard_strictness`), and the ask-me line.

Store side (from the Store Policy): the Pass signature and issuer are accepted, the agent is not blocked, the agent's trust score meets `min_trust`, the category is sold to AI shoppers, the amount is under `max_order_no_human`, and evidence is attached if `require_evidence` is set.

Any `fail` makes the signal `no_match`. Otherwise any `ask` makes it `needs_yes`. Otherwise `match`. The function `lowtides.handshake.evaluate()` is pure (no clock, database or network), so the same inputs always give the same answer.

`guard_strictness`: `strict` turns a GuardPooch `caution` into red, `balanced` into yellow, `relaxed` allows it. A GuardPooch `danger` is always red.

A Handshake can only make a payment **harder** than the agent's own rules. A `match` still goes through every spend rule; if they disagree the signal is downgraded and the receipt says so.

## Modes (both sides)

- Shopper mode (`mode` in `PUT /api/shopper/prefs`): `autopilot` (default: greens pay by themselves, yellow asks), `assist` (adds a `shopper_mode` "ask" check, so every purchase waits for a person), `off` (adds a `shopper_mode` "fail" check, so nothing can be bought; reversible, unlike Stop all, and an attempt while Off costs no trust).
- Store mode (`mode` in the signed Store Policy, `PUT /api/stores/{id}/policy`): `auto` (default), `review` (a `store_mode` "ask" check: every AI order waits for a person), `closed` (a `store_mode` "fail" check: every AI order is refused).
- Default modes add no check, so older receipts and tests are unchanged.

## Extra store KPIs

`GET /api/merchant/kpis` also returns `gross_usd`, `refunded_usd`, `net_captured_usd`, `kept_order_rate` (kept vs. pulled back: a rough satisfaction proxy, not a survey), `returning_shoppers` (agents with 2+ kept orders), `shoppers_with_orders`, `retention_rate`, and `bad_orders_stopped` / `bad_orders_stopped_usd` (real order attempts, `execute: true`, that were refused; look-only checks are not counted). All come from the stored Handshake and spend rows.

## How a store verifies

1. Publish your policy at `/.well-known/lowtides-store-policy.json?store=<id>`. It is signed with Ed25519 (same key style as the Pass).
2. Fetch the issuer's public keys from `/.well-known/lowtides-pass-keys.json`, then check the `signature` over the canonical JSON of the document (sorted keys, no spaces, without the `signature` field itself).
3. For each order, ask for the Handshake receipt (`receipt` in the response). It is signed the same way and names the exact Pass (`pass.jti`) and the exact Store Policy (`policy_sha256`) it was checked against.
4. `GET /api/handshakes/{id}/proof` returns a proof pack: the Pass, the Store Policy, the receipt, the approval (if any), the PayPal ids and the audit-log entry. `lowtides.handshake.verify_proof(pack, keys)` checks every signature and recomputes the audit entry's hash using public keys only. Pass in keys you fetched yourself rather than the copy inside the pack.

Receipts are written to the same hash-chained audit log as everything else, so editing one later breaks the chain.

## Honest limits

- The five stores are **fictional demo stores** on `.example` domains. No real store is involved.
- Money moves only in the **PayPal sandbox**. With sandbox keys set, a match creates a real sandbox payout (the `paypal_order_id` in the response is the payout batch id). Without keys it is a clearly labeled simulation (`simulated: true`).
- GuardPooch in the demo uses fixed seed data (domain age, lookalike distance, price comparison) so the verdicts repeat. A live domain-age lookup (RDAP) exists but is off by default, never runs for `.example` domains and never runs in the public demo.
- A signature proves who said what, not that the other party is honest. The trust score is a heuristic from one server's own history, not a network reputation (see `docs/TRUST-SCORE.md`).
- The proof pack's audit entry can be re-hashed by anyone, but only the server can prove the whole chain is unbroken (`audit.chain_ok` is the server's own check).
- Nothing here files PayPal disputes or talks to any other payment network.

## Routes

`GET /api/stores`, `PUT /api/stores/{id}/policy`, `GET /.well-known/lowtides-store-policy.json?store=`, `GET /api/catalog` (Match Finder), `POST /api/handshake`, `GET /api/handshakes`, `GET /api/handshakes/{id}/proof`, `GET /api/agents/{id}/trust`, `GET /api/merchant/kpis`, `GET|PUT /api/shopper/prefs`. All are in `docs/openapi.yaml`.
