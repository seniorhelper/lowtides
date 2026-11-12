# Changelog

Everything here was built during the PayPal AI Hackathon submission period (opened Oct 1, 2026). The first version, CostCop v1.0, is the first commit (Oct 28, 2026); the entries below list each change made on top of it.

## Nov 12, 2026: Action Muzzle

- **Action Muzzle** (`lowtides/muzzle.py`, `/shop/#muzzle`): the both-sides consent check for what an AI does besides buying. The person gives a job in plain words and the AI may do only that; each kind of action is Allowed, Ask me first or Never; each business publishes a signed action policy; the watchdog refuses lookalikes; outcomes are match, needs_yes, no_match.
- A deposit, fare or fee on an action is checked against the Pass's money limits and paid through PayPal (sandbox) only after both sides say yes.
- **Stop word and freeze:** typing or saying the stop word to Ask Lowtides, or pressing Freeze, holds every AI shopper at every store and business until released. Nothing is canceled.
- Signed action receipts, listed and searchable with the other receipts. `POST /v1/action` and the `request_action` MCP tool for outside agents.
- Demo businesses are fictional and nothing real is contacted. See `docs/ACTION-MUZZLE.md`.

## Nov 11, 2026 (night): optional sign-in accounts, privacy page

- **Accounts (off unless configured).** A shopper or a store can sign in and keep their own dashboard across visits and devices. Supabase Auth holds the email and password hash; Lowtides stores no password. The account's whole sandbox is saved to one row after each change and loaded at sign-in. The visitor demo needs no login and is unchanged. See `docs/ACCOUNTS.md`.
- `POST /api/account/signup`, `/api/account/signin`, `/api/account/signout`, `GET /api/account`. Rate limited, same-site only, signed HttpOnly cookie.
- New `/privacy/` page, linked from every footer.

## Nov 11, 2026 (late): Stop all, wide hero, both-sides tour

- The red button is now called **Stop all** on the home, shopper, store and Handshake pages and in the guide's answers ("Stop all: revokes Scout's Pass at every store at once"). Stopping one shopper is **Stop**. On the developer dashboard at `/app/`, stopping one agent for good is **Shut off** and the round button is still STOP ALL. API paths did not change.
- Home page hero is one wide row (words left, search and buttons right) with the live Handshake animation full width under it.
- New "Two control panels" click-through on the home page: five numbered spots across a shopper panel and a store panel, with sample data and links to the real pages.
- The video block (still off until a YouTube id is set) is labeled "Watch the 3-minute story" and asks YouTube for captions.

## Nov 11, 2026 (night): talk mode, blog, site map

- **Talk with the guide.** Tap Mic once and it becomes a spoken back-and-forth: ask out loud, the answer is read aloud, then the mic opens again by itself. Silence, the Stop button, closing the panel or another tap ends it. The guide still never moves money.
- **Blog** at `/blog/` with three launch guides (AI shopping safety, consent in agentic commerce, how the PayPal sandbox works), each with Article, Breadcrumb and FAQ schema and outside sources linked.
- **Site map** page at `/sitemap/`; `sitemap.xml` and `llms.txt` list the new pages.
- "Patent Pending" in every footer.

## Nov 11, 2026 (evening): say it and it shops, receipts, talk to a human

- **Off / AI Assist / Full Autopilot** now sits beside the search on the shopper page (stores have the same three words). Type "cordless drill under $90" and press one button: `POST /api/shopper/autopilot` picks the best item both sides accept and runs one ordinary Handshake. Full Autopilot pays a green match, AI Assist holds it for a yes, Off buys nothing. Red never pays.
- **Receipts.** `GET /api/receipts?q=` lists every purchase a shopper made or tried (with its signed proof) plus receipts a person adds for things bought elsewhere (`POST /api/receipts`, typed or pasted text). Plain-English search such as "refunded this week over $20". The search is rule-based: no AI model reads receipts.
- **Talk to AI guide / Talk to a human** at the top right of every page. The human request (`POST /api/help`) carries the person's chat with the guide. It is saved for the operator and sent to `LOWTIDES_HELP_WEBHOOK` when that is set; with neither that nor `LOWTIDES_HELP_TOKEN` set, the page says plainly that nobody is on shift.
- The search box now understands a price in the words ("drill under $90").

## Nov 11, 2026: outside agents get the two-sided Handshake

