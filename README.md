# MemHub for Claude Code, Codex, and Cursor

MemHub gives coding agents shared team memory: interactive MCP tools for
searching and saving knowledge, automatic session capture, situated directive
recall, and workflows for artifacts, specs, handoffs, rules, and PR review.
The same `memhub` plugin supports Claude Code, OpenAI Codex, and Cursor through
each host's native plugin system.

## What's in here

This repository publishes the `memhub` plugin to all three hosts. It also
contains the Claude Code-only `fleet` plugin for coordinating parallel agents.

```
.agents/plugins/marketplace.json    # Codex and Cursor marketplace
.claude-plugin/marketplace.json     # Claude Code marketplace
plugins/memhub/                     # production plugin installed by every host
├── .claude-plugin/plugin.json      # Claude Code manifest
├── .codex-plugin/plugin.json       # Codex manifest
├── .cursor-plugin/plugin.json      # Cursor manifest
├── plugin.json                     # Agent Plugins manifest
├── .mcp.json                       # Claude Code MCP configuration
├── mcp.json                        # Codex/Cursor MCP configuration
├── hooks/                          # host-specific capture and recall hooks
├── scripts/                        # shared readers, capture, auth, and setup code
└── skills/                         # model-invoked MemHub workflows
plugins/fleet/
└── ...                             # optional Claude Code fleet coordination
codex/                              # legacy forwarding shims and Codex reference guide
```

## Install

There are two separate authentications on every host:

1. the **MCP connector login** enables interactive tools such as
   `search_memory` and `save_artifact`;
2. **Log in to MemHub** provisions the credential used by background capture
   hooks.

Completing one does not complete the other.

### Claude Code

```text
/plugin marketplace add XTraceAI/agent-plugins
/plugin install memhub@memhub
/reload-plugins
```

Open `/mcp`, select `memhub`, choose **Authenticate**, and approve in the
browser. Then run `/memhub:login` for capture and `/memhub:onboard` from the
repository whose team brain you want to create or select.

### OpenAI Codex

```bash
codex plugin marketplace add XTraceAI/agent-plugins
codex plugin add memhub@xtrace-plugins
```

Restart Codex after installation. Codex normally starts MCP authentication
during installation; if it still reports that `memhub` is not logged in, run:

```bash
codex mcp login memhub --oauth-client-registration cimd
```

Do **not** manually add a second `memhub` server or OAuth client in
`~/.codex/config.toml`. A global server with the same name shadows the plugin
server and can force an incompatible static OAuth client. See
[`codex/README.md`](codex/README.md) for the complete setup, hook approval, and
recovery steps.

In a new Codex task, ask it to **Log in to MemHub**, then **Set up MemHub**.
Restart, open `/hooks`, and trust only MemHub's `PreToolUse`, `PostToolUse`, and
`Stop` handlers from `~/.codex/hooks.json`. Finally ask it to **Onboard MemHub
for this repo**.

### Cursor

```bash
cursor-agent plugin marketplace add https://github.com/XTraceAI/agent-plugins
```

Open **Customize**, find **MemHub** in the XTrace marketplace, and select
**Add**. In the MemHub plugin details, authenticate the MCP entry when it says
it needs attention. Then ask Cursor Agent to **Log in to MemHub** for capture
and **Onboard MemHub for this repo**.

### Capture credential

The foreground login skill opens a browser once and mints a 90-day personal
access key (`mhk_…`) under `~/.config/memhub-plugin/`. Background hooks use
that key because they cannot use the host application's MCP credential store
or open a browser to refresh an OAuth token. Skipping this step leaves capture
unauthenticated even when interactive MCP tools work.

The Claude Code marketplace pins `memhub` to a released tag. Codex and Cursor
install from the Agent Plugins marketplace snapshot and refresh that snapshot
through their marketplace update flow.

> **Working on MemHub itself?** There's a staging build that points at the
> staging backend. It is not part of this marketplace — see
> [CONTRIBUTING.md](CONTRIBUTING.md).

