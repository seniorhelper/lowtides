# Accounts (optional sign-in)

Lowtides works without signing in: every visitor gets a private demo that lives in memory. Accounts add one thing: a shopper or a store can sign in and keep **their own dashboard** (rules, Passes, store policy, receipts, test purchases) across visits and devices.

Accounts are **off** unless all four settings are present. With them off, or with the sign-in service unreachable, the visitor demo behaves exactly as before.

| Setting | What it is |
|---|---|
| `ACCOUNTS_ENABLED` | `1` to turn accounts on |
| `SUPABASE_URL` | the project URL, `https://<project>.supabase.co` |
| `SUPABASE_ANON_KEY` | the project's public (anon) key, used only for sign-up and sign-in calls made by this server |
| `SUPABASE_SERVICE_KEY` | the project's service-role key. **Server only.** It is never sent to a browser and must never be committed |

Also set `LOWTIDES_DEMO_SECRET` (any long random text) so sign-in cookies stay valid across restarts.

## How it works

1. `POST /api/account/signup` or `/api/account/signin` (`{"email", "password", "role": "shopper"|"store"}`). The server passes the email and password to Supabase Auth over HTTPS and stores neither. Supabase keeps the password hash.
2. On success the browser's signed, HttpOnly `lt_demo` cookie is replaced with one tied to the account (`acct-<user id>`). The cookie is signed by this server, so it cannot be forged.
3. The account's sandbox is saved as one row in `lowtides_accounts` a few seconds after every change, and loaded on the next sign-in. The server's Pass signing key is removed from the saved copy first.
4. `POST /api/account/signout` saves, then gives the browser a fresh visitor demo. `GET /api/account` says whether accounts are on and who is signed in.

Limits: 8 sign-in or sign-up tries per network address per 10 minutes (plus Supabase's own limits). If storage cannot be read at sign-in, the person gets a temporary working dashboard and the saved one is left untouched.

## One-time database setup

Run this once in the Supabase SQL editor:

```sql
create table if not exists public.lowtides_accounts (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text not null default '',
  role text not null default 'shopper' check (role in ('shopper', 'store')),
  snapshot text not null default '',
  updated_at timestamptz not null default now()
);
alter table public.lowtides_accounts enable row level security;
revoke all on public.lowtides_accounts from anon, authenticated;
```

Row Level Security is on with **no policies**, so the public key and signed-in browsers can read nothing from this table. Only this server, using the service-role key, reads and writes it, and it only ever touches the row of the account whose cookie it signed.

## Email confirmation

By default Supabase emails a confirmation link at sign-up, and the free tier sends only a few emails per hour. For a demo, turn off **Authentication → Sign In / Providers → Email → Confirm email** so a new account works at once. With confirmation left on, sign-up answers "Check your email for a confirmation link, then sign in."

## What is not included

Password reset and email change are not built into the Lowtides pages yet; they can be done from the Supabase dashboard. Deleting a user in Supabase deletes their saved dashboard (the row cascades).
