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

Cursor's hooks are observational — `afterShellExecution` defines no reply the
agent can see — so the rulebook and the session ↔ PR link hook do not run
there. Cursor links a session to a pull request with `/memhub:link-pr`.

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

5. **Naming.** A captured session is called what its host calls it, so the
   sessions list in MemHub reads the same as the one in the editor: Claude
   Code's generated title (a rename by the user outranks it), and on Codex the
   `thread_name` Codex itself generated — read from the rollout, or from
   `~/.codex/session_index.jsonl` for the hosts that only record it there —
   passed through verbatim. Only a session its host never named falls back to
   a title derived from the first prompt, trimmed to one readable line. Codex
   re-derives this on every flush, so a thread renamed mid-session updates on
   its next turn; Cursor exposes no host-generated name, so its title stays the
   one derived from the opening ask.

All of the above authenticate with the plugin's own credential — separate
from `/mcp`, provisioned by `/memhub:login` (see Install) — because they run
as cold background processes that can never open a browser.
`SessionStart` also runs `capture_health.py`, a *synchronous* check (the
async flush hooks can't surface anything to the user) that reports via
`systemMessage` when the capture credential has expired or a recent flush
failed; silent on the healthy path so it doesn't become wallpaper.

### Cursor observation history

Cursor's native session files omit some usage and timing information. Hooks
save available usage by generation and timestamp pins locally before waiting
for an upload, so a busy cloud upload does not discard a completed-turn usage
observation. Existing cloud delivery keeps its own upload lock and progress.

Saved usage remains available beyond 512 measured records for later local
reads and manual imports. The state retains one exact sample per measured
record rather than a second transcript archive. Unobserved historical usage
stays unknown; a later read never invents missing measurements.

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
names the repo's default agent brain before the first prompt and renders
three checkpoints under one budget, keyed on identifiers, never on similarity:

- **Map** — what the brain holds: the top of the compiled overview's Index
  (counts and the drill commands), plus a short clip of its prose.
- **Apply** — lessons and procedures whose triggers intersect the files this
  branch touches (`git diff --name-only origin/<default>` plus the last 20
  commits' paths), from `recall_directives(entities=…)`. At most five.
- **Recall & Consult** — the episodes and artifacts that *name* the same
  identifiers (paths, `PR #N`, `ENG-N`); a search hit survives only when it
  contains one of them as an exact token. At most five.

`brief` is stdlib-only and makes no network call: the map comes from the
overview cache, apply/recall from a pointer cache, and it spawns a detached
`brain_brief.py pointers` child to refresh that cache, which the first
prompt's hook then delivers. (A live call at `SessionStart` would put the
map itself at the mercy of the host's 5 s hook timeout.) A `UserPromptSubmit`
hook (`brain_brief.py prompt`) extracts identifiers from each prompt — file
paths, symbols that exist in the repo, `PR #N` / `ENG-N`, quoted error strings
— and fires one bounded recall on them, at most three pointers in 600
characters; a prompt with no identifier costs nothing and prints nothing.
There is deliberately no semantic search on prompt text.

Everything rendered joins the session's served list (shared with
`directive_recall`'s `already_fired`), so nothing is shown twice. The budget
is `MEMHUB_BRIEF_TOKEN_BUDGET` (default 2,500 tokens, chars/4), split 2:1
between the brief and the rulebook's session block; when the brief is cut it
drops Recall pointers first, then Apply, never the map, and ends with
`… trimmed to budget`. `MEMHUB_BRIEF_POINTERS=0` disables the background
worker. The brief only tells the *user*, via `systemMessage`, when the
resolved brain changes; a repo with no cached room stays silent. A companion
`Stop` hook (`brain_brief.py refresh`, async) fetches `get_brain_overview`
into the overview cache, throttled to once per 6 hours.

### Rulebooks

A team rule lives in a **rulebook** — a container with its own membership, not
a brain and not a document. Whoever is a member of a rulebook has their coding
agent bound by its active rules; a rulebook can bind an explicit list of people
or every member of the org, and one person can be in several (their org's book
plus their team's). Membership is managed in MemHub, not from here: the plugin
reads it, and the only container it ever creates is one you say yes to.

The `SessionStart` / `PreToolUse` / `PostToolUse` hooks
(`scripts/rulebook_hook.py`) cache every book that binds you, per repo, and
inject a matching rule as an advisory at the moment of the call. An edit rule
(`event: edit`) sees a file however it was written: through the Edit/Write
tools before the write, and through a Bash command — `cat > f <<EOF`, a
`python - <<PY … write_text()`, `sed -i` — right after it, because the
post-call hook reads what the command left changed on disk (git decides what
is a candidate, the file's mtime decides what that call touched) and runs the
same matcher on it. In auto mode the agent is told to write through Bash, so
without this an edit rule would be silent exactly where it is needed. Two
books can both fire on one call. When they do, the plugin does **not** pick a winner and The repo is
the one the **call** works in — the edited file's checkout first, the session's
directory second — so working from a folder that merely *contains* your
checkouts still applies each one's own rules to the calls that touch it, and a
rule scoped to a repo matches from any of its worktrees. A path is followed
only while it stays inside the session's directory, so what the agent edits
can never point the hook at a checkout you did not open. Session-start rules
are the exception, since that event names no file: they resolve from the
session's directory alone, and a session rooted *above* your checkouts still
gets none.
Two books can both fire on one call. When they do, the plugin does **not** pick a winner and
hide the loser — both fire and both reach the local ledger — but the per-call
advisory cap and the session-start note budget are spent **widest book first**,
so an org-wide policy is never crowded out by a two-person book's note. Session
start names the books in play when there is more than one; in-flight advisories
stay as short as they were. The server takes no position on any of this: it
puts each rule's book, scope and member count on the wire and the client
decides.

Authoring (`/memhub:create-rule`, `/memhub:rules-from-sessions`) resolves which
book a rule lands in before drafting anything — one visible book is the answer,
several is a question for you, none is an offer to create one that binds only
you. The conflict check spans every book you can see, and flags a collision in
a book you are *not* filing into: nothing you file can supersede that rule, and
both will fire, so it goes to you as a decision.

## Skills

Thirteen skills ship in `plugins/memhub/skills/` (the deprecated `commands/`
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
- `/memhub:create-rule` — creates a situated Rulebook rule from a concrete
  failure, correction, or procedure, resolves which rulebook it lands in (and
  offers to create one when you have none), and checks it for conflicts across
  every rulebook you can see before saving it.
- `/memhub:rules-from-sessions` — one run over your CLAUDE.md **and** your
  past coding sessions (Claude Code, Codex, Cursor): every candidate rule is
  replayed through the real hook, and each proposal says why it exists (the
  CLAUDE.md sentence, or the sessions and your own words), what it cost you,
  and what changes with it on. Hook rules first — at the command, on the
  error, when a name comes up — session-start notes last. Files survivors as
  `proposed` into the rulebook you pick; never activates anything.
- `/memhub:pr-babysit [pr-number-or-url]` — usually **auto-armed**, not typed:
  a hook offers to start this as a self-paced loop right after `gh pr
  create` (see PR babysitting below). One pass polls the PR's review bots and
  CI, fixes real findings, and — once clean — saves the fixing process to the
  repo's agent brain.
- `/memhub:link-pr [pr] [--session <id>] [--unlink]` — links a coding session
  to a pull request in MemHub, so the PR's session context is published from a
  confirmed fact rather than a branch-name guess. This is also how a PR opened
  by something the hook cannot see — a script, a CI helper — gets linked.
- `/memhub:find-contributing-sessions [pr]` — scans this machine's session
  history (Claude Code, Codex, Cursor) for the sessions that wrote a PR's code,
  ranks the candidates by the evidence that matched, and links the ones you
  approve. It never links anything without an explicit yes.

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

### Session ↔ PR linking

After a call that **addresses GitHub** — `gh pr …`, a `curl` / `gh api` request
to the REST API, or a GitHub MCP tool — whose output names exactly one pull
request, a `PostToolUse` hook (`pr_link_trigger.py`) asks the backend one
question and injects one instruction. There are three answers:

- the org has no GitHub integration connected → the agent mentions once, and
  only if it isn't intrusive, that connecting GitHub is what links sessions to
  the code that shipped;
- **this call ran `gh pr create`** (or the GitHub MCP create tool), and the
  command is one where the returned URL provably came from that create → the
  session links itself, unconditionally. Opening a PR is itself work the
  session did, so no authorship question is asked; a PR has many sessions and
  linking one displaces none;
- **any other GitHub call naming one PR** → the agent decides. It links only if
  it wrote that code in this session, and otherwise offers
  `/memhub:find-contributing-sessions`.

Unconditional self-linking is deliberately the **narrow** lane. A hand-rolled
`curl -X POST …/pulls` is not treated as a creation: recognising a write meant
parsing the option grammar of four HTTP clients to find a method and a body,
and getting that wrong makes an authorship claim nobody can withdraw. Measured
across 15,134 tool calls from 150 real sessions, that layer decided nothing —
every genuine creation was a `gh pr create`. So a `curl` POST lands in the
judged lane instead, which is the safe direction.

The same conservatism applies to the shell around the create. A pipeline hides
a failed `gh pr create` — whose stderr carries the *existing* PR's URL — so
`gh pr create … 2>&1 | tail -5` declines to the judged lane rather than
claiming authorship. **Heredoc bodies are removed before any of this is
decided**: `--body-file - <<'EOF'` is how essentially every real PR body is
written, and reading that prose as command text used to break both directions
at once — a body's apostrophes hid the real `gh pr create` from the parser,
while a `python3 - <<'PY'` script that merely *mentioned* the words looked like
one.

So **linking is never automatic for work this session did not do** — the agent
judges, and offers the finder when the answer is no. The hook is
stateless and holds no per-PR file: a session↔PR relationship is many-to-many,
and a dedup file keyed on the PR is exactly what would stop a genuinely new
session from linking itself later. Every path degrades to silence — a
disconnected org, an unreachable server, no credential, a listing command whose
output names several PRs, or a command that merely *mentions* a PR without
addressing GitHub.

The **one** thing it remembers is a negative: an org that has the feature on but
no GitHub connected cannot change that without an admin acting, so that answer
is cached for **30 minutes** rather than re-asked on every `gh pr` command. The
entry is scoped to the deployment, the repo *and* the credential that earned it,
because which org answers depends on which token is resolved. Two replies are
deliberately never cached — a connected one (`linked_sessions` and `pr.known`
change constantly) and one that just says the feature is off, since the server
answers that from a flag check without looking at the integration at all, so its
`github_connected` field is a default rather than a finding. Caching that field
once silenced linking for a day on machines whose GitHub was connected the whole
time. Set `MEMHUB_PRLINK_NEGATIVE_TTL_S` to change the window; `/memhub:link-pr`
always asks live.

**A PR opened by some other means — a script, a Makefile target, a CI helper,
`hub pull-request` — is not detected, deliberately**: recognising arbitrary
programs that happen to open a PR is the automatic-attribution problem this
design walked away from. `/memhub:link-pr` is one command away.

Per-host coverage differs, because the hosts differ:

| Host | `gh pr create` | GitHub MCP tool | Fallback |
|---|---|---|---|
| Claude Code | detected | detected | — |
| Codex (plugin hooks) | detected | detected | — |
| Codex (compatibility bridge) | detected | not detected | `/memhub:link-pr` |
| Cursor | not detected | not detected | `/memhub:link-pr` |

Cursor ships skills-only here: its `afterShellExecution` hook has no output
schema at all — only `beforeShellExecution` can say anything to the agent, and
that fires before the command has produced a PR URL. The Codex split is a
trust one: the plugin-bundled hook manifest is re-fetched on upgrade and costs
nothing to widen, while the compatibility bridge lives in the user's own
`~/.codex/hooks.json` and widening it would require them to re-approve it.

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

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Copyright 2026 XTrace Inc.
The installable MemHub plugin includes its own copies of `LICENSE` and `NOTICE`.