## How capture works

Claude Code and Cursor load their bundled hooks natively. Codex currently uses
the user-level compatibility bridge installed by the `setup` skill. All hosts
normalize their native transcript format before sending it to the same
watermark-based ingestion endpoint; native usage counters are included where
the host exposes them.

The detailed lifecycle below describes the Claude Code path. Codex and Cursor
use equivalent host-specific readers and flushers under
`plugins/memhub/scripts/`.

Capture runs on independent paths that all feed one server-side watermark
(keyed on `conversation_id` = `session_id`), so re-sending never double-saves:

1. **Per-turn, via the `Stop` hook (primary).** After every assistant turn,
   `flush_turn.py` ships only the transcript bytes written since the last
   successful flush — a byte cursor, not a re-send — to `import_conversation`
   with `flush: "auto"`: durable on arrival, but batched into episodes rather
   than extracted turn-by-turn (a 2–5 event fragment would shred episode
   boundaries). A cheap prefilter (`turn_flush_prefilter.py`, plain `python3`,
   no `uv`) skips the expensive spawn when there's nothing new to send, a
   flush is already in flight, or capture is switched off
   (`MEMHUB_TURN_FLUSH=0`).
2. **`SessionEnd` (backstop).** Deliberately independent of the per-turn
   cursor — it re-sends the whole transcript-so-far and lets the server's
   watermark dedup, so it still captures a session whose per-turn path was
   dormant, unauthenticated, or failing all along.
3. **`PostToolUse` on commit/PR.** A precise prefilter (`flush_prefilter.py`)
   confirms the just-run Bash command actually performed `git commit`, `gh pr
   create`, or `gh pr merge` (not merely mentioned it) before flushing the
   transcript-so-far in the background. Commits are semantic work boundaries:
   flushing there makes memory available mid-session (parallel sessions see
   fresh decisions minutes later) and shapes episodes into work-unit
   narratives.
4. **Routing.** Whichever path fires, the server auto-detects the Claude Code
   shape and runs the **agentic** extraction path (tool-bearing events, the
   agent treated as a valid belief source). The session routes into the
   repo's own agent brain via a per-user cache at
   `~/.config/memhub-plugin/rooms.json` — resolved once by `/memhub:onboard`
   and read by every writer, capture included; until then, everything lands
   in personal memory instead of the repo's room.

All of the above authenticate with the plugin's own credential — separate
from `/mcp`, provisioned by `/memhub:login` (see Install) — because they run
as cold background processes that can never open a browser.
`SessionStart` also runs `capture_health.py`, a *synchronous* check (the
async flush hooks can't surface anything to the user) that reports via
`systemMessage` when the capture credential has expired or a recent flush
failed; silent on the healthy path so it doesn't become wallpaper.

### Directive recall

Independent of capture: `PreToolUse` (Edit/Write/NotebookEdit/Bash) and
`PostToolUse` (on failure) hooks call `recall_directives` — the concrete file
path or command about to run (or, on the reactive path, the failing output)
is checked against situated team lessons/procedures, and any hit is injected
as context *before* the agent acts (or right after a failure, when the error
often names the real cause better than the command line did). A client-side
precision gate drops directives whose triggers don't concretely match the
call — blocking generic filler tokens and the repo's own name, so an
over-broad trigger can't fire on ~every call — and each directive injects at
most once per session. On Bash, a prefilter (`directive_prefilter.py`) only
recalls for commands that mutate durable state (`git commit`, `rm`, package
installs, migrations, …); read-only commands (`grep`, `cat`, `git show`) skip
the round-trip entirely.

### Session orientation

Also independent of capture: a `SessionStart` hook (`brain_brief.py brief`)
names the repo's default agent brain before the first prompt. It closes a
read/write gap — writes already resolved the room, but `search_memory`
without an explicit `agent_brain_id` defaulted to personal memory regardless.
It's stdlib-only and makes no network call (everything comes from the cached
room and a local overview cache), and it injects the brain id — plus a
compiled overview, if one is cached — as context for the agent every
session. It only tells the *user*, via `systemMessage`, when the resolved
brain changes, so it doesn't become wallpaper; a repo with no cached room
(not yet onboarded) stays silent. A companion `Stop` hook (`brain_brief.py
refresh`, async) fetches `get_brain_overview` and writes the cache `brief`
reads, throttled to once per 6 hours since the digest moves on the order of
days, not turns.

