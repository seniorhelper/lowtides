# Ask Lowtides

## Turn on the AI brain (exact steps)

Out of the box the public demo uses the free scripted guide and makes **no outbound call**. To switch the AI brain on:

1. Set two environment variables on the server (locally in a private env file kept outside the repo, on Render under Environment):
   * `ANTHROPIC_API_KEY` = your Anthropic key.
   * `LOWTIDES_ASK_CLAUDE=1` (the public demo only calls the AI when this is set; a private copy needs just the key).
2. Optional: `LOWTIDES_ASK_DAILY_CAP=1.00` is the **hard daily dollar cap** for the whole server (default `$1.00` per UTC day, shared by every demo sandbox). It can also be set in the config file as `assistant.daily_cap_usd`. The model is `claude-haiku-5-5` (the cheapest Claude in the price table in `config.example.json`); change it with `LOWTIDES_ASK_MODEL` or `assistant.model`, but the id must be in the price table or the guide stays scripted.
3. Restart the server.
4. **Confirm it is live:** open any page, tap **Ask Lowtides**, and ask a question. Under the answer the small tag must say **answered by AI** (not "scripted answer") and show a cost like `$0.0004`. The dashboard (`/app/`) also shows an agent named **Ask Lowtides** with that spend.
5. To turn it off again: remove the variable, or set `LOWTIDES_ASK_CLAUDE=off` (off everywhere, even with a key).

If the tag says "scripted answer", the AI brain was not used: no key, the flag is missing in the public demo, the daily cap is spent, the visitor hit the hourly limit (40 an hour), the Ask Lowtides agent is stopped or out of budget, or the call failed or took longer than 15 seconds. The visitor still gets an answer.

The AI brain gets: the product vocabulary and honesty rules, a true guide to every page and feature (Shop, Merchants, See both sides, the store numbers one by one, modes, policy editor, what is real PayPal sandbox and what is simulated, how a store would adopt it, the Devpost context, how to try the demo in 60 seconds), and the page the person is on. For anything about live data it must call a read tool first. It can only **propose**; it can never act.

## Turn on the premium voice

Voice always works for free with the voices built into the browser. The **premium voice** is an upgrade: the server reads each answer with Google Cloud Text-to-Speech (Chirp 3 HD, `en-US-Chirp3-HD-Algenib`, MP3) and the browser plays it. It is off until you add a key, has a hard daily cap, and silently falls back to the browser voice whenever it is off, capped, or fails.

1. In the Google Cloud project, enable the **Cloud Text-to-Speech API**, then create an **API key restricted to that one API** (APIs & Services, Credentials, Create credentials, API key, Restrict key, Cloud Text-to-Speech API).
2. Set `GOOGLE_TTS_API_KEY` to that key: locally in a private env file kept outside the repo, on Render as an environment variable. The key stays on the server and never reaches the browser.
3. Optional settings (config key `assistant`, all optional): `voice_name` (default `en-US-Chirp3-HD-Algenib`), `voice_lang` (`en-US`), `voice_rate` (`1.0`), `voice_daily_chars` (default **60000**), `voice_daily_requests` (default **300**), `voice_price_per_m` (default **30.00** USD per million characters, used for the ledger line), `voice_max_chars` (600 per request), `voice_enabled`. `LOWTIDES_ASK_VOICE=off` turns it off everywhere.
4. Restart. **Confirm it is live:** open the panel, tap **Read aloud** (a row appears), ask a question, and the row says **Premium voice** while it speaks. If it says **Browser voice**, the key is missing, a cap is reached, or the call failed. Both voices are labeled honestly.

**Cost at the default cap.** At $30 per million characters, 60,000 characters a day is at most **$1.80 a day** (about $54 a month if it were hit every day), and never more than 300 paid calls a day. Typical use is far less, because repeats are cached and answers are short (a 40-word answer is about 250 characters, roughly $0.0075). Each paid call is a cost line in the ledger under **Ask Lowtides**, so the dashboard shows the voice bill next to the AI bill.

How it is kept safe:

* `POST /api/ask/speak {text}` only reads text the assistant itself just gave **that visitor** (the last few answers and confirmations, plus the two fixed greetings). Anything else gets a 403, so it is not a free text-to-speech service.
* Text is cleaned server side the same way the browser cleans it (no links, ids or symbols, "0-100" read as words), then cut to 600 characters at a sentence end.
* The daily character and request caps are for the whole server and are **reserved before the call** (a failed call gives its reservation back). The counter is saved to `ask_voice_cap.json` next to the database, so a restart does not reset it. On Render, `LOWTIDES_DB` points at `/tmp`, which is wiped on a redeploy; attach a persistent disk and point `LOWTIDES_DB` at it if you want the counter to survive redeploys too.
* Audio is cached by a hash of voice and text (in memory, bounded, and on disk in `ask_voice_cache/` when a data folder exists), so a repeated answer costs nothing.
* Per-visitor rate limit; the site's Content-Security-Policy already allows `media-src 'self' blob:`.
* Not configured, off, capped, slow-down, stopped agent or an upstream error all return `200 {"fallback":"browser","reason":...}` and the page uses the browser voice.

## What it is

A talking guide on every page that knows the whole system. It explains, looks things up in the running product's real data, and **suggests** changes. A person's tap on Confirm is the only thing that ever changes anything, and it can never move money by itself.

## Using it
Include two lines on any page (it mounts its own button and panel, no markup needed):

    <link rel="stylesheet" href="/ask.css">
    <script src="/ask.js" defer></script>

