# Lowtides guard for the PayPal Agent Toolkit

Puts Lowtides in front of PayPal's official Agent Toolkit (`paypal-agent-toolkit` on PyPI). Any agent using the toolkit must get Lowtides' OK before a money-moving PayPal tool runs. Works with the toolkit's OpenAI Agents SDK, LangChain, CrewAI and Bedrock adapters. See [docs/PAYPAL-AGENT-TOOLKIT.md](../../docs/PAYPAL-AGENT-TOOLKIT.md) for the why.

## Quickstart

```
pip install -r requirements.txt                  # paypal-agent-toolkit==1.11.0 (Python 3.11+)
set PAYPAL_CLIENT_ID=... & set PAYPAL_CLIENT_SECRET=...   # sandbox app only
set LOWTIDES_URL=http://127.0.0.1:8787 & set LOWTIDES_AGENT_KEY=lt-...
python example.py --offline                      # no keys or network: see the three decisions
python example.py                                # live: sandbox PayPal + your running Lowtides
```

In your agent, add one line:

```python
from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
from lowtides_guard import guard_toolkit

toolkit = guard_toolkit(PayPalToolkit(client_id=..., secret=..., configuration=config))
agent = Agent(name="Buyer", tools=toolkit.get_tools())   # same tools, now governed
```

## Example run (`python example.py --offline`)

```
list_transactions  {}                           -> {"transaction_details": []}
pay_order          {"order_id": "ORDER-SMALL"}  -> {"id": "ORDER-SMALL", "status": "COMPLETED"}
pay_order          {"order_id": "ORDER-MEDIUM"} -> {"lowtides": {"decision": "needs_approval", "request_id": "sp_demo2", "message": "Waiting for owner approval in Lowtides (request sp_demo2). ...", "reasons": ["$35.00 is over the $20.00 auto-approve limit."]}}
pay_order          {"order_id": "ORDER-BIG"}    -> {"lowtides": {"decision": "denied", ..., "reasons": ["$900.00 is over the $50.00 per-purchase limit."]}}
brand_new_tool     {}                           -> {"lowtides": {"decision": "denied", ..., "reasons": ["brand_new_tool is not a PayPal tool Lowtides knows about yet, so it was blocked. ..."]}}
```

## What is guarded

| Tool | Treatment | Amount sent to Lowtides |
|---|---|---|
| `pay_order` (capture) | Guarded | Order total, read with `get_order_details` just before capture |
| `create_subscription` | Guarded | Highest fixed price in the plan x quantity |
| `accept_dispute_claim` | Guarded | Dispute amount from `get_dispute` |
| `create_refund` (TypeScript toolkit only today) | Guarded | `amount` argument |
| `get_*`, `list_*`, `show_*`, `search_*`, `generate_*` | Pass through | |
| Other writes that move no money (`create_order`, invoices, products, tracking, `cancel_subscription`) | Pass through | |
| Any tool name not in the lists | Blocked (fail closed) | |

If Lowtides is unreachable, the key is wrong, or the amount can't be found, the PayPal tool does not run.

## How it works

Every adapter in the Python toolkit calls `PayPalAPI.run(method, params)` when a tool fires. `guard_toolkit` swaps that one method on the toolkit's `PayPalAPI` instance, so tools built before or after guarding are all covered. Calling the same tool again with the same arguments re-checks the pending request (`GET /v1/spend/{id}`) instead of filing a new one.

The guard sends `"settle": "external"` with each request, meaning "PayPal's toolkit moves this money; record it, don't pay it out". Lowtides applies every rule, approval and kill-switch check, then records the purchase as `paid` with payout status `PAID_BY_CALLER` and skips its own Payouts step, so nothing is paid twice.

## Tests

`python -m unittest discover -s tests` from the repo root (`tests/test_toolkit_guard.py`, no network or keys). The guard itself is standard library only.