## Skills

Eleven skills ship in `plugins/memhub/skills/` (the deprecated `commands/`
format is gone; invocation is unchanged). Each is both user-invocable as
`/memhub:<name>` and **model-invocable**: saying "save this spec to memhub" or
"what did we decide about X?" in plain language triggers the right skill.

- `/memhub:setup [--status | --remove]` — installs, checks, or removes the
  Codex user-hooks compatibility bridge; on Claude Code and Cursor it checks
  the native integration without writing Codex configuration.
- `/memhub:login [--status | --force]` — authenticates capture's own
  credential (see Install/How capture works): mints or verifies the personal
  access key the background hooks use, distinct from `/mcp`'s connector
  login. `--status` reports without opening a browser; `--force` discards the
  cached credential and redoes the browser flow.
- `/memhub:onboard [session-id-or-path]` — crosses the empty-brain cold
  start for a repo: resolves or creates its agent brain, caches the room so
  automatic capture routes there, seeds it from one real session, and proves
  proactive directive recall on the repo's own symbols before reporting an
  activation funnel.
- `/memhub:import-session <id-or-path> [title]` — terminal upload of a past
  session transcript; auto-chunks very large sessions. The ONLY skill that
  imports: live sessions are captured per turn, so importing is for backfill —
  sessions that predate capture, or ran while it was dormant. It imports under
  the session's own id, the same id capture uses, so a session is one
  conversation rather than two competing copies.
- `/memhub:save-artifact <file> [name]` — terminal upload of a file as an
  artifact. Both upload skills exist so the model never re-emits file or
  transcript content token by token — a helper script ships the bytes.
- `/memhub:search-memory <query>` — read-only recall over facts, episodes,
  artifacts, and documents, with tag / time filters. In a repo with a
  resolved agent brain (named by session orientation, above), searches that
  brain first, then repeats the same query without it and merges the
  results — widening to personal memory rather than replacing it, so neither
  side goes silently missing.
- `/memhub:handoff-session <teammate> [title]` — hand the current session to a
  teammate: creates an agent brain holding a composed handoff brief (goal,
  state, decisions, next steps, gotchas) and shares it read-only via
  `share_agent_brain`, alongside the repo room where per-turn capture already
  extracted the session. No re-import: the session's memory exists once, and
  the brief points into it.
- `/memhub:spec <init|revise|check|status>` — spec-driven development on team
  memory. Each repo gets **one shared agent brain** (`Repo: <org>/<name>`,
  derived from the git remote) holding ALL its specs alongside reviews, ADRs,
  and imported implementation sessions — share it once per teammate and every
  current and future spec is visible to them. Each spec is a **versioned
  artifact** in that room (every revision carries a rationale; versions are
  diffable via `diff_artifact_versions`), mirrored by a file in the repo
  (`docs/specs/<slug>.md`); a `spec:<slug>` tag picks it out of the shared
  room. `init` drafts/uploads and shares; `revise` versions with a required
  rationale and reports the diff; `check` detects the spec drifting under
  this session's work (local file vs. artifact lineage); `status` is the
  multiplayer view — repo overview with no topic, per-spec activity with one.
  Sharing is read-only, so the room's creator owns revisions; teammates
  propose spec changes through the normal repo/PR flow.
