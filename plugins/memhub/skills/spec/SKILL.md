---
description: Use when the user wants spec-driven development backed by team memory — create, revise, drift-check, or report on a spec held in MemHub (e.g. "start a spec for X", "save this as the team spec", "revise the spec", "did the spec change under me?", "what's the status of the retry-policy spec?"). Specs are versioned artifacts in the repo's shared agent brain; every revision carries a rationale and is diffable.
argument-hint: <init|revise|check|status> [file|topic] [...]
allowed-tools: mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__get_artifact, mcp__plugin_memhub-staging_memhub__get_artifact, mcp__plugin_memhub_memhub__get_artifact_lineage, mcp__plugin_memhub-staging_memhub__get_artifact_lineage, mcp__plugin_memhub_memhub__diff_artifact_versions, mcp__plugin_memhub-staging_memhub__diff_artifact_versions, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__share_agent_brain, mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub_memhub__list_tags, mcp__plugin_memhub-staging_memhub__list_tags, Bash
---

Run spec-driven development on top of MemHub. The model:

- **One agent brain per repo** — the repo's shared room, named
  `Repo: <org>/<name>` from `git remote get-url origin` (host and `.git`
  stripped — e.g. `Repo: XTraceAI/memhub-claude-plugin`). Match it EXACTLY in
  `list_agent_brains` and reuse what you find — a teammate may have created
  it. Edge cases (SSH remotes, no remote, worktrees, not a git repo) and the
  create-time rules — resolve before create, required description, report
  where it landed — are in `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md`.
  ALL of the repo's specs live there, alongside reviews, ADRs, and imported
  implementation sessions — share it once per teammate and every current and
  future spec in the repo is visible to them.
- The **spec is a versioned artifact** (`artifact_type: "spec"`) inside that
  room. Revisions are versions with a `rationale`; `diff_artifact_versions`
  shows what moved and `get_artifact_lineage` shows why, in order.
- A **work-item tag `spec:<slug>`** (kebab-case from the title) goes on the
  spec and every related artifact. Many specs share one room, so the tag
  (plus the artifact name `Spec: <title>`) is how revise/check/status pick
  out THIS spec — never guess by name similarity alone.
- The spec also lives **in the repo as a file** (default
  `docs/specs/<slug>.md`). The file is what implementers read in their
  worktree; the artifact is the shared truth. `check` compares the two.
  Because `init` accepts any existing file as the spec, the file's
  repo-relative path is recorded on the artifact as a **`path:<repo-relative-path>`
  tag** — that tag, not the default location, is how later sessions find the
  file again.
- The spec records **which source files it governs**, in the repo-local
  `.claude/artifact-map.json` (written by the helper script below, never by
  hand). That map is what the plugin's artifact-sync PostToolUse hook reads:
  editing a mapped file injects a reminder to VERSION this spec rather than
  publish a parallel artifact. Writing it is part of `init`/`revise` — the
  index is a byproduct of spec-driven development, not a second chore.
- Sharing is **read-only**: teammates can search/check/status the room, but
  uploads into it work only for its creator. The intended flow: the spec
  owner runs `init`/`revise`; read-only members propose changes by editing
  the repo file (PR), and the owner lands them as a revision.

File uploads ALWAYS go through the helper script (never call the
`save_artifact` MCP tool directly, never re-emit file contents):

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
  --file "<path>" --name "Spec: <title>" --type spec \
  --agent-brain-id "<repo-ab-id>" --tags "spec,spec:<slug>,path:<repo-relative-path>" \
  [--parent-id "<latest-version-id>"] [--rationale "<why>"]
```

Linking the spec to the code it governs ALWAYS goes through the map script
(never hand-edit `.claude/artifact-map.json`). It is idempotent per artifact
id — re-running replaces that artifact's link, so revisions just refresh the
globs:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/artifact_map.py" add \
  --artifact-id "<root-version-id>" --brain-id "<repo-ab-id>" \
  --name "Spec: <title>" --glob "<repo-relative globs>"
```

