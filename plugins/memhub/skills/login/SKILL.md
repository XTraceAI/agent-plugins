---
description: Use when the user wants to sign in to MemHub or sign in again, or MemHub says they are not signed in (e.g. "log in to memhub", "memhub login", "sign in to memhub", "sign in again", "memhub says I'm not signed in", "re-auth memhub"). Signs this machine in, so sessions are saved and team rules apply, and checks that it works.
argument-hint: "[--status | --force] [--host cursor|claude-code|codex]"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/login.py" *)'
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
an explicit token (the `memhub_token` plugin option, else `$MEMHUB_TOKEN`),
then the **personal access key** (`mhk_…`) at
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

- no arguments → `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/login.py"`
  Signs in if needed (a code to approve in the browser), then verifies against the server.
- `--status` → append `--status`. Reports only, never opens a browser. Use this
  when the user is asking *whether* they are logged in.
- `--force` → append `--force`. Sets the saved sign-in aside and signs in again.
  Use when a sign-in exists but is broken or unrenewable.

Append `--host cursor`, `--host claude-code`, or `--host codex` using the
coding host you are actually running in. This is an explicit integration value,
not a guess from the user's browser or user-agent. If the host is unknown, omit
`--host`; the result links to a host chooser. Never pass an arbitrary URL.

On the first run the command prints a sign-in code and a link, and tries to open
that link in a browser tab. Before it finishes, tell the user to open the link
(the tab may not appear), check that the page shows the same code, and approve.
It waits up to 5 minutes. This is a device-code sign-in: nothing listens on a
local port, so a busy callback port, a container or an SSH session does not
break it. Only a server that does not offer device codes falls back to the
browser callback.

## What it actually provisions

The sign-in is only the bootstrap. What the hooks end up using is a
**personal access key** (`mhk_…`) that this command mints for you with the token
the sign-in produced — one key per machine, labelled
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

## Answering the user

Lead with ONE plain line, then stop unless something is wrong:

- Success: "Signed in to MemHub <environment>. Sessions on this machine are saved from now on, and MemHub's tools are ready from your next session."
- Failure: what went wrong in one sentence, and the one thing to do next.

Say the environment (production or staging) when it is not the one the user
would assume. Do NOT relay key labels, `mhk_`, scopes, expiry maths, "orphaned
key", `mode` or `renewal` lines, or file paths: they are for diagnosing, and
reading them out is what made sign-in look like three different logins. Give
them only when the user asks, or when one of the cases below applies.

## Reading the output (for diagnosing)

It prints `environment`, `mode`, `status`, then one of `credential` or
`access key`, and `renewal`.

- **`mode`** — which credential answered: an explicit token (named), a stored
  access key, or a sign-in (device code; browser callback as fallback).
- **`credential`** — a still-valid stored key answered directly (the steady
  state for up to 90 days); `renewal` beside it always reads `n/a`, which is
  expected, not a warning.
- **`access key`** — on a run that signed in: `created`, `reusing`, or
  `replaced orphaned key` (all success). A `NOT created` here is not a failed
  sign-in: it works today on the short-lived credential, so say capture may
  stop within a day and to run login again. The exception is
  `NOT created — this account has no MemHub team workspace yet`: see below.
- **`environment`** — `production` and `staging` are separate sign-ins. If the
  user expected the other one, the cause is which plugin is active (`memhub`
  vs `memhub-staging`), not this command.
- **`status: OK`** — the credential works. On Codex, hook review is still
  required before capture runs (`/memhub:onboard`).
- **`no MemHub team workspace yet`** — MemHub has no account or workspace for
  this identity yet; it is created when the person first signs in to the
  MemHub web app (a verified work email). Relay the printed `fix`: sign in to
  the web app once, then run login again. Running login alone will not help.
- **`status: NOT LOGGED IN`** — only `--status` prints this. Offer to run login
  without arguments.
- **`renewal: NONE`** — signed in but cannot renew, so it stops within a day.
  Do not call this a clean success; relay the fix it prints.

## The tools in a session that was already open

Claude Code connects the `memhub` tools once, when a session starts, so in a
session that started before sign-in they stay unavailable. Say they are ready
from the next session. Only if the user needs them in this one: `/mcp` →
`memhub` → **Reconnect** (never the **Authenticate** browser flow, which is not
needed). Cursor and Codex sign their tools in separately (Codex:
`codex mcp login memhub`).

`$MEMHUB_TOKEN` authenticates the hooks and scripts but not the MCP tools:
Claude Code strips credential variables from a plugin's helper. A headless setup
that relies on it still needs a stored key for the tools.

## After a successful first login

If this was a first-time setup, mention that `/memhub:onboard` finishes the
setup: this machine's hooks (on Codex, the hook approval), and the repo's
brain, docs and starter rules. Once the host has loaded and approved the capture hooks, sessions are
captured into the user's personal memory — never into a brain — while saved
artifacts and specs go to the repo's brain once onboard creates it. Do not
run it unprompted.

The `next steps` line links to the guide for the initiating host. On the
browser-callback fallback, the localhost callback redirects there only after the
foreground command completes successfully. A code arriving is not proof of
authentication.
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