- `/memhub:create-rule` — turns a concrete failure, correction, or procedure
  into a situated Rulebook rule: a when-X-then-Y sentence plus a deterministic
  check (a bash/edit/output matcher, an ordering, or recall anchors), checked
  for conflicts against the book before anything is filed. Rules always
  **advise**, never block. Where the rule lands is the server's decision, not
  the skill's: at one confirmation turn you choose "file for review" or "put it
  in the book now", and only an org admin picking the second gets a live rule —
  everyone else's waits for a reviewer, and the reply says so.
- `/memhub:import-claude-md` — imports reusable instructions from a `CLAUDE.md`
  file into Rulebook without turning generic prose into noisy directives. It
  decomposes the doc here in the repo (so a rule can be scoped to real paths
  and its patterns checked against real commands), runs the same conflict pass
  over every candidate, and files each row against its `path@sha#heading` so a
  re-import replaces rules instead of duplicating them. Nothing imported ever
  goes live on its own — a person activates it in MemHub.
- `/memhub:pr-babysit [pr-number-or-url]` — usually **auto-armed**, not typed:
  a hook offers to start this as a self-paced loop right after `gh pr
  create` (see PR babysitting below). One pass polls the PR's review bots and
  CI, fixes real findings, and — once clean — saves the fixing process to the
  repo's agent brain.

## PR babysitting

A `PostToolUse` hook (`pr_babysit_trigger.py`) watches Bash calls for a
successful `gh pr create` — it only fires once the tool's own stdout actually
contains a PR URL, so a failed create stays silent — and injects context
telling the agent to start a self-paced `/loop` running `/memhub:pr-babysit
<url>`. A hook can't call an MCP tool or arm a loop itself; it only injects
the instruction, and the agent decides whether to follow it (a user who said
not to babysit PRs, in this session or in memory, is a signal to skip).

Each `/memhub:pr-babysit` pass resolves the PR and the repo's agent brain,
then collects **new** findings since the last pass: review comments from a
bot reviewer (login containing `cursor`/`bugbot` or `codex`/`chatgpt`) and any
failing required check. It triages each — bots are wrong often enough that "a
bot said so" isn't a reason to change code — fixes real findings on the PR's
branch (one commit per finding or coherent batch, never force-pushed), and
replies to false positives with a one-line rationale. A pass that pushed
nothing, found nothing new, and has given the bots a review window (an
existing bot review/comment on the head commit, or ~20 minutes since it was
pushed) is clean. The final clean pass composes a **PR review record** —
findings, verdicts, fix commits or rejection rationale, and any repo-specific
pattern worth remembering — and saves it as a versioned artifact
(`save_artifact`, stable `name` so a later babysit of the same PR supersedes
rather than competes) into the repo's room, then ends the loop. It never
imports the session transcript: per-turn capture already ships that into the
same room continuously, so babysit only adds the judgment call a transcript
doesn't record — which findings were real, which were rejected and why.

## Artifact-sync reminder

Agents keep memory current by **appending** new artifacts as conclusions
evolve, instead of **versioning** the canonical one. Retrieval is semantic, so
the co-existing versions compete — and a stale claim can rank ABOVE its own
correction. Measured 2026-07-20: an over-read "AppWorld ON tripled partial
progress" artifact scored 0.596 for the query "does memory help?" while its
correction ("within the noise floor") scored 0.466, so a fresh agent read the
wrong conclusion first.

`save_artifact` already supports supersession (reuse the `name`, or pass
`parent_id`). What was missing is a prompt to use it at the moment the code
moves. A `PostToolUse` hook on `Edit|MultiEdit|Write|NotebookEdit` matches the
edited file against the repo's **artifact map** and, on a hit, injects the
exact `save_artifact(...)` call that versions the linked artifact — debounced
to once per artifact per session.

The map is repo-local, at `.claude/artifact-map.json`, so links version with
the code:

```json
{"version": 1, "links": [
  {"glob": "app/retry.py|app/**/backoff.py",
   "brain_id": "<agent-brain-id>",
   "artifact_id": "<root-version-id>",
   "artifact_name": "Spec: Retry policy"}]}
```