`--glob` takes repo-relative POSIX patterns with `*`, `**`, `{a,b}` braces,
and `|` between alternatives (e.g. `app/retry.py|app/**/backoff.py`). Use the
`--artifact-id` of the lineage's FIRST version and keep it stable across
revisions — the hook passes it as `parent_id`, which chains the new version
onto the lineage regardless of which version is currently latest.

Arguments: `$ARGUMENTS` — the first token is the subcommand and is consumed
before the per-subcommand parsing below; each subcommand reads only the
REMAINING tokens. With no recognized subcommand, infer one from what the user
said (creating → init, editing → revise, "did it change" → check, "where are
we" → status) and treat all of `$ARGUMENTS` as its arguments.

Every subcommand starts by **resolving the repo's room**: derive the name as
above, then match it EXACTLY in `list_agent_brains` — it may be one a
teammate created and shared with you; use theirs rather than creating a
duplicate. Only `init` creates it when missing (`create_agent_brain`, omit
`workspace_id` — you need creator access to share it); the other subcommands
stop and point at init if no room exists. Not a git repo → ask which agent
brain to use.

## init `[file-path | title...] [for <teammates>]`

1. Get the spec content. If the first remaining token is an existing file
   path (e.g. `init docs/specs/retry.md` → `docs/specs/retry.md`), that
   file IS the spec. Otherwise compose the spec from the current conversation
   (sections: Goal, Non-goals, Design, Decisions, Open questions, Milestones)
   and write it to `docs/specs/<slug>.md` in the repo — composing it yourself
   is the point here; this is NOT the file-upload case the save-artifact
   skill guards against. Derive `<title>` from the argument or content;
   `<slug>` is its short kebab-case form.
2. Resolve the repo's room; create it only if no exact-name match exists.
3. Check whether THIS spec already has a lineage there: `search_memory` with
   `memory_type: "artifacts"`, `tags: ["spec:<slug>"]`, and the room's
   `agent_brain_id`. A hit → STOP the init flow and run the **revise**
   steps instead (`--parent-id` the newest version, rationale required) —
   uploading without a parent would create a second root artifact and break
   check/revise diffs. Any `for <teammates>` sharing still applies (step 6).
4. Upload the file with the script (no `--parent-id` — this is the first
   version of a fresh lineage). `path:` in `--tags` is the spec file's path
   relative to the repo root — the path the user gave, or the
   `docs/specs/<slug>.md` you wrote; never an absolute path.
5. Link the spec to the code it governs: run the map script with the new
   artifact's id. Derive the globs from the spec's own Design/Milestones —
   the files it says will be written or changed — and confirm them with the
   user in one line before writing ("this spec governs `app/retry.py`,
   `app/**/backoff.py` — right?"). A spec that governs nothing concrete yet
   (pure research or a decision record) → skip and say you skipped it.
6. If the user named teammates ("for Alice and Bob"), resolve each via
   `list_teammates` (case-insensitive; ambiguous → show candidates and ask,
   never guess between two people) and `share_agent_brain` with each
   `user_id`. Tell the user this opens the repo's WHOLE room — every spec
   and imported session in it, now and future — not just this spec. Nobody
   named → skip; note it may already be shared from an earlier spec.
7. Report: artifact id, room name, file path, the `spec:<slug>` tag, the
   globs now linked to it, who can see it, and the line teammates send their
   agent verbatim:

   > Ask your agent: *search the "Repo: <org>/<name>" agent brain in
   > memhub for "<title>"*

## revise `[file-path] [rationale...]`

1. Resolve the room, then the spec inside it: `search_memory` with
   `memory_type: "artifacts"`, the room's `agent_brain_id`, and
   `tags: ["spec:<slug>"]` if the slug is known from context, else
   `tags: ["spec"]` plus a query for the topic. Several candidates → ask.
2. `get_artifact_lineage` on it; the NEWEST version's id is the
   `--parent-id`.
3. The revised content is the repo file — the path given as the argument,
   else the artifact's `path:` tag, else `docs/specs/<slug>.md`. If the
   change was discussed but not yet applied, edit the file first. A rationale
   is REQUIRED — take it from the arguments or the conversation; if you can't
   state why this version supersedes the last, ask.
4. Upload with the script (`--parent-id`, `--rationale`, same `--name` and
   tags as before — except `path:`, which must reflect the file's current
   repo-relative path: update it if the file moved, add it if the lineage
   predates path tags).
5. Refresh the link if the revision changed which files the spec governs
   (new components, moved paths): re-run the map script with the SAME
   `--artifact-id` as the existing link (`artifact_map.py list` shows it) and
   the updated globs — it replaces that link rather than adding a second. No
   link yet (lineage predates the map) → add one now, keyed on the lineage's
   root version id.
6. `diff_artifact_versions` (previous → new) and report the delta in plain
   English plus the rationale. Remind the user that teammates' agents see the
   new version on their next `check` — there is no push notification.

If the upload fails on permissions, the room belongs to a teammate and you
are a read-only member: don't fight it — put the change in the repo file via
the normal PR flow and tell the user the room's owner runs `spec revise` to
land it as a version.

## check `[file-path]`

Answer: "is the spec I'm building against still the spec?"

1. Resolve the room and the spec (as in revise), `get_artifact` the latest
   version.
2. Find the local spec file — first existing match wins: the argument; the
   artifact's `path:` tag; `docs/specs/<slug>.md`; the file from earlier in
   this session. A candidate missing on disk just falls through to the next
   (only an explicit argument that doesn't exist is an error worth raising).
   Compare contents:
   - identical → in sync; say so, one line, done.
   - local matches an OLDER version in the lineage (walk `get_artifact_lineage`,
     compare against each) → the spec moved underneath: report every newer
     version's rationale in order, `diff_artifact_versions` from the local
     version to latest, and which changed sections touch work from this
     session.
   - local matches NO version → local edits never landed: show the
     local-vs-latest difference and offer `revise` (if local should win) or
     overwriting the file with the latest artifact content (if the team
     version should win). Never overwrite without asking.
2b. Also check the other direction — has the CODE moved out from under the
   spec? Read this spec's globs (`artifact_map.py list`) and run
   `git log --oneline --since=<the latest version's date> -- <globs>`. Commits
   there mean mapped files changed after the spec's last revision: name them
   and ask whether the spec needs a `revise`. No link for this spec → say so
   and offer to add one, since without it the artifact-sync hook can't fire.
3. No local file at all → print the latest version's content summary,
   rationale chain, and where to write the file (the `path:` tag's location,
   else `docs/specs/<slug>.md`).

## status `[topic]`

The multiplayer view: what the team's memory holds about a spec — or the
whole repo.

1. Resolve the room. No topic given → repo overview: `search_memory` the room
   for artifacts tagged `spec`, list each spec with its version count and
   latest rationale plus any recent related activity, and stop.
2. With a topic, pick the spec (as in revise), then `search_memory` the room
   with `memory_type: "all"`, a raised `top_k` (~30), and the spec title +
   topic as the query. The room is repo-wide — facts and episodes from OTHER
   specs' sessions will surface; filter by relevance and drop them rather
   than padding the report.
3. Report, citing memory types: current version + how many revisions and the
   latest rationale; decisions recorded (facts/episodes from imported
   implementation sessions); related artifacts (reviews, ADRs, handoffs);
   open questions still in the spec. If nothing relevant exists beyond the
   spec artifact itself, say so plainly — no implementation session touching
   this spec has been imported yet.

To land an implementation session into the repo's room, import it with a
fresh conversation id (re-imports dedup per conversation_id globally) and a
title naming the spec:

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" --agent-brain-id "<repo-ab-id>" \
  --conversation-id "$(uuidgen)" --title "Spec: <title> — <what was built>"
```

Plain-English output throughout; surface ids only where the user needs them
(artifact id, agent brain id for scripts). On first ever script run the
browser may open once for OAuth approval — expected, not an error.
