# Leash: stop data dumps

A leash keeps one AI call from getting huge by accident, like an agent pasting a whole database into a prompt or writing a 20-page reply. You set it per agent in **Rules** on the dashboard, under "Leash (data dumps)".

## The four leashes

| Setting | What it does | Rule key |
|---|---|---|
| Max reply length | Limits how long the AI's answer can be. Short is about 400 tokens, Medium about 1,000, Long about 4,000, or type your own. | `max_reply_tokens` |
| Biggest prompt allowed | Blocks a call whose prompt is bigger than this many pages. 1 page is about 500 tokens. Blank means no limit. | `max_input_tokens` |
| Max cost per AI call | Blocks a single call that would cost more than this many dollars. Blank means no limit. | `max_cost_per_call` |
| Alert when a call is X times bigger than usual | Flags (does not block) a call that is X times the agent's usual size. Default 5. Set 0 to turn it off. | `dump_alert_multiple` |

## Examples

- Plain English in the Rules box: `Keep replies short. Never send more than 50 pages. No single call over 25 cents.`
- Same thing as rules: `{"max_reply_tokens": 400, "max_input_tokens": 25000, "max_cost_per_call": 0.25}`

## What happens when a leash is hit

- **Prompt too big or call too expensive:** the call is blocked before it is sent, so you are not charged. The agent gets a plain message saying which limit was hit, so it can shorten the prompt and try again.
- **Reply length:** the model is told to stop at your limit, so long answers end early.
- **Bigger than usual:** the call goes through, and it is flagged on the dashboard so you can look.

## Limits

- Token counts are estimates (about 4 characters per token), not exact. Leave some room.
- Images and files sent inline (base64) are counted by size. An image or PDF sent only as a link or file ID counts as a flat ~1,600 tokens, because Lowtides can't see how big it is. A huge PDF sent that way can still slip past the prompt limit; the per-call cost cap still applies to the reply.
- The "usual size" alert needs a few earlier calls from the agent to compare against.
- A leash caps single calls. Your daily and per-task budgets still apply on top.
