---
description: Use when the user asks to import, upload, or save a Claude Code, Codex, or Cursor session/conversation/transcript into MemHub or team memory (e.g. "import this session into memhub", "save session <id> to memhub", "put that conversation in an agent brain"). Ships the transcript via a terminal upload script — any size, no token-by-token re-emit.
argument-hint: <session-id-or-path> [title...]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing `plugin.json` and the `scripts/` directory. A trusted host
variable such as `CLAUDE_PLUGIN_ROOT` or `CURSOR_PLUGIN_ROOT` may already point
there; use it only when it resolves to that same ancestor. Substitute the
resulting absolute path as `<plugin-root>` below; do not infer it from the
workspace cwd. Commands show `python3`; on native Windows use `py -3`.

Import a past Claude Code, Codex, or Cursor session into MemHub team memory on
demand. The unified capture script locates the host session, normalizes it when
needed, and ships it to the `import_conversation` MCP tool — **do NOT call the
MCP tool yourself and do NOT read or paste transcript content**; sessions can
exceed a million tokens and the script handles any size without putting the
transcript in model context. This is a terminal operation.

Arguments: `$ARGUMENTS`
- First token = a session id or native session path (required). Paths and bare
  ids are host-detected. A bare id is accepted only when exactly one of Claude,
  Codex, or Cursor owns it; cross-host collisions are refused.
- Remaining text = an optional conversation title.
- If invoked without arguments (e.g. the user said "import this session"), ask
  which session they mean. For "this/the current session", determine the
  current host and run `capture.py list --host <claude|codex|cursor> --limit 20`;
  select the current session only when its id or cwd is unambiguous, otherwise
  ask. The literal ref `latest` always requires an explicit current host.

Do exactly this:

1. **Resolve the destination — default to the repo's room.** A session about a
   repo belongs in that repo's brain, where teammates and future sessions can
   find it; raw workspace memory is the fallback, not the default.
   - **Check the cache first** — `python3
     "<plugin-root>/scripts/room_map.py" show` prints the room's brain
     id when the repo has one. The import script reads that same cache, so on a
     cached repo you can simply omit `--agent-brain-id` and let it route.
   - Nothing cached → derive `Repo: <org>/<name>` from `git remote get-url
     origin` (host and `.git` stripped), then `list_agent_brains` →
     **exact-name match**. Found → use its `agent_brain_id`, and persist it
     (`room_map.py set --brain-id <id> --org-id <org-id>` — the org id is the
     one you passed to `list_agent_brains`, or the default org's from
     `list_orgs`; the response's `scope` carries only `org_name`) so later
     writers route without repeating this lookup.
   - **No match, or not in a git repo → do NOT create a brain.** Import into
     workspace memory (pass `--no-room`) and say so, mentioning that
     `/memhub:onboard` sets up the repo's room if they want one.
   - The user naming a brain explicitly always wins over all of the above.
   - Edge cases (SSH remotes, no remote, worktrees) and the cache's rules are in
     `<plugin-root>/references/repo-brain.md`.

2. Run the import in the terminal — one command, substitute the real values:

   ```bash
   python3 "<plugin-root>/scripts/capture.py" import \
     --session "<session-id-or-path>" --host auto [--title "<title>"] \
     [--agent-brain-id "<id>"]

   For the literal ref `latest`, replace `--host auto` with the explicit current
   host. Pass `--agent-brain-id` only when step 1 resolved a room the cache did not
   already hold, or when the user named a brain explicitly; a cached repo
   routes on its own. Use `--no-room` for the workspace-memory fallback.
   NEVER pass `--conversation-id`. Omitted, Claude uses the session id and
   Codex/Cursor use the same host-prefixed id as automatic capture. That keeps
   one conversation per room and makes re-imports incremental. A fresh id would
   split the session's memory. If nothing new lands, automatic capture already
   did its job; do not work around that with a new id.
   Very large transcripts are AUTO-CHUNKED (default threshold ~3.5MB): the
   script sends disjoint slices sequentially under one conversation_id and
   waits for each slice's extraction (the session gist folding forward)
   before the next — payloads beyond ~8MB fail server-side as one shot, so
   never disable chunking for huge sessions. This is slow but unattended;
   just let the command run.
   ```

3. Report back the returned `conversation_id`, `source_platform`, `path` (should
   be `"agentic"`), `messages_received`, and scope. Tell the user:
   - **where it landed, by name** — "imported into `Repo: <org>/<name>`" or
     "imported into your workspace memory" — so a wrong destination is
     obvious now rather than weeks from now;
   - extraction runs in the background (allow several minutes for large
     sessions) — facts, episodes, artifacts, and the session **gist** land in
     `search_memory` as it completes;
   - re-importing the same session later is **incremental** (only new records
     are processed; the gist folds forward — nothing duplicates).

4. If the script prints an auth error, no setup is needed — it opens the browser
   ONCE for approval and then mints a personal access key (`mhk_…`) that later
   runs and the capture hooks reuse without a browser. `/memhub:login` does the
   same thing deliberately if you would rather provision it up front, and the
   MCP tools use the same key. If it can't find the session id, ask the user
   for the transcript path.

Never use curl or raw HTTP; never pass transcript content as tool arguments.
