# Changelog

Everything changed during the PayPal AI Hackathon (submission period started Oct 1, 2026). The untouched v1.0 is the first commit, so `git log` shows each change on top of it.

## 2026-10-29
- **Burst-proof spending limits:** purchase decisions are now made one at a time (decide and record under one lock), so 20 simultaneous requests can no longer each read the same "spent so far" total and overspend a daily limit. New test fires 20 purchases at the same instant.
- **Startup screen:** a 2-second branded intro on the first visit of a session (skippable, respects reduced motion), shown while the dashboard loads underneath.
- **Product page** at `/why/`: the problem, how it works, the PayPal APIs used, who it's for, and an FAQ.
- **Search and AI discovery:** page titles and descriptions, Open Graph and Twitter cards, structured data (SoftwareApplication, FAQPage, BreadcrumbList), `robots.txt`, `sitemap.xml` and `llms.txt`. The server now serves folder pages (`/why/`).
- **Always awake for judges:** an outside monitor pings `/health` every 5 minutes (GitHub's scheduler alone ran too rarely), so the free host never shows its wake-up screen.
- Replay offer now says 1 minute, matching the tour's real length.
- Version 1.1.0.

## 2026-10-28
- Renamed the product to **Lowtides, the AI spend governor**: app name, the Python package (`lowtides`), env vars (`LOWTIDES_*`), agent headers (`X-Lowtides-*`), PayPal checkout brand name, docs and tests.
- New brand: the animated "dot tide" logo, favicons and the Sea Glass palette, plus Sora, Instrument Sans and JetBrains Mono fonts in the dashboard.
- Merged the folder layout into a single repo root.
- License changed from MIT to AGPL-3.0 (official GNU text in `LICENSE`).
- **Agent purchases governed end to end** (new `lowtides/spend.py`, `POST /v1/spend`, `GET /v1/spend/{id}`, `GET /v1/budget`):
  - **Per-agent rules:** per-purchase cap, daily purchase limit, auto-approve line, combined AI-plus-purchases daily cap, allowed and blocked categories, blocked merchants, allowed hours, and purchases per hour.
  - **Held for a person:** new payees, text that reads like prompt injection, duplicate charges within 10 minutes, and amounts over 3x the agent's typical purchase.
  - **Paying:** approved buys are paid with **PayPal Payouts**. Without keys, payouts are simulated and labeled as simulated.
- **Plain-English rules:** Claude (claude-haiku-5-5) turns typed rules into strict, validated rules, with an offline parser when no key is set. The AI never decides a payment.
- **One-tap approvals** on a new Approvals tab, plus a decision log with the reasons for every decision.
- **Kill switch:** stops the agent, denies its pending buys, voids its open PayPal holds and cancels its unclaimed payouts. STOP ALL does this for every agent.
- **Proof for disputes:** a hash-chained, tamper-evident audit log (`/api/audit/verify`), and a dispute evidence pack per purchase (`/api/spend/{id}/evidence`).
- **Dashboard:** spend forecast (`/api/forecast`, tracking whichever limit is closest), the dot-tide "Tide chart", a Merchants list with PayPal payees, and a Rules dialog.
- **MCP server** (`python -m lowtides.mcp`) with `request_spend`, `check_spend` and `get_budget`, so any MCP agent asks before buying.
- **One-command runaway-agent replay** for the demo (`python -m lowtides.scenario`).
- **Combined budget for model calls too:** the AI-plus-purchases daily cap now also blocks model calls.
- **Tests:** 53 new tests (125 total). Fixed two old tests that failed now and then on Windows because of how its clock ticks.
- **Live research** on the real gaps in agent spending, with sources: `docs/RESEARCH.md`.
