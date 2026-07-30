# MemHub for Claude Code

Auto-capture your Claude Code sessions into **MemHub team memory**. At the end of
each session, an agent hook reads the transcript and saves it through the
`memhub` MCP server, which runs **tool-aware (agentic) extraction** of
facts, episodes, and artifacts.

## What's in here

This repo is a **marketplace** with three plugins:

```
.claude-plugin/marketplace.json     # makes the plugins installable
plugins/memhub/                     # PROD build — install this one
├── .claude-plugin/plugin.json      # plugin manifest
├── .mcp.json                       # the memhub MCP server → prod (per-user OAuth)
├── hooks/hooks.json                # SessionEnd → agent hook → import_conversation
└── skills/                         # /memhub:* skills (also auto-invoked by Claude)
    ├── handoff-session/            # hand the current session to a teammate
    ├── import-session/             # import a past session, any size
    ├── save-artifact/              # store a file as a MemHub artifact
    ├── search-memory/              # read-only team-memory recall
    └── spec/                       # spec-driven dev on versioned spec artifacts
plugins/memhub-staging/             # STAGING build for MemHub devs (see below)
├── .claude-plugin/plugin.json      # its own manifest
├── .mcp.json                       # the memhub MCP server → staging
├── skills/  → ../memhub/skills     # symlinked: shared with memhub, never drifts
├── hooks/   → ../memhub/hooks      # symlinked
└── scripts/ → ../memhub/scripts    # symlinked
plugins/fleet/
├── .claude-plugin/plugin.json      # plugin manifest
├── hooks/hooks.json                # SessionStart/UserPromptSubmit/PostToolUse/SessionEnd
├── scripts/fleet_board.py          # one script, one subcommand per hook event
├── scripts/fleet_start_launch.sh   # session launcher (tmux/iTerm/Terminal/headless)
├── skills/start/                   # /fleet:start — decompose, provision, launch
└── skills/status/                  # /fleet:status — pretty-print the board
codex/                              # OpenAI Codex integration (not a Claude plugin)
├── codex_to_claude.py              # transform a Codex rollout → Claude Code record shape
├── import_codex_session.py         # import a Codex session into MemHub (reuses import_session.py)
├── codex_notify.py                 # optional `notify` hook for auto-capture
└── README.md                       # Codex MCP config + session capture
```

**Using Codex instead of Claude Code?** See [`codex/README.md`](codex/README.md):
the MemHub MCP server drops into `~/.codex/config.toml`, and
`import_codex_session.py` captures Codex sessions into the same team memory (it
reshapes the rollout so MemHub's agentic extraction kicks in — no backend
change).

## Install

```text
/plugin marketplace add XTraceAI/memhub-claude-plugin
/plugin install memhub@memhub
```

Then authenticate the MCP server once (the hook can't run until it's connected):

```text
/mcp
```

Select `memhub`, choose **Authenticate**, and approve in the browser.

### Prod vs. staging

Almost everyone wants **`memhub`** — it talks to the production backend.

**MemHub developers** iterating on the service can install **`memhub-staging`**
instead, which is byte-for-byte the same plugin (skills/hooks/scripts are
symlinked, so they never drift) but pointed at the staging backend:

```text
/plugin install memhub-staging@memhub
```

Both plugins register an MCP server literally named `memhub` (that's what keeps
the skills identical), so **install one or the other, never both** — two
`memhub` servers in one client collide. The two differ only in `.mcp.json`
(backend URL + OAuth client). Switching env = uninstall one, install the other,
then re-run `/mcp` to authenticate against the new backend.

## How it works

1. **SessionEnd** fires when a Claude Code session ends.
2. The **agent hook** (a subagent with MCP access) reads the session transcript
   `.jsonl` and calls `import_conversation`, passing the raw transcript records
   and the `session_id` as the `conversation_id`.
3. The MCP server auto-detects the Claude Code shape and routes to the
   **agentic** ingestion path: tool-bearing events are extracted with the
   `agentic` prompt variant (the agent is treated as a valid belief source;
   facts/episodes/artifacts land in your personal team-LTM).
4. Re-running the same session dedups (the `conversation_id` keys a deterministic
   re-import), so nothing is double-saved.

## Incremental flush on commit / PR (v0.2)

Besides the SessionEnd backstop, a `PostToolUse` hook watches for `git commit`,
`gh pr create`, and `gh pr merge` and flushes the transcript-so-far in the
background (async — never blocks your session). Commits are semantic work
boundaries: flushing there makes memory available **mid-session** (parallel
sessions see fresh decisions minutes after each commit), shapes episodes into
work-unit narratives, and survives sessions that never end cleanly. All
triggers share one `conversation_id` (= `session_id`) and one server-side
watermark, so the full transcript is re-sent but only the **delta** is ever
processed — total extraction cost is the same as a single end-of-session
import.

