---
description: Use when the user asks to save, store, or upload a file/document/spec to MemHub or team memory as an artifact (e.g. "save this spec to memhub", "store this doc as an artifact", "version this design doc in memhub"). Uploads the file's bytes via a terminal script — never call save_artifact directly or re-emit file contents.
argument-hint: <file-path> [artifact name]
allowed-tools: Bash, mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Store an existing file as a MemHub artifact. The file's bytes are uploaded by a
helper script — **for content that already exists on disk, do NOT call the
`save_artifact` MCP tool yourself and do NOT paste/retype the file contents**;
that would regenerate the whole document token by token. This is a terminal
operation, like `cat`-ing a file. Content you compose in-session and that
lives nowhere on disk (a handoff brief, a PR review record) is the other case:
there, calling `save_artifact` directly is fine — nothing is being re-emitted.

Arguments: `$ARGUMENTS`
- First token = the path to the file to store (required). If invoked without a
  path (e.g. the user said "save this to memhub" about a file just discussed),
  use that file's path; ask if ambiguous.
- Remaining text = the artifact name (optional; if omitted, use the file's base
  name as a readable title).

**One canonical artifact per topic.** When a conclusion changes, VERSION that
artifact — re-upload under the same `--name` into the same brain and the
server chains it onto the lineage's head — never publish a parallel one.
Retrieval is semantic, so an un-retracted stale claim can outrank its own
correction and mislead the next agent. Before uploading something that
restates or corrects existing knowledge, `search_memory` (`memory_type:
"artifacts"`) for the artifact it supersedes and reuse its exact name; if a
prior conclusion is now wrong, say so explicitly in the new version rather
than leaving both to compete. `--parent-id` exists for explicit chaining but
must be the CURRENT latest version's id (the server rejects an older one as
`parent_stale`) — the same name is simpler and never stale.

Do exactly this:

1. Resolve the file path (`$1`) and a name. If no name was given, derive a short
   Title-Case name from the filename.
2. Pick an `artifact_type` from the extension/content: `spec`, `design_doc`,
   `adr`, `runbook`, or `document` (default).
3. Run the upload via Bash — substitute the real values, keep it one command:

   ```bash
   uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
     --file "<path>" --name "<name>" --type "<type>"
   ```

   The artifact routes to the repo's room automatically — from the cache in
   `~/.config/memhub-plugin/rooms.json`, or resolved from the server when the
   cache is empty (the script prints which room it used) — so a file in a
   repo lands where teammates search without any extra flag.

   Optional flags when relevant: `--agent-brain-id <id>` to override the
   destination brain, `--no-room` to save into personal workspace memory
   instead, `--rationale "..."` to note why this version supersedes the last,
   `--parent-id <id>` only with the current head's id.

   **Always pass `--tags`.** Two or three topic words is enough
   (`--tags retry,reliability`); the server normalises them to lowercase
   snake_case, so `Skill-Design` is stored as `skill_design` and searches
   normalise the same way. Tags are what a later `search_memory(tags=…)` can
   filter on, and an org with required tags turned on REFUSES an untagged save
   and answers with the brain's own tag vocabulary — pick from what it offers
   and re-run rather than inventing a new word.

4. **A rendered deliverable — an HTML page, a chart PNG, a PDF — goes through
   `--attach`, not `--file`.** `--file` reads UTF-8 text; a deliverable is
   bytes, and they are base64-encoded by the script so they never pass through
   your context either:

   ```bash
   uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
     --attach "<rendered file>" [--attach "<another>"] \
     [--entrypoint "<the file to render first>"] \
     [--file "<a short text summary>"] --name "<name>" --type document
   ```

   `--attach` is repeatable and the bundle path is each file's basename
   (so `--entrypoint index.html` names an `--attach build/index.html`). Give a
   text body too whenever you have one: the bytes are stored as the payload and
   the text is what makes the deliverable findable by search. Keep the bundle
   to a few MB — a large file belongs in `ingest_document_from_url` instead.
5. Report the returned `{id, action}` to the user, **and which brain it landed
   in, by name** — automatic routing that happens silently reads as losing
   things. On first ever run the script may open the browser once for approval
   and then mint a personal access key (`mhk_…`) that later runs reuse without
   one — that is expected, not an error. It is the plugin's own credential, not
   the `/mcp` connector's; `/memhub:login` provisions it up front if you would
   rather not be interrupted mid-save.

You only emit the short command with a path — the script reads the file and
ships it to `save_artifact`.
