# MemHub for Claude Code, Codex, and Cursor

MemHub gives coding agents shared team memory: interactive MCP tools for
searching and saving knowledge, automatic session capture, situated directive
recall, and workflows for artifacts, specs, handoffs, rules, and PR review.
The same `memhub` plugin supports Claude Code, OpenAI Codex, and Cursor through
each host's native plugin system.

## What's in here

This repository publishes the `memhub` plugin to all three hosts.

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
codex/                              # legacy forwarding shims and Codex reference guide
```

## Read native sessions locally

The plugin also exposes the existing Codex and Cursor readers as a local JSONL
command. This is useful for discovery or importing history into another local
consumer:

```bash
python3 plugins/memhub/scripts/readers_cli.py --host codex --metadata-only
python3 plugins/memhub/scripts/readers_cli.py --host cursor --since 2026-01-01T00:00:00Z
```

Each session has an identity header followed by canonical records. Metadata-only
mode omits records and prompt-derived titles. Missing or unreadable sources
return an incomplete-coverage error; the command does not upload data or advance
capture cursors. See the [reader stream contract](docs/wire-contract.md) for
fields, filtering and exit codes.

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
Restart, open `/hooks`, and trust only MemHub's `SessionStart`, `PreToolUse`,
`PostToolUse`, and `Stop` handlers from `~/.codex/hooks.json`. Finally ask it to **Onboard MemHub
for this repo**.

> **Both of those steps are required, not optional.** Codex reports
> `plugin_hooks` as `removed` (`codex features list`, verified on 0.146 and
> 0.154), so the `hooks` key in the plugin's own manifest is never dispatched —
> **Set up MemHub** installs the user-level bridge that is the only path by
> which MemHub hooks run. And an untrusted handler is never dispatched either,
> so a session started before the `/hooks` approval captures nothing. Skip
> either step and capture is silently off, with the plugin otherwise appearing
> installed and healthy.

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

One Codex-specific rule falls out of that:

- **Codex's own threads are not captured.** Codex runs threads alongside
  yours — spawned subagents, guardian action-reviews, memory consolidation —
  and each copies the conversation it is working on, so it looks like a real
  transcript and takes its title from the reviewer's prompt. Capture reads
  `thread_source` from the rollout header and skips those three kinds by name.
  Everything else is yours and is captured: a rollout old enough to predate the
  field, and — deliberately — a kind we have never seen. Codex's own
  `ThreadSource` type is open-ended (`Feature(String)`), so new product
  surfaces appear as new values; refusing them by default would silently drop
  real sessions, and because the skip advances the capture cursor that loss
  would be unrecoverable. An unfamiliar value is captured and noted in the
  Codex capture log instead.

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
   (`MEMHUB_TURN_FLUSH=0`). The delta is **bounded** before it is sent — it
   goes through `transcript_chunks.slices()`, the same splitter the
   whole-transcript paths use, and only the first payload ships, with the
   cursor landing on the last record in it. Without that bound, a session whose
   pending delta outgrew the server's request limit got a `413` on every turn,
   could not advance a cursor past bytes that were never sent, and re-sent the
   same ever-growing delta forever — per-turn capture dead for the rest of that
   session's life, on exactly the long sessions worth keeping. A backlog now
   drains over the next few turns, because steady-state deltas are kilobytes;
   if the server's limit turns out to be lower still the cap halves for that
   session, and a single record no payload can carry is stepped over rather
   than pinning every turn behind it.
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

A fire is reported under the same session identity capture used, so the fire
history can show *which session* a rule fired in. That identity is namespaced
per host — Claude sends the session id bare, Codex and Cursor send
`codex-`/`cursor-`-prefixed, matching what their capture uploads. Before this,
a Codex fire named the bare id while its session was stored prefixed, so every
Codex fire showed as `Not linked yet`. The namespace is applied only on the
wire: local ordering state, obligations and dedup keys still key off the raw
id, so an in-flight session keeps its own state. Fires recorded by an older
plugin keep the id they were written with and stay unlinked — linking those
retroactively is a server-side change, not a client one.

Authoring (`/memhub:create-rule`, `/memhub:start-rulebook`) resolves which
book a rule lands in before drafting anything — one visible book is the answer,
several is a question for you, none is an offer to create one that binds only
you. The conflict check spans every book you can see, and flags a collision in
a book you are *not* filing into: nothing you file can supersede that rule, and
both will fire, so it goes to you as a decision.

### Harness-tied memory (flagged off)

On by default since v0.69.0 (set `MEMHUB_HARNESS_EXTRACT=0` to turn it off), the plugin helps a
correction you make in a session become a proposed team rule. At each turn's
Stop, a detached child sends a redacted slice of that turn to MemHub
(`POST /v1/team/rulebook/harness/classify`), whose classifier says whether the
moment is worth the agent's attention. At a later turn's Stop, once the agent
has finished your request, the hook **blocks the stop** and hands the flagged
moment to the agent that lived the turn. The agent cannot end the turn without
a verdict: if there is a lesson that would change what an agent does next
time, it runs the create-rule skill on it (which asks you which rulebook,
checks for a twin, and proves the rule), passing the turn's harness stamp;
if there is none, it says so in one line, `No rule from turn N: <why>`. At most
8 stops a session are blocked, and a moment more than 3 turns old is dropped. A
moment from the session's last turn is never handed. The rule lands
`proposed` for a person to activate; nothing fires from it, and the plugin
never activates one. The slice is redacted before it leaves the machine
(MemHub keys, home directories, e-mail addresses, command-line credentials,
quoted or not).
With the variable unset, the default, none of this runs.

## Skills

Sixteen skills ship in `plugins/memhub/skills/` (the deprecated `commands/`
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
- `/memhub:onboard [folder-or-file ...]` — connects a repo: resolves or
  creates its agent brain, caches the room so automatic capture routes there,
  scans the repo for its own markdown documents wherever it keeps them (no
  directory layout assumed — `scripts/onboard_docs.py`), saves the ones that
  look important to the brain as artifacts without asking (arguments override
  the choice), shows the brain's Index, says what now works (handoff, sharing,
  search), and ends with an optional hint that `/memhub:start-rulebook`
  creates the team's rulebook. It imports no session: sessions are captured
  automatically from the next turn.
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
- `/memhub:spec <work|init|revise|bootstrap|check|resolve|status|setup>` — the
  common entry point for the repository's spec workflow; routes to the focused
  skills below and preserves existing `init`, `revise`, `check`, and `status` use.
- `/memhub:spec-work <task>` — reads governing Git or Brain requirements before
  implementation, maps them to acceptance checks, and follows through on the
  requested code change without silently rewriting conflicting requirements.
- `/memhub:spec-check [file|topic|PR] [--base <ref>]` — compares requirements
  with the actual diff, including branch, staged, unstaged, untracked, renamed,
  and deleted paths. Reports evidence and coverage gaps; historical audits are
  context, not a substitute for fresh analysis.
- `/memhub:spec-maintain <init|revise|bootstrap|resolve|status|setup>` — creates
  and revises specs, resolves drift, and diagnoses workflow readiness. Bootstrap
  offers `--local` (the coding agent's usage) or `--cloud` (MemHub backend usage,
  subject to product credits). With neither a choice nor a supported saved
  preference, it asks before discovery. Both modes confirm domains before
  generation and never silently fall back to the other execution mode.
  Git specs live in the configured directory with `owns` frontmatter; their
  brain mirrors are read-only. Brain mode reads explicitly selected governing
  documents and prepares revision proposals. Publication requires a supported
  authenticated capability and a user request; a local draft isn't published.
  Cloud bootstrap currently produces Git spec PRs. These skills do not add a
  unified backend settings API or imply Brain parity for Git hooks/audits.

- `/memhub:create-rule` — creates a situated Rulebook rule from a concrete
  failure, correction, or procedure, resolves which rulebook it lands in (and
  offers to create one when you have none), and checks it for conflicts across
  every rulebook you can see before saving it.
- `/memhub:start-rulebook [--starter | --mine] [--days N | --all]` — where a
  team's Rulebook starts. It asks first which you want: **starter rules** (a
  tested set every team running a coding agent wants — irreversible git,
  secrets, tests before push, big files read as slices — fitted to this repo
  from a scan of it; about a minute), **rules from your own work** (your
  CLAUDE.md and your last 30 days of Claude Code / Codex / Cursor sessions;
  10–20 minutes, because it reads them), or **both**, de-duplicated into one
  list. Every candidate is run through the real hook and replayed over your
  sessions, and each proposal says why it exists, what it cost you, and what
  changes with it on. Older sessions are read only if you ask (`--days N`,
  `--all`). Files survivors as `proposed` into the rulebook you pick; never
  activates anything. (Was `/memhub:rules-from-sessions`.)
- `/memhub:pr-babysit [pr-number-or-url]` — usually **auto-armed**, not typed:
  a hook offers to start this as a self-paced loop right after `gh pr
  create` (see PR babysitting below). One pass polls the PR's review bots and
  CI, fixes real findings, and — once clean — saves the fixing process to the
  repo's agent brain.
- `/memhub:link-pr [pr] [--session <id>] [--unlink]` — links a coding session
  to a pull request in MemHub, so the PR's session context is published from a
  confirmed fact rather than a branch-name guess. This is also how a PR opened
  by something the hook cannot see — a script, a CI helper — gets linked, and
  on Cursor it is the only way to record a PR's work type, and only for a PR
  this session itself wrote.
- `/memhub:find-contributing-sessions [pr]` — scans this machine's session
  history (Claude Code, Codex, Cursor) for the sessions that wrote a PR's code,
  ranks the candidates by the evidence that matched, and links the ones you
  approve. It never links anything without an explicit yes, and it never
  records a work type — a scan cannot say what the work was.

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

**The agent also says what kind of work the PR is.** When it links a pull
request that has no type yet, the same call carries one of `feat`, `fix`,
`chore`, `docs`, `perf`, `refactor` or `other` — read off the change itself,
never parsed out of the title. MemHub deleted its own title-and-branch
inference, so a pull request nobody labels simply has no type; there is no
hidden backfill.

**The first label is permanent**, and that is why the type is asked for only
when the server reports the PR as unclassified. A second attempt does not just
lose the type: the server refuses the whole write, so the session would not get
linked either. A PR that already carries a type therefore gets the plain link
instruction, and a genuine race is reported rather than retried. A
multi-purpose PR gets its primary purpose; `other` is a legitimate answer and a
better one than a guess.

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

## Spec ownership reminder

Specs are authored in git under `docs/specs/` (or `MEMHUB_SPEC_DIR`). Their
frontmatter `owns` paths connect each spec to code. `/memhub:spec init` and
`revise` edit these files in the same branch as the implementation. Bootstrap
first proposes domains for human confirmation, then opens a spec-only PR.

After an edit, the hook names the owning spec once per session. Before a push
or PR, the Rulebook's `repo.spec_untouched` predicate checks the branch diff,
including working-tree edits, and names specs whose code changed without a
spec edit. Updating the spec records conversion of the reminder. No hook
uploads spec artifacts; backend merges and audits maintain read-only mirrors.
Retired specs and missing/malformed frontmatter do not fire. The legacy
`.claude/artifact-map.json` is no longer read or written.

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
