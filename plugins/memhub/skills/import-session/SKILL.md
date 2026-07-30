---
description: Use when the user asks to import, upload, or save a Claude Code session/conversation/transcript into MemHub or team memory (e.g. "import this session into memhub", "save session <id> to memhub", "put that conversation in an agent brain"). Ships the transcript via a terminal upload script — any size, no token-by-token re-emit.
argument-hint: <session-id-or-path> [title...]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains
---

Import a past Claude Code session into MemHub team memory on demand. A helper
script reads the transcript file and ships it to the `import_conversation` MCP
tool — **do NOT call the MCP tool yourself and do NOT read or paste transcript
content**; sessions can exceed a million tokens and the script handles any
size in one call. This is a terminal operation.

Arguments: `$ARGUMENTS`
- First token = a session id (e.g. `03374a1f-b074-4eb9-9900-...`) or a path to
  a `.jsonl` transcript (required). A bare id is resolved automatically under
  `~/.claude/projects/*/`.
- Remaining text = an optional conversation title.
- If invoked without arguments (e.g. the user said "import this session"), ask
  which session they mean — or, for "this/the current session", use the most
  recently modified `.jsonl` sitting DIRECTLY inside the `~/.claude/projects/`
  directory matching the current working directory (top level only — `.jsonl`
  files in subdirectories are subagent/workflow transcripts, not sessions).

Do exactly this:

1. **Resolve the destination — default to the repo's room.** A session about a
   repo belongs in that repo's brain, where teammates and future sessions can
   find it; raw workspace memory is the fallback, not the default.
   - Derive `Repo: <org>/<name>` from `git remote get-url origin` (host and
     `.git` stripped), then `list_agent_brains` → **exact-name match**. Found →
     use its `agent_brain_id`.
   - **No match, or not in a git repo → do NOT create a brain.** Import into
     workspace memory (omit `--agent-brain-id`) and say so, mentioning that
     `/memhub:onboard` sets up the repo's room if they want one.
   - The user naming a brain explicitly always wins over both.
   - Edge cases (SSH remotes, no remote, worktrees) are in
     `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md`.

2. Run the import via Bash — one command, substitute the real values:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
     --session "<session-id-or-path>" [--title "<title>"] \
     [--agent-brain-id "<id>"]

   Pass `--agent-brain-id` with the id resolved in step 1. Omit it only for
   the workspace-memory fallback.
   NOTE: re-imports dedup per conversation_id GLOBALLY — to re-extract an
   already-imported session into an agent brain, pass a fresh
   `--conversation-id`.
   Very large transcripts are AUTO-CHUNKED (default threshold ~3.5MB): the
   script sends disjoint slices sequentially under one conversation_id and
   waits for each slice's extraction (the session gist folding forward)
   before the next — payloads beyond ~8MB fail server-side as one shot, so
   never disable chunking for huge sessions. This is slow but unattended;
   just let the command run.
   ```

3. Report back the returned `conversation_id`, `path` (should be `"agentic"`
   for Claude Code sessions), `messages_received`, and scope. Tell the user:
   - **where it landed, by name** — "imported into `Repo: <org>/<name>`" or
     "imported into your workspace memory" — so a wrong destination is
     obvious now rather than weeks from now;
   - extraction runs in the background (allow several minutes for large
     sessions) — facts, episodes, artifacts, and the session **gist** land in
     `search_memory` as it completes;
   - re-importing the same session later is **incremental** (only new records
     are processed; the gist folds forward — nothing duplicates).

4. If the script prints an auth error, no setup is needed — the script runs the same
   OAuth flow as /mcp and will open the browser ONCE for approval (token is
   cached after that). If it can't find the session id, ask the user for the
   transcript path.

Never use curl or raw HTTP; never pass transcript content as tool arguments.
