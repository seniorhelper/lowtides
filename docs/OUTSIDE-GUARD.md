# Outside spend guard

The outside spend guard watches the bill your AI provider sends you. It does not depend on calls going through Lowtides. If something else spends your money, the guard sees it and can switch off the keys you listed.

## Why it exists

Lowtides' budgets live in its proxy. They only see calls that pass through it.

A script that called the Anthropic API directly, bypassing Lowtides, ran up about $400 overnight. The proxy budgets could not see it, because the script never touched the proxy.

The fix is to watch the provider's own cost report. That report covers everything billed to your account, whatever sent the call.

## What it catches

Any spend billed to your provider organization, from any tool or script. That includes:

- Scripts that call the API directly.
- Other apps and agents that use your keys.
- Anything else on the same organization.

It works for Anthropic and OpenAI. It does not matter whether the call went through Lowtides.

## How it works

1. Every N minutes (default 5, never faster than once a minute) Lowtides reads each enabled provider's cost report.
2. It compares today's spend and this month's spend to your caps.
3. You can set a daily cap and a monthly cap for each provider, and a combined daily and monthly cap across providers. A cap of 0 means no cap.
4. At the warn percentage (default 80%) it raises an alert and writes an event.
5. At the cap it acts. It:
   - switches off only the keys you listed,
   - writes a hash-chained audit entry,
   - shows a red banner on the dashboard,
   - presses STOP ALL for Lowtides' own agents (you can turn this part off with `stop_agents`).

If the banner is cleared and spend is still over the cap, the guard trips again. Raise the cap to resume.

### What "switch off" means

| Provider | What happens to a listed key | Can you undo it? |
|---|---|---|
| Anthropic | The key is set to "inactive". | Yes. You can turn it back on in the Anthropic Console. |
| OpenAI | The key is **deleted**. OpenAI has no way to disable a key. | **No. It is permanent.** You will need to make new keys. |

Because OpenAI keys are deleted, think about which ones you list. A good habit is to give each risky script its own OpenAI project key, then list only that key. Your main keys stay safe.

If a key cannot be switched off, the banner and the audit entry say so. Turn it off by hand right away.

## Limits

This caps the damage. It cannot make it zero.

- **Billing data lags.** Anthropic says usage and cost data usually appears within 5 minutes, but sometimes takes longer. OpenAI publishes no figure. Lowtides assumes about 15 minutes for OpenAI, which is our own guess.
- **Cost reports are daily buckets in UTC.** "Today" means the UTC day, not your local day.
- **Anthropic's cost report leaves out Priority Tier costs.** Spend on that tier will not show up.
- **Only keys you list are touched.** An unlisted key keeps working, even while spend is over the cap.
- **It needs an organization admin key.** An ordinary API key will not work.
- **The Anthropic Admin API is not available for individual accounts.** You need an organization.
- **The server has to be running.** If Lowtides is off, nothing is watching.

Set your caps below what you can afford to lose. The gap between real spend and the number the guard sees is money it cannot stop.

## Turn it on

### 1. Set the admin keys on the server

Put the keys in environment variables on the machine that runs Lowtides:

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin...
export OPENAI_ADMIN_KEY=sk-admin-...
```

Set only the ones you use. These keys live in the server's environment only. They are never read from the config file or the dashboard, and never stored, logged or shown. The dashboard cannot point the guard at a different key or host.

### 2. Choose your settings

Use the **Outside watch** panel in the dashboard, or the `outside` block of `config.json`. Both control the same settings. Here is an example with the default values filled in for Anthropic and OpenAI:

```json
{
  "outside": {
    "enabled": true,
    "poll_minutes": 5,
    "warn_percent": 80,
    "stop_agents": true,
    "combined": { "daily_cap": 50, "monthly_cap": 400 },
    "providers": {
      "anthropic": {
        "enabled": true,
        "daily_cap": 30,
        "monthly_cap": 250,
        "protect_keys": ["apikey_01AbCdEfGh", "apikey_01XyZxYzXy"]
      },
      "openai": {
        "enabled": true,
        "daily_cap": 20,
        "monthly_cap": 150,
        "protect_keys": ["proj_abc/key_xyz"]
      }
    }
  }
}
```

Out of the box, `enabled` is false, every cap is 0 and no keys are listed. The guard does nothing until you turn it on.

Caps are in US dollars. List key IDs, never the secret keys. Lowtides rejects anything that starts with `sk-`.

### 3. Find your key IDs

- **Anthropic:** the ID looks like `apikey_01...`. Find it on the API keys page of the Anthropic Console, or list keys with the Admin API (`GET /v1/organizations/api_keys`). The `id` field is what you list.
- **OpenAI:** list each key as `project_id/key_id`, for example `proj_abc/key_xyz`. Find the project ID in the OpenAI dashboard's project settings. List a project's keys with the API (`GET /v1/organization/projects/{project_id}/api_keys`). The `id` field is the key ID.

Check each ID before you rely on it. A wrong ID fails at the cap, when it is too late.

## Public demo

The public demo at https://lowtides.ai uses a simulated provider with fake data. It never calls a real admin API and holds no real keys.

Press **Simulate a runaway script** to see the guard work: the fake spend jumps past the cap, the fake key is switched off, the banner goes red, and the audit log gets an entry.

## Sources

Fetched 2026-10-29. Items marked UNVERIFIED were not confirmed in the provider's documentation.

Anthropic:
- Usage and Cost API guide (freshness, polling limits, Priority Tier note): https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Cost report reference: https://platform.claude.com/docs/en/api/beta/organization/cost_report/retrieve
- Admin API (admin keys, no individual accounts, keys can only be read, renamed or have status changed): https://platform.claude.com/docs/en/manage-claude/admin-api
- List API keys: https://platform.claude.com/docs/en/api/beta/organization/api_keys/list
- Update API key (status "inactive"): https://platform.claude.com/docs/en/api/beta/organization/api_keys/update
- UNVERIFIED: that an inactive key can be turned back on. The status values suggest it, and the Console is where we expect to do it, but the docs do not say so outright.

OpenAI:
- Costs API: https://developers.openai.com/api/reference/resources/organization/subresources/usage
- List project API keys: https://developers.openai.com/api/reference/resources/organization/subresources/projects/subresources/api_keys/methods/list
- Delete project API key: https://developers.openai.com/api/reference/resources/organization/subresources/projects/subresources/api_keys/methods/delete
- UNVERIFIED: that OpenAI has no way to disable a key. We found only list, retrieve and delete.
- UNVERIFIED: how fresh OpenAI cost data is. No figure is published. The 15 minutes in Lowtides is our own guess.
- UNVERIFIED: that OpenAI cost amounts are in dollars. The page does not say, but dollars is standard.
- UNVERIFIED: the exact permissions an OpenAI admin key needs.