Globs are repo-relative POSIX with `*` (stops at `/`), `**`, `{a,b}` braces,
and `|` alternatives. `/memhub:spec` writes and refreshes these links at
`init`/`revise` time via `scripts/artifact_map.py`, so **which files a spec
governs** is a byproduct of spec-driven development rather than a separate
chore — and `/memhub:spec check` uses the same links in reverse, reporting
mapped files that changed since the spec's last revision. To inspect or hand-
manage links: `python3 scripts/artifact_map.py list [--for <path>]`.

Hooks cannot call MCP tools, so this only **reminds** — the agent performs the
`save_artifact` itself. That is deliberate for a memory product: the version
bump stays visible and auditable instead of team memory being silently
rewritten on every keystroke. Missing or malformed map, no git root, unwritable
state → exit 0, no output; a reminder never blocks an edit.

## Fleet plugin

`plugins/fleet/` is a separate, local-only plugin for running **many Claude
Code agents in parallel git worktrees of one repo**. All worktrees share the
repo's common `.git` directory, so a single board file at
`$(git rev-parse --git-common-dir)/fleet-board.json` is visible to every
agent with no server and no auth. Hooks keep it current:

- **SessionStart** — registers the session (branch, worktree, session id),
  prunes stale/ghost entries, and injects a snapshot of the other active
  agents into context.
- **UserPromptSubmit** — heartbeats the entry, refreshes its one-line
  "working on" from your prompt, and injects only the *delta* of sibling
  changes since this agent last looked (joined / ended / committed /
  changed focus). No changes → no injection, no token cost.
- **PostToolUse** (git commits) — records the commit message and files
  touched on this agent's entry, so siblings get collision warnings before
  editing the same files.
- **SessionEnd** — marks the entry ended (siblings see it; pruned later).

For a human-facing view, `/fleet:status` (also triggered by "what's the
fleet doing?") pretty-prints the board: who's active where, what each agent
is working on, last commits with age, and any file overlaps between agents.

To *start* a fleet instead of assembling it by hand, `/fleet:start <task>`
decomposes the task into 2–4 independent workstreams (confirming the
split first), provisions a worktree + branch + kickoff brief per stream, and
launches a real session in each — interactive tabs (tmux/iTerm/Terminal) or
`--headless` detached runs. Launched sessions register on the board through
the normal hooks, so coordination from there is automatic.

Pairs with the memhub plugin: the board says *who is doing what right now*
(seconds, one line each); per-turn capture already lands every session's
history in MemHub, so an agent that needs the *why* behind a sibling's
change searches team memory with the session id from the board entry.
Each board entry costs ~1 short line of injected context; everything fails
soft (not a git repo / hook error → silent no-op).

## Notes & trade-offs

- **Auth is per-user, and split in two.** Nothing secret travels with the
  plugin. Each person authenticates the interactive MCP tools themselves via
  `/mcp`, and separately authenticates the background hooks via
  `/memhub:login` (see Install) — being connected in one says nothing about
  the other. Capture hooks talk to the MCP server directly (their own
  credential, their own connection), so they don't go through Claude Code's
  per-tool-call permission prompt the way a model-invoked MCP tool call does.
- **Cost.** Per-turn capture ships only the bytes written since the last
  flush, so its cost scales with the turn, not the whole session. The
  `SessionEnd` backstop still re-sends the full transcript-so-far once per
  session (the server's watermark discards whatever per-turn capture already
  sent) — a non-trivial token cost for a very long session, paid once rather
  than per turn.
- **Requires** the MemHub server to expose `import_conversation` (capture)
  and, for directive recall, `recall_directives`. If your `/mcp` connection
  lists both, you're good.

## Configuration

To point at a different MemHub instance, edit `plugins/memhub/.mcp.json`
(`url` and `oauth.clientId`).
