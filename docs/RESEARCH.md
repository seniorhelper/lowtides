# Agent spend: real gaps (live research, 2026-10-28, Sonnet helper)

Summary. Anything marked UNVERIFIED wasn't confirmed against a primary source.

## Top problems
1. Runaway loops and token bills with no hard stop. Example reports (secondhand): a $47k LangChain loop over 11 days (dev.to/igorganapolsky), $1,800 to $6,000 overnight Claude bills (devtoolpicks.com). The lesson from these: enforcement has to sit in the request path, because dashboards only report after the money is gone (decryptiondigest.com).
2. No per-agent budgets, and shared keys, so nobody can tell which agent spent what.
3. Prompt injection leading to payments and fraud. The OWASP 2026 agentic Top 10 lists injection in 6 of 10 categories. Unit 42 described gift-card theft through injection (via proof.com). WorkOS: "agentic fraud looks like clean, fast, successful transactions."
4. Disputes and chargebacks for agent purchases, where liability is unsettled (chargeflow.io, channelx.world).
5. No audit trail or proof of intent to use as dispute evidence.
6. Approval fatigue, forecasting, compliance exports: plausible needs, only weakly sourced.

## PayPal agent tech
- `@paypal/agent-toolkit` (Node) with 43 tools: orders, refunds, disputes, invoices, subscriptions, reporting. MCP via `@paypal/mcp` (UNVERIFIED name). Sandbox is supported.
- Agentic Commerce Services (Agent Ready, Store Sync) are merchant-side and gated behind a contact form, so they can't be used for a sandbox demo.

## Competitor gap
- LiteLLM, Portkey and Helicone handle token budgets and logs, but don't move money.
- Stripe Issuing, Visa and Mastercard handle cards and agent identity, but don't track token spend.
- x402 and Skyfire are crypto only.

**Nobody joins token spend and purchase spend under one budget, one STOP button and one audit trail, with refunds attached.** That's the Lowtides opening.