* `window.LowtidesAsk.open("Why was it held?")` opens the panel and asks. `close()` and `toggle()` too.
* Any element with `data-ask="question"` opens the panel with that question when clicked.
* Suggested-question chips change with the page. Esc closes; focus stays inside the panel.
* The panel shows "answered by AI" or "scripted answer", and the cost when it is above $0.

### Voice (browser, free)
* The **Read aloud** button (off by default, remembered in localStorage) reads answers aloud. It picks the best English voice the browser has: names with Natural, Neural or Online first, then Google US English, Samantha, Aria, Jenny, Ava; en-US over other English; it waits for the browser's `voiceschanged` event. Rate 0.98, pitch 1.0.
* The text is cleaned first (no URLs, ids or symbols; "$11.99" stays; "0-100" reads as "zero to one hundred") and split into short sentences that are queued, so long answers do not hit Chrome's ~15 second cutoff.
* Speech stops when the panel closes, Read aloud goes off, a new question is asked, or **Stop** is tapped (a small Stop button shows while it speaks). When more than one good English voice exists a picker appears next to the label; the choice is remembered in localStorage.
* **Talk** says a short hello, then uses the browser's speech recognition: it says "Listening...", sends automatically when it hears the final words, and shows a plain message when the microphone is blocked, missing, or the browser cannot do speech recognition. The server sends `Permissions-Policy: microphone=(self)`, which allows it on this site.

## API
`POST /api/ask` body `{question, page}` returns
`{answer, source: "claude"|"scripted", actions: [{id, label, kind, params, expires_in}], cost_usd}`.

`POST /api/ask/confirm` body `{action_id}` runs one suggestion; `{action_id, cancel: true}` drops it.

`POST /api/ask/speak` body `{text}` returns `audio/mpeg`, or `{fallback: "browser", reason}`.

Same-site and admin-password rules are the same as every other `/api/` write.

## The safety design
* **Read tools** (no side effects): `get_state`, `get_ledger`, `list_stores`, `list_handshakes`,
  `get_trust`, `find_matches`. Stores, handshakes, trust and the catalog come from other parts of
  Lowtides through this server's own contract routes; if one isn't there the answer says
  "that part isn't available yet".
* **One write tool**: `propose_action(kind, params)`. It only stores a card. Kinds: `set_prefs`,
  `approve_purchase`, `deny_purchase`, `kill_agent`, `stop_all`, `run_handshake`. Nothing else exists,
  so there is no way to buy something, pay, or edit a rule from a question, however it is worded or
  whatever a model is tricked into saying.
* **Proposals** have an unguessable id, belong to the visitor they were shown to (a demo sandbox, else
  the demo cookie, else the caller's address), expire in 5 minutes and work once. A wrong, expired or
  someone else's id is refused (unknown and foreign ids look identical).
* **Confirm** re-validates, then runs the existing functions: `spend.approve / deny / kill`,
  `App.stop_all`, the saved spending rules (or `PUT /api/shopper/prefs` when present, which re-issues the
  Pass), and `POST /api/handshake` (`run_handshake` shows what it will do; paying only happens if both
  sides say yes, in PayPal sandbox, after the tap). Every confirm is written to the hash-chained audit log.

## Two brains
1. **The AI brain** (tool calling, default `claude-haiku-5-5`, max 300 output tokens, up to 3 tool rounds) when a
   key is configured and the request is allowed. The key stays on the server.
2. **Scripted**: about 70 intents. Store safety ("Is this store safe?", "can I trust Tidepool Pets", the lookalike
   by name or domain) answers from that store's real GuardPooch verdict and trust badge ("Fake store", "Be careful",
   "Real store"); with no store named it uses the store in your latest Handshake, else lists every demo store.
   There is one answer per page and feature (Autopilot / Assist / Off, the three sliders, GuardPooch strictness,
   Deals vs. safest, Match Finder, the buttons on a match, the store trust badge, thumbs, My AI shoppers and presets,
   the plain-words box, Where your money went, proof pack, Stop all vs Off, Reset demo; on Merchants the store modes, the
   policy editor and Sign and publish, the public policy file, Opportunities, each numbers tile, the tables and CSV,
   the shopper trust labels, the Pass checker), what is real PayPal sandbox and what is simulated, how a store gets
   paid, how a store would adopt it, the hackathon context, the 90-second and 30-second tours, who built it, plus
   commands ("raise my daily cap to 150", "stop Scout", "stop everything", "show held purchases"). Questions that
   match nothing get an honest "I'm the built-in guide" reply instead of a wrong answer. Small misspellings of the
   product's own words ("hndshake", "gaurdpooch") are corrected first. `source` always says which brain answered.

The knowledge lines live in one table (`K` in `lowtides/assistant.py`); the scripted answers and the AI prompt
both use it, and a test checks the button labels they name against the real pages.

## Lowtides governs its own brain
Each AI answer is written to the same ledger as any AI call, under the agent **Ask Lowtides** (created on
first use, visible on the dashboard, stoppable like any agent). A **hard daily cap** (default $1.00 per UTC
day, shared by the whole server and every demo sandbox) is reserved before each call at its worst-case cost,
so it cannot be overshot. The existing per-IP rate limits apply to `/api/ask` too. The premium voice is metered
into the same ledger, call by call.

## Settings
Config key `assistant` (all optional): `model`, `daily_cap_usd`, `agent_daily_budget`, `max_tokens`,
`max_rounds`, `timeout`, `per_owner_hour`, `claude_in_demo`, and the voice keys listed above. Environment:
`ANTHROPIC_API_KEY`, `LOWTIDES_ASK_CLAUDE`, `LOWTIDES_ASK_MODEL`, `LOWTIDES_ASK_DAILY_CAP`, `GOOGLE_TTS_API_KEY`,
`LOWTIDES_ASK_VOICE`.
