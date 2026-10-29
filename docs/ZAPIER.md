# Alerts to Zapier, Make or any webhook

Lowtides can tell you, the moment it happens, when an agent's purchase is waiting for you, when money moves, and when an agent is stopped. It sends a signed JSON event to any webhook URL you save. The most common use: a Zapier "Catch Hook" that texts, emails or Slacks you, so you can approve a purchase from your phone.

Webhooks never slow your agents down. Lowtides decides first, puts the event on a queue, and sends it in the background. A dead or slow webhook is retried with backoff (after about 2 s, 10 s and 60 s) and every attempt is logged, but no decision ever waits for it.

## Events

| Event | When | Useful fields in `data` |
|---|---|---|
| `spend.needs_approval` | A purchase is waiting for you | `agent_name`, `amount`, `merchant`, `item`, `reason`, `reasons`, `flags`, `id`, `approve_url` |
| `spend.paid` | A purchase was paid (PayPal Payouts, or simulated) | `amount`, `merchant`, `payout_status`, `payout_batch_id`, `payout_proof` |
| `spend.denied` | The rules blocked a purchase, or a person denied it | `amount`, `merchant`, `reasons`, `decided_by` |
| `agent.killed` | Someone pressed KILL on one agent | `agent_name`, `denied`, `holds_voided`, `payouts_cancelled` |
| `stop_all` | STOP ALL was pressed (by you or the outside spend guard) | `by`, `denied`, `holds_voided`, `payouts_cancelled` |
| `loop_guard.paused` | The loop guard paused an agent that kept repeating itself | `agent_name`, `repeats` |
| `test.ping` | You pressed Test | `message` |

Every event looks like this:

```json
{
  "id": "evt_9c1f0a2b3d4e5f60",
  "type": "spend.needs_approval",
  "created": 1793301188,
  "description": "A purchase is waiting for a person to approve or deny it.",
  "dashboard": "https://lowtides.ai",
  "data": {
    "id": "sp_3f9a1c0b7d2e4a61",
    "agent_name": "Research bot",
    "amount": 45.0,
    "merchant": "Acme Data",
    "item": "Full company database export",
    "reason": "Bigger list for next week",
    "reasons": ["$45.00 is over the $25.00 auto-approve limit."],
    "flags": [],
    "status": "pending",
    "approve_url": "https://lowtides.ai/#approvals"
  }
}
```

The same event keeps its `id` across retries, so a receiver can ignore duplicates. Headers sent with it: `X-Lowtides-Event` (the type), `X-Lowtides-Delivery` (event id plus attempt number) and `X-Lowtides-Signature`.

## Text yourself when a purchase needs approval (Zapier)

Webhooks by Zapier is available on Zapier's paid plans (its free trial includes it).

1. In Zapier, click **Create > Zaps**.
2. Trigger: choose **Webhooks by Zapier**, event **Catch Hook**. Continue. Zapier shows a URL like `https://hooks.zapier.com/hooks/catch/123456/abcdef/`. Copy it.
3. In Lowtides, open **Settings > Alerts to Zapier / webhooks**. Paste the URL, give it a label like "Text my phone", leave **Only approvals and stops** ticked, and click **Add webhook**. Lowtides shows the signing secret once; save it if you want to check signatures later.
4. Click **Test** next to the webhook. Lowtides sends a `test.ping`.
5. Back in Zapier, click **Test trigger**. You should see the `test.ping` request. To get real fields to map, make an agent ask for a purchase over its approval line (or run the Postman collection, folder 2), then test the trigger again and pick that request.
6. Add a **Filter** step: continue only if `Type` exactly matches `spend.needs_approval`. (Skip this if you want every event.)
7. Action: pick how you want to hear about it:
   - **SMS by Zapier** (send to your own phone), or
   - **Gmail / Email by Zapier**, or
   - **Slack: Send Direct Message**.
8. Message text, using the fields Zapier found:
   ```
   {{Data Agent Name}} wants {{Data Amount}} at {{Data Merchant}} for {{Data Item}}.
   Why it's waiting: {{Data Reasons}}
   Approve or deny: {{Data Approve Url}}
   ```
9. Publish the Zap.

Make (Integromat) works the same way: use a **Webhooks > Custom webhook** trigger and paste its URL into Lowtides.

## Check the signature (optional)

`X-Lowtides-Signature` is `t=<unix time>,v1=<hex HMAC-SHA256>`, computed over `"<t>.<raw request body>"` with your webhook's secret. Reject the event if it doesn't match or if `t` is more than 5 minutes old. In Python:

```python
import hashlib, hmac, time

def verify(secret, raw_body, header, tolerance=300):
    parts = dict(p.split("=", 1) for p in header.split(","))
    t = int(parts["t"])
    if abs(time.time() - t) > tolerance:
        return False
    mac = hmac.new(secret.encode(), b"%d." % t + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, parts["v1"])
```

`lowtides.notify.verify()` does the same thing. A Zapier Catch Hook can't check signatures itself; treat the Catch Hook URL like a password and keep it private.

## Settings API

These are dashboard routes (admin password, same-site only), also in `docs/openapi.yaml` and the Postman collection:

- `GET /api/webhooks` lists webhooks (secrets hidden), the last 50 deliveries, and the event types
- `POST /api/webhooks` with `{"url": "...", "label": "...", "events": ["spend.needs_approval"]}` (empty `events` means all) returns the webhook and its `secret`, once
- `POST /api/webhooks/{id}/test` sends a signed `test.ping` now and returns `{"ok", "status", "error"}`
- `DELETE /api/webhooks/{id}`

On the shared public demo, webhooks may only point at Zapier, Make, Pipedream, Slack or webhook.site over https, at most 3 can be saved (Reset demo clears them, along with the delivery log), and saved URLs are shown shortened, because strangers share the demo. Your own copy accepts any public URL.

Everywhere, Lowtides never follows a redirect (a 3xx answer is logged as a failed delivery: use the final URL), and it refuses to send to a host that resolves to a private, loopback, link-local, multicast or reserved address, checked at send time and pinned for the connection so DNS rebinding can't get around it. If your own copy really needs a webhook on its own network (say, n8n on your LAN), set `LOWTIDES_WEBHOOK_ALLOW_PRIVATE=1`. The public demo ignores that setting.