- **Agent door.** `GET /v1/stores`, `POST /v1/handshake` and `GET /v1/handshake/{id}`, with the agent's own key. Same engine as the site's buttons: match, needs_yes or no_match, both sides' checks, a signed receipt, and a `next` line that tells the agent what to do.
- **MCP.** Two new tools, `list_stores` and `handshake` (six in all).
- **Connect your own AI** on the shopper page: copy-paste setups for MCP, a web call and Python, with the selected shopper's key.
- **Search from the front door.** The home page search bar opens the shopper page on that search.
- Ask Lowtides answers "how do I connect my own AI agent?".

## 2026-11-10 (b): attack bench, README and Devpost text, judging checklist
- **Attack bench** (`scripts/attack_bench.py`, `docs/ATTACK-BENCH.md`, `tests/test_attack_bench.py`): starts the demo server in-process on a temporary database (simulated PayPal, no keys, localhost only) and plays 31 bad purchase attempts against the real HTTP API, plus 4 legitimate purchases that must succeed. Attacks: lookalike store, per-item cap, daily budget, never-buy category, category not allowed, store no-human limit, minimum trust, store Closed, shopper Off, Stop all across every store, revoked Pass at `/v1/pass/verify`, prompt injection (three ways), tampered Pass, Store Policy, receipt and proof pack, approval and purchase replays, an agent approving its own hold, direct `/v1/spend` while Off or after Stop all, the Ask guide told to buy, and another visitor confirming a proposal. A row passes only if the refusal is the expected one and the server's own ledger shows $0.00 moved. The summary line is computed, the exit code is non-zero on any failure, and `--update-docs` rewrites the result tables in the README, the bench doc and the Devpost text.
- **README:** an architecture diagram near the top, the attack-bench table, a Screenshots section (images still to add), the testing-window note (demo live and unchanged until Dec 21, 2026), and the trust-score line corrected (Stop all no longer costs trust). The long cost-governor manual moved to `docs/LEGACY-COSTCOP.md`.
- **`docs/DEVPOST_SUBMISSION.md`:** opens with the problem, the audience and what is new, then "What a judge can verify in 2 minutes"; one tagline; a marked note on what to change once the sandbox and AI keys are set; what is next (portable reputation, shopper-controlled data sharing, Orders v2 and Vault checkout for agent orders, market insights).
- **`docs/JUDGING_CHECKLIST.md`:** testing window corrected to Dec 21, 2026, the freeze rule (nothing changes after Thu Nov 12, 12:00 pm PT), and a pre-submit checklist for the owner.
- **Tests:** 552 pass.

## 2026-11-10: Consent Shopping ("both say yes, or no money moves")
- **The engine** (`lowtides/handshake.py`, `stores.py`, `guardpooch.py`, `trust.py`): a shopper's Pass, a store's signed Store Policy and a store safety check are compared in both directions. Three signals: `match` (both said yes), `needs_yes` (a person must say yes, a normal held approval) and `no_match` (no money moved). `evaluate()` is a pure function covering every rule on both sides.
  - **Store Policy:** per-store rules for AI shoppers (max order with no human, categories sold to agents, minimum trust, blocked agents, require evidence), signed with Ed25519 like the Pass and published at `/.well-known/lowtides-store-policy.json?store=`. `PUT /api/stores/{id}/policy` signs it again.
  - **Handshake receipts** are signed and written into the hash-chained audit log. `GET /api/handshakes/{id}/proof` returns a proof pack (Pass, policy, receipt, approval, PayPal ids, audit entry) that `verify_proof()` checks with public keys only.
  - **GuardPooch:** domain age, lookalike name, signed policy and too-good prices give a store a `safe`, `caution` or `danger` verdict. Fixed seed data in the demo; the optional live RDAP lookup is off by default and never runs for `.example` domains or in the public demo.
  - **Trust score:** 0 to 100 from the agent's own history (`docs/TRUST-SCORE.md`). A heuristic. A refused attempt or a denied approval visibly lowers it.
  - **Shopper preferences** (`GET|PUT /api/shopper/prefs`) rewrite the agent's enforced rules and re-issue its Pass. **Match Finder** (`GET /api/catalog`) ranks items green, yellow, red. **Merchant KPIs** (`GET /api/merchant/kpis`).
  - Five fictional demo stores (one lookalike scam) and a demo shopper, Scout, made the first time a visitor's sandbox uses a shopping route. The original runaway-agent story is untouched.
  - `execute: true` on a green match pays through the existing spend path: a PayPal sandbox payout when keys are set, a labeled simulation otherwise. STOP ALL / Stop all make every later Handshake a `no_match` at every store.
