---
description: Use when the user wants to authenticate or re-authenticate the MemHub plugin, or when memory capture is not working because of auth (e.g. "log in to memhub", "memhub login", "authenticate memhub", "memhub says I'm not authenticated", "my sessions aren't being saved", "capture stopped working", "re-auth memhub"). Provisions the plugin's own OAuth token — which is SEPARATE from the /mcp connector's login — and verifies it works.
argument-hint: [--status | --force]
allowed-tools: Bash
---

Authenticate this MemHub plugin install and confirm capture can actually run.

**The one thing to understand before answering any question here:** the plugin's
hooks do NOT use the `/mcp` connector's login. They share an Auth0 client, but
the tokens live in two different stores — Claude Code keeps the connector's in
its own credential store, while the hooks read
`~/.config/memhub-plugin/tokens-<host>.json`, which only a foreground plugin
script can write. So "I'm connected in `/mcp`" and "my sessions are being
captured" are independent facts, and a user can very reasonably have the first
without the second. Never tell someone their capture is fine because `/mcp`
shows connected.

Arguments: `$ARGUMENTS`

Run exactly one command and report what it says:

- no arguments → `uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/login.py"`
  Logs in if needed (opens a browser once), then verifies against the server.
- `--status` → append `--status`. Reports only, never opens a browser. Use this
  when the user is asking *whether* they are logged in.
- `--force` → append `--force`. Discards the cached token and redoes the browser
  flow. Use when a login exists but is broken or unrenewable.

The command opens a browser tab on the first run. Tell the user to expect it and
to complete the approval; it waits up to 5 minutes.

## What it actually provisions

A browser login is only the bootstrap. What the hooks end up using is a
**personal access key** (`mhk_…`) that this command mints for you with the token
the browser flow produced — one key per machine, scoped `memory:read` +
`memory:write`, expiring in 90 days, stored at
`~/.config/memhub-plugin/pak-<host>.json`.

That indirection is the point. A hook is a cold background process that can
never open a browser, so it cannot refresh an expiring OAuth token — which is
how per-turn capture once died silently for a day. A key is a static bearer with
none of that machinery.

Re-running is safe and cheap: a stored key that is still valid is reused without
touching the server at all. You hold at most five keys, so if minting reports the
cap, revoke one in the MemHub app and re-run.

## Reading the output

It prints `environment`, `mode`, `status`, then one of `credential` or
`access key`, and `renewal`. Relay them plainly.

- **`mode`** — which credential answered: `$MEMHUB_TOKEN`, a stored access key,
  or the browser flow. This is the line that tells you what is actually in use.
- **`credential`** — printed instead of `access key` when a still-valid stored
  key answered directly (the steady-state case on every run after the first,
  for up to 90 days) — no server round trip needed to confirm it. `renewal`
  beside it always reads `n/a — a key does not refresh`; that is expected, not
  a warning — `/memhub:login` mints a fresh one once this one lapses.
- **`access key`** — printed instead, on a run that went through the full
  OAuth flow (first-ever login, after `--force`, or once the stored key has
  expired): `created`, `reusing`, or `replaced orphaned key`. A `NOT created`
  here is not a failed login: OAuth still verified and capture works today,
  but it is back on the short-lived credential, so say so.

- **`environment`** — say which one out loud. `production` and `staging` are
  separate tenants with separate logins, so authenticating one does nothing for
  the other. If the user expected the other environment, the cause is which
  plugin is active (`memhub` vs `memhub-staging`), not this command.
- **`status: OK`** — capture is authenticated and working. Say so and stop.
- **`status: NOT LOGGED IN`** — only `--status` produces this. Offer to run
  `/memhub:login` without arguments to fix it.
- **`renewal: NONE`** — this is the important one and it is easy to skim past.
  The login WORKS but cannot renew itself, so it will expire (24h) and capture
  will go silent with no further warning. Do not report this as a clean success.
  Surface the fix the command prints: enable *Allow Offline Access* on that
  environment's API in Auth0 so the grant includes `offline_access`.

## After a successful first login

If this was a first-time setup, mention that `/memhub:onboard` connects the repo
to its team brain — without it capture still runs, but everything it saves lands
in personal memory instead of the repo's room. Do not run it unprompted.
