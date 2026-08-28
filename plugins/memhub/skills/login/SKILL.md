---
description: Use when the user wants to authenticate or re-authenticate the MemHub plugin, or when memory capture is not working because of auth (e.g. "log in to memhub", "memhub login", "authenticate memhub", "memhub says I'm not authenticated", "my sessions aren't being saved", "capture stopped working", "re-auth memhub"). Provisions the one credential the plugin's MCP tools and capture hooks share, and verifies it works.
argument-hint: [--status | --force]
allowed-tools: Bash
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Authenticate this MemHub plugin install and confirm capture can actually run.

**The one thing to understand before answering any question here:** this is
the plugin's only login. The `memhub` server in `/mcp` is a local proxy
(`scripts/mcp_proxy.py`) and the capture hooks are background scripts, and
both resolve the same credential in the same order: `$MEMHUB_TOKEN`, then the
**personal access key** (`mhk_…`) at `~/.config/memhub-plugin/pak-<host>.json`
(the normal case — a static bearer that `login.py` mints, because a cold
background hook can never open a browser to refresh a token), then the OAuth
token cache at `~/.config/memhub-plugin/tokens-<host>.json`. Only a foreground
plugin script can write those files, which is why this command exists. Until it
has run, `/mcp` shows the `memhub` server failing with "not logged in" and
nothing is captured; after it, both work.

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

If this was a first-time setup, mention that `/memhub:onboard` creates and
seeds the repo's team brain. Capture already runs, and the hooks route to a
brain named `Repo: <org>/<name>` on their own if one exists — but a repo with
no such brain yet saves to personal memory until onboard creates it. Do not
run it unprompted.
