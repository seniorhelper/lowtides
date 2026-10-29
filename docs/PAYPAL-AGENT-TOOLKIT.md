# Lowtides in front of the PayPal Agent Toolkit

## What it is

PayPal publishes an official Agent Toolkit. It hands an AI agent ready-made PayPal tools: create and capture orders, invoices, subscriptions, disputes, refunds, and reports. It comes as `paypal-agent-toolkit` for Python (PyPI, 1.11.0, Sep 2026) and `@paypal/agent-toolkit` for TypeScript (npm, 1.11.0). PayPal also runs an MCP server built on it (`npx -y @paypal/mcp`, or hosted at `mcp.paypal.com` / `mcp.sandbox.paypal.com`).

The toolkit gives an agent the power to move money. It doesn't decide whether the agent should.

`integrations/paypal-agent-toolkit/lowtides_guard.py` adds that decision. One line, `guard_toolkit(toolkit)`, makes every money-moving toolkit tool ask Lowtides first (`POST /v1/spend`):

- **Approved:** the PayPal tool runs.
- **Needs approval:** the tool waits. The agent is told "waiting for owner approval" and gets the request id. The owner approves on the Lowtides dashboard, and the agent's next try goes through.
- **Denied:** the tool doesn't run. The agent gets the plain-English reasons.

Read-only tools pass straight through. If Lowtides can't be reached, or the guard can't tell how much money is involved, the tool doesn't run (fail closed). The same applies to a tool name the guard has never seen.

## Why it matters

- **One policy for every agent.** The same per-agent rules apply whether an agent pays through Lowtides' own spend API or through PayPal's toolkit. That covers the daily limit, ask-over-$X, blocked categories, new-merchant checks, the kill switch, and the hash-chained audit log.
- **No agent rewrite.** The guard wraps the toolkit's single `PayPalAPI.run` call, which every adapter uses (OpenAI Agents SDK, LangChain, CrewAI, Bedrock). Agents keep the tools they already have.
- **Real amounts, not claimed ones.** Before a capture, the guard reads the order from PayPal and sends that total to Lowtides. A prompt-injected agent can't pass a $5,000 capture off as $5.

## Limits today

- The guard covers the **Python** toolkit. The TypeScript toolkit and the `@paypal/mcp` server need a matching Node wrapper or an MCP proxy. The same `/v1/spend` call would work for either; neither is built yet.
- Lowtides' spend API was built for Lowtides to pay merchants itself, through PayPal Payouts. The guard marks its requests `"settle": "external"` so the server records and enforces them without paying them out (status `paid`, payout status `PAID_BY_CALLER`). The toolkit's own PayPal call moves the money, so nothing is paid twice.
- Sandbox only. Never put live PayPal keys in a demo.