- **Consumer front door** at `/` (the shared `brand.css`); the original dashboard moved to `/app/`.
- **Ask Lowtides** (`lowtides/assistant.py`, `ui/ask.js`, `docs/ASK-LOWTIDES.md`): a talking guide on every page with two brains, an AI model with tool calling and a scripted one. It only proposes; a person's tap on Confirm runs one action, and it can never move money. Claude answers are metered under their own agent with a hard daily cap, and the public demo is scripted unless the owner opts in.
- **Shopper, store and "See both sides" pages** (`/shop/`, `/merchants/`, `/handshake/`) built on these routes.
- **Docs:** README reorganized for a five-minute judge (try-it path, PayPal and AI tables, honest limits), `docs/HANDSHAKE.md`, `docs/DEVPOST_SUBMISSION.md`, `docs/JUDGING_CHECKLIST.md`, and a "Consent Shopping" folder in the Postman collection. OpenAPI updated.
- **Small supporting changes:** `PUT` is routed, `SpendGovernor.request` accepts extra reasons for a person to decide, and the demo reset also resets the stores.
- **Tests:** 493 pass (`python -m pytest -q`), including the Handshake, front door and Ask suites.

## 2026-10-29 (d)
- **Lowtides Pass for merchants:** check an AI agent's pass right before you capture its PayPal payment. Capture when Lowtides says allowed, hold when the owner must approve, refuse (and void the PayPal authorization) when the pass is revoked, expired, forged or the purchase is denied. Fails closed when Lowtides can't be reached.
  - `integrations/merchant-check/lowtides-pass-check.js`: dependency-free ES module for browsers and Node. Verifies the Ed25519 signature offline with Web Crypto (public keys fetched from `/.well-known/lowtides-pass-keys.json` and cached), and always checks revocation online with `POST /v1/pass/verify`. Keys come from the server you trust, never from the pass itself.
  - `integrations/merchant-check/paypal_capture_guard.py`: standard-library Python guard around Orders v2 capture: reads the order's real amount, checks the pass, then captures, holds, or refuses and voids the authorization. Dry run without PayPal keys; PayPal sandbox demo with them.
  - New public page **`/merchants/`** ("For merchants: check an AI agent's Pass before you take its money") with a live checkout simulator: a real pass from this server, offline signature check in the browser, the online check, and the Capture / Hold / Refuse result. Buttons to tamper with the pass and, in the public demo, to press Stop all as the owner and watch the same pass get refused.
  - Added to `sitemap.xml` and `llms.txt`. Docs: `integrations/merchant-check/README.md` (with a sequence diagram), a "For merchants" section in `docs/LOWTIDES-PASS.md` and the README.
  - Tests: `tests/test_merchant_guard.py` (fake PayPal + real local Lowtides: valid captures, Stop all refuses and voids, owner revoke refuses, over the approval line holds, over the limit refuses, tampered pass refuses, Lowtides down holds, CLI dry run, the page) and a `node --test` suite for the JS module.

## 2026-10-29 (c)
- **Lowtides Pass:** a signed spending permission an AI agent carries, so merchants and other agents can check that it's allowed to spend. It says who the agent is, which Lowtides server governs it, and its live limits (per purchase, per day, approval line, allowed and blocked categories, blocked merchants, hours, expiry). AP2-aligned: shaped like an AP2 Intent Mandate (plain-English intent plus structured constraints), not an official AP2 integration.
  - Signed with Ed25519 in pure Python (`lowtides/ed25519.py`, checked against the RFC 8032 test vectors). JWS-style `header.payload.signature`, `alg: EdDSA`, `typ: lowtides-pass+jws`. The key is made once and stored with a key ID.
  - `GET /v1/pass` (agent), `GET /.well-known/lowtides-pass-keys.json` and `POST /v1/pass/verify` (public, read-only: "would this purchase be allowed?" with the same checks as a real spend request). Tampered passes, `alg: none` and unknown keys are refused.
  - **Stop all, STOP, a loop-guard pause, a rules change and the owner's Revoke button revoke it instantly**, and it stays revoked after a restart.
  - New public page **`/pass/`** ("Check a Lowtides Pass"), a **Pass** button on each agent in the dashboard (copy, open verifier, revoke, issue a new one), and the MCP tool `get_pass`.
  - The demo agent gets a pass, and the replay's Stop all step now shows it revoked.
  - Docs: `docs/LOWTIDES-PASS.md`. 25 new tests (277 total).

