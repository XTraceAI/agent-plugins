---
description: Use when the user wants to authenticate or re-authenticate the MemHub plugin, or when memory capture is not working because of auth (e.g. "log in to memhub", "memhub login", "authenticate memhub", "memhub says I'm not authenticated", "my sessions aren't being saved", "capture stopped working", "re-auth memhub"). Provisions the plugin's own access key — which on Claude Code also authenticates the memhub MCP tools, but is separate from any /mcp connector login — and verifies it works.
argument-hint: [--status | --force] [--host cursor|claude-code|codex]
allowed-tools: Bash
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Authenticate this MemHub plugin install and confirm capture can actually run.

**The one thing to understand before answering any question here:** the plugin's
hooks do NOT use the `/mcp` connector's login. They share an Auth0 client, but
the credentials live in different stores — Claude Code keeps the connector's in
its own credential store, while the hooks resolve theirs in this order:
`$MEMHUB_TOKEN`, then the **personal access key** (`mhk_…`) at
`~/.config/memhub-plugin/pak-<backend-host>.json` (the normal case — a static
bearer that `login.py` mints, because a cold background hook can never open a
browser to refresh a token), then the OAuth token cache at
`~/.config/memhub-plugin/tokens-<backend-host>.json`. `<backend-host>` is the
MemHub API host (production and staging get separate files), not the coding
agent. Only a foreground plugin script
can write those files. So "I'm connected in `/mcp`" and "my sessions are being
captured" are independent facts, and a user can very reasonably have the first
without the second. Never tell someone their capture is fine because `/mcp`
shows connected.

The reverse direction is covered on **Claude Code**: the `memhub` MCP server
runs `scripts/mcp_headers.py` as its `headersHelper`, which hands the server
this same access key. So after this login the model's memhub tools work too,
with no separate `/mcp` login. Cursor and Codex do not run that helper; there
the `/mcp` sign-in is still what their tools use.

Arguments: `$ARGUMENTS`

Run exactly one command and report what it says:

- no arguments → `uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/login.py"`
  Logs in if needed (opens a browser once), then verifies against the server.
- `--status` → append `--status`. Reports only, never opens a browser. Use this
  when the user is asking *whether* they are logged in.
- `--force` → append `--force`. Discards the cached token and redoes the browser
  flow. Use when a login exists but is broken or unrenewable.

Append `--host cursor`, `--host claude-code`, or `--host codex` using the
coding host you are actually running in. This is an explicit integration value,
not a guess from the user's browser or user-agent. If the host is unknown, omit
`--host`; the result links to a host chooser. Never pass an arbitrary URL.

The command opens a browser tab on the first run. Tell the user to expect it and
to complete the approval; it waits up to 5 minutes.

## What it actually provisions

A browser login is only the bootstrap. What the hooks end up using is a
**personal access key** (`mhk_…`) that this command mints for you with the token
the browser flow produced — one key per machine, labelled
`claude-code-<hostname>` whichever coding agent ran login (so Codex and Cursor
on the same machine share it), scoped `memory:read` + `memory:write`, expiring
in 90 days, stored at `~/.config/memhub-plugin/pak-<backend-host>.json`.

That indirection is the point. A hook is a cold background process that can
never open a browser, so it cannot refresh an expiring OAuth token — which is
how per-turn capture once died silently for a day. A key is a static bearer with
none of that machinery.

Re-running is safe and cheap: a stored key that is still valid is reused
without minting another (it is still checked against the server). You hold at
most five unexpired keys, so if minting reports the cap, revoke one in the
MemHub app and re-run. Minting also needs the org's subscription to be active
with API access; a billing refusal comes back verbatim in `NOT created (…)`.

## Reading the output

It prints `environment`, `mode`, `status`, then one of `credential` or
`access key`, and `renewal`. Relay them plainly.

- **`mode`** — which credential answered: `$MEMHUB_TOKEN`, a stored access key,
  or the browser flow. This is the line that tells you what is actually in use.