The flush hook authenticates with the plugin's own OAuth (same Auth0 client
as the `/mcp` connector, cached at `~/.config/memhub-plugin/`). A background
hook never opens a browser, so the cache must be seeded once by running any
memhub terminal script interactively — e.g. `/memhub:import-session` — or by
setting `$MEMHUB_TOKEN`. Until then the hook degrades silently (the
SessionEnd agent hook still captures everything at close).

## Client-side encrypted text (proof of concept)

Artifact content can be encrypted locally before upload and decrypted locally
after retrieval. This path is deliberately opt-in while MemHub has no client /
server key exchange and the backend's extraction pipeline still expects
plaintext session records.

The cryptography comes entirely from the official `xtrace-ai-sdk==0.1.1`
package (imported as `xtrace_sdk`): its `PassphraseKeyProvider` derives the key
and its `AESClient` performs authenticated AES-256-GCM encryption/decryption.
The plugin adds only a version marker around the SDK's ciphertext so a loader
can recognize it.

Set a passphrase without putting it in the command line, then upload:

```bash
read -rsp "MemHub encryption passphrase: " MEMHUB_ENCRYPTION_PASSPHRASE
export MEMHUB_ENCRYPTION_PASSPHRASE

uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/save_artifact.py \
  --file private-notes.md --name "Private notes" --type document --encrypt
```

The upload prints the artifact id. Fetch that artifact by id and decrypt its
body locally with the same passphrase:

```bash
uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/load_encrypted_artifact.py \
  --artifact-id "<artifact-id>"

# Or keep plaintext out of terminal output:
uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/load_encrypted_artifact.py \
  --artifact-id "<artifact-id>" --output restored-private-notes.md
```

Current limitations are intentional:

- There is no key exchange or recovery. The passphrase stays local and must be
  supplied out of band on every machine that decrypts; losing it loses access
  to the content.
- Only the artifact body is encrypted. Its name, type, tags, id, and other MCP
  metadata remain plaintext.
- The server receives opaque ciphertext, so it cannot extract, summarize, or
  semantically search this artifact. Load it directly by id.
- Session imports, automatic flushes, directives, and ordinary MCP tool calls
  retain their current plaintext behavior. Encrypting transcripts now would
  break server-side agentic extraction.

Run the offline SDK-backed tests with:

```bash
uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/test_encrypted_artifacts.py
```

## Skills (v0.6)

Five skills ship in `plugins/memhub/skills/` (the deprecated `commands/`
format is gone; invocation is unchanged). Each is both user-invocable as
`/memhub:<name>` and **model-invocable**: saying "save this spec to memhub" or
"what did we decide about X?" in plain language triggers the right skill.

- `/memhub:import-session <id-or-path> [title]` — terminal upload of a past
  session transcript; auto-chunks very large sessions.
- `/memhub:save-artifact <file> [name]` — terminal upload of a file as an
  artifact. Both upload skills exist so the model never re-emits file or
  transcript content token by token — a helper script ships the bytes.
- `/memhub:search-memory <query>` — read-only recall over facts, episodes,
  artifacts, and documents, with agent-brain / tag / time filters.
- `/memhub:handoff-session <teammate> [title]` — hand the current session to a
  teammate: creates an agent brain holding a composed handoff brief (goal,
  state, decisions, next steps, gotchas) plus the full session import, and
  shares it read-only via `share_agent_brain`. The teammate's agent picks it
  up by searching that agent brain.
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

## Artifact-sync reminder (v0.11)

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

## Fleet plugin (v0.1)

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
(v0.3) decomposes the task into 2–4 independent workstreams (confirming the
split first), provisions a worktree + branch + kickoff brief per stream, and
launches a real session in each — interactive tabs (tmux/iTerm/Terminal) or
`--headless` detached runs. Launched sessions register on the board through
the normal hooks, so coordination from there is automatic.

Pairs with the memhub plugin: the board says *who is doing what right now*
(seconds, one line each); the flush hook already lands every session's
history in MemHub, so an agent that needs the *why* behind a sibling's
change searches team memory with the session id from the board entry.
Each board entry costs ~1 short line of injected context; everything fails
soft (not a git repo / hook error → silent no-op).

## Notes & trade-offs

- **Auth is per-user.** Nothing secret travels with the plugin — each person
  authenticates the MCP server themselves via `/mcp`. The first time the hook
  tries to use the server you'll see a trust/permission prompt; that's by design.
- **Cost.** The agent hook reads the full transcript and passes it to the tool
  at session end. For long sessions that's a non-trivial token cost once per
  session. (A future refinement: incremental capture via a transcript watermark.)
- **Requires** the backend `import_conversation` tool (MemHub-Backend branch
  `fm-feat/mcp-agentic-session-ingest`) to be deployed to staging.

## Configuration

To point at a different MemHub instance, edit `plugins/memhub/.mcp.json`
(`url` and `oauth.clientId`).
