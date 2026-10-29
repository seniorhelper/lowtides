# Devpost submission text (draft)

**Tagline (under 200 characters):** One budget and a STOP button for AI agents: Lowtides caps what they cost to run and what they buy, and settles every approved purchase through PayPal.

**Try it:** https://lowtides.ai (no login, PayPal sandbox, simulated AI models, "Reset demo" button)
**Code:** https://github.com/seniorhelper/lowtides (AGPL-3.0)
**Overview and FAQ:** https://lowtides.ai/why/

## Inspiration
I run more than seven AI agents across my businesses and lease agents to clients. Two things kept me up at night: AI bills I couldn't predict, and agents that can now pay for things. A shopping agent famously spent $31.43 on eggs it was only asked to find. Surveys say 31% of US consumers would allow no autonomous AI spending at all, and 6 in 10 UK adults would quit an AI shopping agent after one mistake. People won't hand agents a wallet until there's a budget and a STOP button they trust.

## What it does
Lowtides puts **one budget, plain-English rules, one-tap approvals and a kill switch** between AI agents and your money, covering **both** ways an agent spends:

- **What it costs to think (model calls):** Lowtides sits in the request path as a drop-in for the OpenAI and Anthropic APIs. It routes easy steps to cheaper models, answers exact repeats for free, trims wasted context, caps each call, limits prompt size, and the **loop guard pauses a runaway agent** before it burns more money.
- **What it buys (purchases):** agents ask Lowtides before every purchase (`POST /v1/spend` or the MCP tool `request_spend`). Your rules approve it, deny it or send it to you. Approved purchases are paid through **PayPal Payouts**.
- **One combined daily cap** covers AI usage plus purchases, per agent.
- **Rules in plain English:** "Max $100 a day. No more than $60 per purchase. Ask me over $25. Never buy gift cards or crypto." Claude turns them into strict, validated rules a person reviews. **The AI never decides a payment; code does.**
- **Held for a person:** purchases over your line, new payees, text that reads like prompt injection, duplicate charges and unusual amounts.
- **STOP ALL / KILL:** stops agents, denies pending buys, **voids open PayPal holds, cancels unclaimed PayPal payouts and revokes the agent's saved PayPal payment method** (Vault v3), so Lowtides can't charge it again until you reconnect it.
- **Burst-proof limits:** simultaneous requests are decided one at a time, so 20 purchases fired at once can't overspend a limit.
- **Proof:** a hash-chained, tamper-evident audit log and a one-click dispute evidence pack per purchase.
- **Alerts anywhere:** signed outbound webhooks (Zapier, Slack, your phone) for approvals and stops; an OpenAPI 3.1 spec and a Postman collection for developers.
- **Home and Business modes:** a family can give a teen's or parent's agent a $25 "ask me" line; agencies get per-client budgets and PayPal invoices.

## How we built it
Python standard library only (no dependencies), SQLite for the ledger, a vanilla JS dashboard with an **AG Grid** ledger, hosted on **Render** with a health check. Claude (Anthropic) was the coding partner, and Claude Haiku compiles plain-English rules. 329 automated tests, including end-to-end runs against a fake PayPal server.

### PayPal, used for real money movement (sandbox)
| PayPal API | What Lowtides does with it |
|---|---|
| Orders v2 (capture) | Prepaid agent wallets: agents can only spend what you've topped up |
| Orders v2 (authorize, capture, void) | Quote and hold: PayPal holds the quote, Lowtides captures only the real cost and releases the rest; STOP voids open holds |
| Payouts | Pays merchants for approved agent purchases; the kill switch cancels unclaimed payouts |
| Invoicing v2 | Agencies bill clients for their agents' AI usage, with the savings receipt in the memo |
| Vault v3 (payment tokens) | PayPal on file per agent for no-checkout top-ups; KILL and STOP ALL delete the token at PayPal (simulated in the public demo, real with sandbox keys) |
| Webhooks | Signature-verified, credited exactly once; a PayPal-Request-Id on every write so retries never double-charge |
| Agent Toolkit | Lowtides guards the PayPal Agent Toolkit, checking each tool call against your rules first |

## How it maps to the judging criteria
- **Technological implementation:** seven PayPal surfaces (Orders, Payouts and Invoicing move real sandbox money; Vault runs simulated in the public demo), enforcement in the request path, atomic spend decisions, a tamper-evident audit chain, MCP server and API proxy.
- **Design:** a complete product, not a proof of concept: live dashboard, one-tap approvals, a one-minute guided replay, phone-friendly, accessible.
- **Potential impact:** the trust gap is the blocker for agentic commerce. Lowtides gives families and small businesses a budget and a STOP button they understand.
- **Innovation:** one budget across both sides of agent spending, the AI bill and the purchases, plus a kill switch that pulls back money that hasn't landed.
- **Presentation:** the demo opens on a runaway agent and shows every control stopping it, live.

## Challenges
- Making limits hold under a burst of simultaneous purchases (solved by deciding and recording atomically).
- Keeping the AI out of the money decision while still letting people write rules in plain English.
- Being honest about savings: "would have cost" is strictly the same tokens on the requested model, and failed cheap attempts count against us.
- Not replaying side effects: answer memory never caches tool-calling responses.

## What we built during the submission period
Lowtides starts from CostCop v1.0, my earlier cost-saving AI proxy. It's the first commit, kept untouched, so `git diff` against it shows exactly what's new. Everything that governs **purchases** was built during the submission period (which opened Oct 1): the spend API, rules engine, plain-English rules, approvals, PayPal Payouts, the kill switch with payout cancellation and PayPal Vault revocation, the audit chain and evidence packs, the MCP server, signed webhooks, the OpenAPI spec, the guided replay, the combined budget, burst-proof limits, per-visitor demo sandboxes, the product page and the rebrand. The full list is in `CHANGELOG.md`, and `git log` shows each change.

## What's next
A hosted version for families and small businesses, phone push approvals, per-client dashboards for agencies, and a governor bundled with every agent we lease.

## Built with
python, sqlite, javascript, paypal-orders-api, paypal-payouts-api, paypal-invoicing-api, paypal-webhooks, paypal-agent-toolkit, mcp, claude, anthropic-api, ag-grid, render
