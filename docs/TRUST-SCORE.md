# Agent trust score

A number from 0 to 100 for each AI agent, worked out from what that agent has actually done on this Lowtides server. Stores can ask for a minimum score in their Store Policy (`min_trust`), and the shopper can see why the number is what it is.

**It is a heuristic.** It uses only the records this server holds (spend requests, the activity log, the audit log and the Handshake records). It is not a network-wide reputation, it does not follow an agent to another Lowtides server, and a new agent starts from the same place as every other new agent.

## The formula

Start at **40**. Then apply, in time order:

| What happened | Change |
|---|---|
| A purchase was paid and kept | +6 each (the total from this is capped at +36) |
| A person denied a held purchase | -6 each |
| A blocked attempt: the rules refused a purchase, or a Handshake was refused on the shopper side (its own Pass or rules said no) | -4 each |
| The loop guard paused the agent | -15 each |
| You rated a paid purchase thumbs down ("Was this what you wanted?" on /shop/). Changing it to thumbs up removes it. | -5 each |

After every event the score is limited to the range 0 to 100. `history` is the score after each event, so the chart you see is the exact walk the formula took.

**Stop all does not cost trust.** When you press Stop all (or STOP ALL) the agent's Pass is revoked and every store refuses it, but its trust score stays exactly as it was. A payout that Stop all cancels still counts as a purchase that was kept (the agent did nothing wrong), and refusals that happen only because the Pass is revoked cost nothing. Turn the agent back on and it gets a fresh Pass and can shop again at the same score.

What does **not** cost the agent points: a store refusing for its own reason (its minimum trust, a category it doesn't sell, a blocked agent list), and a Handshake that was only a preview (`execute: false`). A held purchase that is still waiting for a person changes nothing yet.

## Grades

`A` 85 and up, `B` 70 to 84, `C` 55 to 69, `D` 40 to 54, `F` below 40.

## API

`GET /api/agents/{id}/trust`

```json
{
  "score": 64, "grade": "C",
  "factors": [
    {"label": "Starting point for every agent", "delta": 40, "detail": "A new agent starts at 40."},
    {"label": "Purchases paid and kept", "delta": 24, "detail": "4 times (+6 each, up to +36 in total)"}
  ],
  "history": [{"at": "2026-11-10T07:12:05Z", "score": 46}],
  "kind": "heuristic",
  "note": "A heuristic from this agent's history on this Lowtides server, not a network reputation."
}
```

The `delta` of each factor is the points it really added after the 0 to 100 limit, so the factors always add up to `score`.

## Worked example (the public demo shopper, Scout)

Scout begins with four simulated purchases: 40 + 4 x 6 = **64**. Harbor Hardware asks for 60, so Scout is in. Brightside Office asks for 70, so Scout is not, until one more purchase is kept (70). A gift-card attempt that Scout's own never-buy list refuses then costs 4 (66), and a purchase you deny at the approval step costs another 6 (60).

The weights are constants in `lowtides/trust.py` (`WEIGHTS`), and a test checks that this page and the code agree.

## Store trust (what the shopper sees)

The score above is how much a **store** can trust an **AI shopper**. The shopper gets the other half: a badge on every store, **Trusted store** (green), **Be careful** (amber) or **Danger** (red), with a number from 0 to 100 behind it. The store sees the shopper's score and badge, the shopper sees the store's, and neither is a public rating.

**It is a heuristic too.** It uses GuardPooch's verdict for the store and this one shopper's own orders at that store. Nothing is shared between shoppers or servers.

### The formula

Start at 75% of GuardPooch's score for the store (so a store GuardPooch scores 100 starts at 75). Then:

| What happened | Change |
|---|---|
| An order was paid and kept at this store | +4 for each order kept, up to +12 in total |
| You rated a purchase here thumbs up | +6 for each thumbs up, up to +12 in total |
| You rated a purchase here thumbs down | -10 for each thumbs down |
| A paid order was refunded at the store (the demo has no store refunds yet, so this stays at 0 here) | -15 for each order pulled back |

The result is limited to 0 to 100. Then GuardPooch has the last word: a store it calls `danger` is never above 39, and a store it calls `caution` is never above 69.

### The badge

* **Trusted store**: GuardPooch says `safe` and the score is 70 or more.
* **Danger**: GuardPooch says `danger`, or the score is below 40.
* **Be careful**: everything else (a `caution` store, or a safe store that has let you down).

### API

`GET /api/stores?agent_id=` adds `store_trust` to each store, `GET /api/catalog` adds `store_trust: {badge, score}` to each item, and `POST /api/handshake` and `POST /api/handshakes/{id}/rating` return it too:

```json
{"badge": "careful", "word": "Be careful", "score": 69,
 "factors": [{"label": "GuardPooch's checks", "delta": 75, "detail": "GuardPooch scored this store 100 out of 100 (a real store). Counted at 75% so your own orders can move it."},
             {"label": "Orders you kept here", "delta": 4, "detail": "1 kept (+4 each, up to +12)"},
             {"label": "Thumbs down from you", "delta": -10, "detail": "1 time (-10 each)"}],
 "kind": "heuristic", "note": "From GuardPooch's checks and your own orders here. Not a public rating."}
```

The factors always add up to the score. The weights are constants in `lowtides/trust.py` (`STORE_WEIGHTS`) and a test checks that this page and the code agree.

### On the store's side

The store's screens show the AI shopper's 0 to 100 score with a matching three-level badge against that store's own minimum (`min_trust` in its Store Policy): **Trusted shopper** at the minimum plus 10 or more, **Meets your minimum**, or **Below your minimum**. Tapping it shows the same factors as `GET /api/agents/{id}/trust`.
