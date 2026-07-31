---
description: Use when the user asks to save, store, or upload a file/document/spec to MemHub or team memory as an artifact (e.g. "save this spec to memhub", "store this doc as an artifact", "version this design doc in memhub"). Uploads the file's bytes via a terminal script — never call save_artifact directly or re-emit file contents.
argument-hint: <file-path> [artifact name]
allowed-tools: Bash
---

Store an existing file as a MemHub artifact. The file's bytes are uploaded by a
helper script — **do NOT call the `save_artifact` MCP tool yourself and do NOT
paste/retype the file contents**; that would regenerate the whole document token
by token. This is a terminal operation, like `cat`-ing a file.

Arguments: `$ARGUMENTS`
- First token = the path to the file to store (required). If invoked without a
  path (e.g. the user said "save this to memhub" about a file just discussed),
  use that file's path; ask if ambiguous.
- Remaining text = the artifact name (optional; if omitted, use the file's base
  name as a readable title).

**One canonical artifact per topic.** When a conclusion changes, VERSION that
artifact (same `--name`, or `--parent-id` its latest version) — never publish a
parallel one. Retrieval is semantic, so an un-retracted stale claim can outrank
its own correction and mislead the next agent. Before uploading something that
restates or corrects existing knowledge, `search_memory` for the artifact it
supersedes and pass `--parent-id`; if a prior conclusion is now wrong, say so
explicitly in the new version rather than leaving both to compete.

Do exactly this:

1. Resolve the file path (`$1`) and a name. If no name was given, derive a short
   Title-Case name from the filename.
2. Pick an `artifact_type` from the extension/content: `spec`, `design_doc`,
   `adr`, `runbook`, or `document` (default).
3. Run the upload via Bash — substitute the real values, keep it one command:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
     --file "<path>" --name "<name>" --type "<type>"
   ```

   The artifact routes to the repo's room automatically when one is cached in
   `~/.config/memhub-plugin/rooms.json` (the script prints which room it used),
   so a file in a repo lands where teammates search without any extra flag.

   Optional flags when relevant: `--agent-brain-id <id>` to override the
   destination brain, `--no-room` to save into personal workspace memory
   instead, `--parent-id <id>` to version an existing artifact, `--rationale
   "..."` to note why this version supersedes the last, `--tags a,b`.
4. Report the returned `{id, action}` to the user, **and which brain it landed
   in, by name** — automatic routing that happens silently reads as losing
   things. On first ever run the script may
   open the browser once for OAuth approval (same flow as /mcp; token cached
   after that) — that is expected, not an error.

You only emit the short command with a path — the script reads the file and
ships it to `save_artifact`.