## 2026-10-29 (b)
- **PayPal on file, revoked by the stop switch** (new `lowtides/vault.py`): each agent can have a saved PayPal payment method, a **PayPal Vault v3** payment token (setup token with `usage_type` MERCHANT, approved by the payer, then turned into a payment token).
  - **Shut off and STOP ALL now revoke it at PayPal** (`DELETE /v3/vault/payment-tokens/{id}`), so stopping an agent cuts off its money source, not just the agent. The stop result, the activity feed and the audit log all say so: "Revoked the saved PayPal payment method, so this agent can't be charged again until you reconnect it."
  - **Honest failures:** if PayPal refuses the revoke, the token is marked `revoke_failed`, the stop result lists it under problems, and the agent's purchases stay blocked. A PayPal server error is retried once; the next Stop all or the Revoke button tries again.
  - **Purchases refused after a revoke:** once an agent has had PayPal on file, `POST /v1/spend` is denied (and approving a held purchase is refused) until you reconnect it.
  - **Top up from PayPal on file:** wallet top-ups charged straight to the saved token with Orders v2 `payment_source.paypal.vault_id`, no checkout page.
  - New routes: `POST /api/agents/{id}/vault/connect|revoke|topup`, `GET /api/agents/{id}/vault`, and `/paypal/return?kind=vault`.
  - Simulated, and labeled "simulated", without PayPal keys and in the public demo. The demo story and the guided tour connect a simulated token, so Stop all visibly revokes it.
- **Dashboard:** a "PayPal on file" panel on the Wallet tab (connected / revoked, Connect, Revoke, top up), the status on each agent card, and the stop dialog and summary mention the revoke.
- New `vault_tokens` table, created on start for existing databases too.
- **Tests:** 21 new tests (fake PayPal now speaks the Vault v3 endpoints): connect, return URL, revoke on Stop all and on STOP ALL, purchases denied after a revoke, failed and retried revokes, simulated mode, demo seed, old databases.

## 2026-10-29
- **A private demo for every visitor** (public demo only, new `lowtides/sandbox.py`): each browser gets a signed `lt_demo` cookie (HttpOnly, SameSite=Lax, Secure over https) and its own in-memory sandbox, seeded with the runaway-agent story. STOP ALL, Stop all, Reset, "simulate runaway" and the guided replay now affect only the caller; agent calls find their sandbox by agent key. Bounded: 200 sandboxes (least recently used evicted), 2 hours idle expiry, 8 MB per sandbox database and 160 MB for all of them, 30 new sandboxes per IP per hour (after that, the shared demo), and per-IP rate limits on POSTs (20/s, bursts of 80) with a friendly 429. Callers without a cookie (curl, the scenario CLI) keep the shared demo. Self-hosted mode is unchanged.
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
- **Plain-English rules:** AI turns typed rules into strict, validated rules, with an offline parser when no key is set. The AI never decides a payment.
- **One-tap approvals** on a new Approvals tab, plus a decision log with the reasons for every decision.
- **Stop switch:** stops the agent, denies its pending buys, voids its open PayPal holds and cancels its unclaimed payouts. STOP ALL does this for every agent.
- **Proof for disputes:** a hash-chained, tamper-evident audit log (`/api/audit/verify`), and a dispute evidence pack per purchase (`/api/spend/{id}/evidence`).
- **Dashboard:** spend forecast (`/api/forecast`, tracking whichever limit is closest), the dot-tide "Tide chart", a Merchants list with PayPal payees, and a Rules dialog.
- **MCP server** (`python -m lowtides.mcp`) with `request_spend`, `check_spend` and `get_budget`, so any MCP agent asks before buying.
- **One-command runaway-agent replay** for the demo (`python -m lowtides.scenario`).
- **Combined budget for model calls too:** the AI-plus-purchases daily cap now also blocks model calls.
- **Tests:** 53 new tests (125 total). Fixed two old tests that failed now and then on Windows because of how its clock ticks.
- **Live research** on the real gaps in agent spending, with sources: `docs/RESEARCH.md`.
