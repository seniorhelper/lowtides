# Action Muzzle

**Your AI does only what you asked. Both sides say yes, or it does not happen.**

A purchase goes through the Handshake. Action Muzzle is the same two-sided consent check for everything else an AI might do for a person: reserve a table, schedule an appointment, book travel, place a call, sign up, cancel, send a message, send a form, share personal details. When one of those carries a deposit, fare or fee, the money still moves only through PayPal, and only after both sides said yes.

Try it at `/shop/#muzzle`. Code: `lowtides/muzzle.py`. Tests: `tests/test_muzzle.py`.

## How one action is decided

1. **The job.** The person says the job in plain words ("book a table for two Friday at 7 at Harbor Bistro"). It becomes a *task scope*: the kinds of action and the business the request implies. The rule is **do only what was asked**: anything outside the scope has to ask, even if it would otherwise be allowed.
2. **The person's levels.** Each kind of action is set to **Allowed**, **Ask me first** or **Never**. Actions that cannot be undone (cancel, message, share details) default to Ask me first.
3. **The mode.** Off: nothing happens. AI Assist: every action asks. Full Autopilot: a match goes ahead by itself.
4. **Money limits.** If the action carries an amount (`params.amount`), the Pass's per-item limit and ask-me line apply exactly as they do to a purchase.
5. **The business's policy.** Each business publishes a signed *action policy*: which kinds of request it accepts from AI assistants, which need one of its people, the minimum trust it asks for, and that the AI must say it is an AI.
6. **The watchdog.** A business whose name is a near copy of another, or that has published no signed policy, is refused.
7. **Private details.** A Social Security number, password, card number or bank account number is never shared, whatever the level says.

Outcome, as with purchases: **match** (done), **needs_yes** (held for the person's one-tap yes or no), **no_match** (nothing happens).

## PayPal

When a matched action has an amount, Lowtides pays it through the same path a Handshake purchase uses: a PayPal Payouts call in the sandbox (or a payment labeled simulated when the server has no sandbox keys). A held action pays nothing until the person says yes. A refused action never reaches PayPal. The receipt carries the PayPal payout id.

## Stop word and freeze

The person picks a stop word (default "red light"). Typing or saying it to Ask Lowtides, or pressing **Freeze everything now**, puts every AI shopper's Pass on hold: every store and every business then answers no. Nothing is canceled. **Release the freeze** lets the shoppers work again, and jobs that were waiting for a yes are still waiting. This is different from Stop all, which also cancels what has not gone through.

## Receipts

Every action, whatever its outcome, gets a signed receipt (`GET /api/muzzle/receipt/{id}`) holding the action, the task scope it was checked against, the business's policy digest, every check and the result. It verifies against the server's published key, and it appears in the person's receipts and receipt search.

## API

| Call | What it does |
|---|---|
| `GET /api/muzzle` | The person's state: levels, the job, businesses and their policies, recent actions |
| `POST /api/muzzle/task` `{"text"}` | Set the job |
| `POST /api/muzzle/rules` `{"levels": {...}, "stop_word"}` | Set levels or the stop word |
| `POST /api/muzzle/action` `{"action_type", "counterparty", "summary", "params"}` | Try an action |
| `POST /api/muzzle/decide` `{"id", "approve"}` | The person's yes or no on a waiting action |
| `POST /api/muzzle/freeze`, `POST /api/muzzle/release` | Freeze and release |
| `POST /v1/action`, `GET /v1/action/{id}` | The same check for an outside AI agent, with its own agent key |

Outside agents can also use the `request_action` tool on the MCP server (`lowtides/mcp.py`).

## Honest limits

- The businesses are fictional and **nothing real is contacted**. A "done" action records what would have been done and returns a demo confirmation number. Every result says so.
- Payments are PayPal sandbox test money, or labeled simulated without keys.
- Turning a job into a scope is rule-based. No AI model reads the job.
- Live phone calls with checks at each point where the other side asks for a commitment are part of the design but are not built.
- The stop word works when typed, or spoken through the built-in Talk. It is not listened for during a natural-voice (Vapi) call.

Patent pending.
