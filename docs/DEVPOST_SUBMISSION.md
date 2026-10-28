# Devpost submission text (draft)

## Inspiration
I run more than seven AI agents across my businesses and lease agents to clients. The bills were unpredictable, and I could never answer "what did that agent cost, and was it worth it?" Research backs this up: a 2026 Stanford Digital Economy Lab study found agent tasks use about 1,000x more tokens than chat, and that agents can't predict their own costs.

## What it does
Lowtides is a cost governor for AI agents. Agents point at Lowtides instead of the model provider, with one line changed. Every call then goes through:
- **Answer memory:** exact repeats are free. Tool calls are never replayed.
- **Context trimming:** old tool output the agent keeps re-reading is shortened safely.
- **Automatic prompt caching** for Claude.
- **Cheapest fit:** a free grader starts each step on the cheapest suitable model, never above the model the agent asked for.
- **Check and climb:** weak, unsure or broken answers move up one model, and those attempts are counted honestly.
- **The governor:** daily allowances, per-job budgets, a prepaid wallet, a speed limit, a loop detector that pauses stuck agents, and STOP / STOP ALL.
- **A receipt in plain dollars:** "Would have cost $X. You paid $Y."

**PayPal is the money layer:**
- **Wallet top-ups** (Orders API, capture): agents spend only prepaid allowance.
- **Quote and hold** (Orders API, authorize, then capture or void): Lowtides quotes a job from history, PayPal holds the quote, and when the job is done Lowtides captures only the real cost and releases the rest. It falls back to capture-then-refund on accounts without partial capture, and voids holds under half a cent.
- **Client invoices** (Invoicing API v2): agencies bill clients for the AI they run, with markup and the savings receipt in the memo.
- **Webhooks**, with signature verification and duplicate protection. Every write uses a PayPal-Request-Id, so a retry can never double-charge.

## How we built it
Python standard library only, with no dependencies. SQLite for the ledger, and a vanilla JS dashboard with an AG Grid ledger view. It's a drop-in for the OpenAI and Anthropic APIs, including streaming. Built with Claude (Anthropic) as the coding partner, and Claude models (Haiku, Sonnet, Opus) are the default model ladder.

## Challenges
- Being honest about savings. "Would have cost" is defined strictly: the same tokens on the requested model, with no trimming, no Lowtides-added caching and no memory. Failed cheap attempts count against us.
- Not breaking agents. Trimming never removes a message, so tool call/result pairs stay intact.
- Not replaying side effects. Answer memory refuses to cache tool-calling responses.

## What's next
Hosted version for small businesses, per-client dashboards, and bundling a governor with every agent we lease.

## Built with
python, sqlite, javascript, paypal-orders-api, paypal-invoicing-api, paypal-webhooks, claude, anthropic-api, ag-grid