- **`credential`** — printed instead of `access key` when a still-valid stored
  key answered directly (the steady-state case on every run after the first,
  for up to 90 days); `status` above it still proves it against the server. `renewal`
  beside it always reads `n/a — a key does not refresh`; that is expected, not
  a warning — `/memhub:login` mints a fresh one once this one lapses.
- **`access key`** — printed instead, on a run that went through the full
  OAuth flow (first-ever login, after `--force`, or once the stored key has
  expired): `created`, `reusing`, or `replaced orphaned key`. A `NOT created`
  here is not a failed login: OAuth still verified and capture works today,
  but it is back on the short-lived credential, so say so. The exception is
  `NOT created — this account has no MemHub team workspace yet`: see below.

- **`environment`** — say which one out loud. `production` and `staging` are
  separate tenants with separate logins, so authenticating one does nothing for
  the other. If the user expected the other environment, the cause is which
  plugin is active (`memhub` vs `memhub-staging`), not this command.
- **`status: OK`** — the capture credential is available. This does not prove the host loaded or approved its hooks; on Codex, setup and hook review are still required.
- **`no MemHub team workspace yet`** — the token checked out, but MemHub has no
  account or workspace for this identity: it is created only when the person
  first signs in to the MemHub web app, and that sign-in is refused for an
  unverified email or (where the work-email gate is on) a free-mail address.
  Every tool call would be refused too, so the command exits non-zero. Relay
  the printed `fix`: sign in to the web app once with a verified work email,
  then re-run `/memhub:login`. Re-running login alone will not help.
- **`status: NOT LOGGED IN`** — only `--status` produces this. Offer to run
  `/memhub:login` without arguments to fix it.
- **`renewal: NONE`** — this is the important one and it is easy to skim past.
  The login WORKS but cannot renew itself, so it will expire (24h) and capture
  will go silent with no further warning. Do not report this as a clean success.
  Surface the fix the command prints: enable *Allow Offline Access* on that
  environment's API in Auth0 so the grant includes `offline_access`.

## After a successful login: the MCP tools in this session

Claude Code reads the key when it *connects* the `memhub` server, so a session
that was already running still shows it as needing authentication. Tell the user
to open `/mcp`, select `memhub`, and choose **Reconnect** — that picks the key
up immediately. A new session also works, except that Claude Code remembers a
"needs authentication" result for about 15 minutes, so one started soon after a
session without a key can still skip it; Reconnect fixes that too. Do not send
them through the `/mcp` **Authenticate** browser flow — it is not needed.

`$MEMHUB_TOKEN` authenticates the hooks and scripts but not the MCP tools:
Claude Code strips credential variables from a plugin's helper. A headless setup
that relies on it still needs a stored key for the tools.

## After a successful first login

If this was a first-time setup, mention that `/memhub:onboard` creates and
seeds the repo's team brain. Once the host has loaded and approved the capture hooks, they route to a
brain named `Repo: <org>/<name>` on their own if one exists — but a repo with
no such brain yet saves to personal memory until onboard creates it. Do not
run it unprompted.

The `next steps` line links to the guide for the initiating host. After a new
browser login, the localhost callback redirects there only after the foreground
command completes successfully. A code arriving is not proof of authentication.
Failures stay on a recovery page and in the terminal. Never send codes, tokens,
or OAuth state to a guide, analytics service, or remote URL.

Native MCP login belongs to the host (`/mcp`, Cursor's connector UI, or
`codex mcp login`), so this helper does not replace its callback or promise to
redirect it. On Claude Code there is no native login to wait for: the key this
command mints already authenticates the tools (see above). After native login
where a host still needs one, offer the appropriate public guide:
`https://mem.xtrace.ai/plugin/cursor`, `/plugin/claude-code`, or `/plugin/codex`.
For staging use `https://staging.mem.xtrace.ai` with the same path. Confirm the
plugin environment before choosing. Direct guide visits do not certify login.
