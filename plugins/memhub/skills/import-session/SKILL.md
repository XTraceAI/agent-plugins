---
description: Use when the user asks to import, upload, or save a Claude Code, Codex, or Cursor session/conversation/transcript into MemHub or team memory (e.g. "import this session into memhub", "save session abc123 to memhub", "put that conversation in an agent brain"). Ships the transcript via a terminal upload script — any size, no token-by-token re-emit.
argument-hint: "<session-id-or-path> [title...]"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" import *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" list *), mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs'
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing the `scripts/` directory and a plugin manifest
(`.claude-plugin/plugin.json` or `plugin.json`). A trusted host
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

1. **It goes to the user's personal memory — never to a brain.** A session is
   the author's: the transcript lands in their Sessions view, and its **gist**
   and task **episodes** (never facts, never directives) land in their
   personal memory. The repo's room is not an option, and neither is a brain
   the user names: the import takes no brain. If the user asked to "put it in
   a brain", say so plainly and offer what does reach a brain — saving a
   document with `/memhub:save-artifact`, or `/memhub:handoff-session` to pass
   the work to a teammate. For an account in several orgs, `--org-id` picks
   whose personal memory (`list_orgs` names them); omitted, the default org.

2. Run the import in the terminal — one command, substitute the real values:

   ```bash
   python3 "<plugin-root>/scripts/capture.py" import \
     --session "<session-id-or-path>" --host auto [--title "<title>"] \
     [--org-id "<org-id>"]
   ```

   For the literal ref `latest`, replace `--host auto` with the explicit current
   host.
   NEVER pass `--conversation-id`. Omitted, Claude uses the session id and
   Codex/Cursor use the same host-prefixed id as automatic capture. That keeps
   one conversation per session and makes re-imports incremental. A fresh id would
   split the session's memory. If nothing new lands, automatic capture already
   did its job; do not work around that with a new id.
   Very large transcripts are AUTO-CHUNKED (default threshold ~3.5MB): the
   script sends disjoint slices sequentially under one conversation_id and
   waits for each slice's extraction (the session gist folding forward)
   before the next — payloads beyond ~8MB fail server-side as one shot, so
   never disable chunking for huge sessions. This is slow but unattended;
   just let the command run.

3. Report back the returned `conversation_id`, `path` (should be
   `"agentic"`), `messages_received`, and `scope`, plus the platform from the
   script's `source platform :` line (the response does not echo it). Tell the
   user:
   - **where its memory landed** — their personal memory, with the
     transcript in their Sessions view; not the repo's brain;
   - extraction runs in the background (allow several minutes for large
     sessions) — as it completes, `list_sessions` lists the session,
     `read_memory(<conversation_id>)` opens it, and
     `search_memory(kind="episode", session_id=<conversation_id>)` finds its
     gist and episodes. Don't promise facts or directives from it: an import
     creates none;
   - re-importing the same session later is **incremental** (only new records
     are processed; the gist folds forward — nothing duplicates).

4. If the script prints an auth error, no setup is needed — it opens the browser
   ONCE for approval and then mints a personal access key (`mhk_…`) that later
   runs and the capture hooks reuse without a browser. `/memhub:login` does the
   same thing deliberately if you would rather provision it up front. This is
   the plugin's own credential, not the `/mcp` connector's, so being connected
   in `/mcp` will not satisfy it. If it can't find the session id, ask the user
   for the transcript path.

Never use curl or raw HTTP; never pass transcript content as tool arguments.
