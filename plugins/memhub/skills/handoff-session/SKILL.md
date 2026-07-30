---
description: Use when the user wants to hand off the current session/work to a teammate via MemHub (e.g. "hand this off to Alice", "handoff this session to Bob", "share my context with Carol so she can pick this up", "pass this work to X"). Creates a shareable agent brain holding a handoff brief plus the full session's extracted memory, and shares it read-only with the teammate.
argument-hint: <teammate> [title...]
allowed-tools: mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__save_artifact, mcp__plugin_memhub-staging_memhub__save_artifact, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__share_agent_brain, Bash
---

Hand the current session off to a teammate: bundle a concise handoff brief and
the session's full extracted memory into one agent brain and share it
read-only. The teammate's agent then picks it up by searching that agent
brain — no transcript pasting, no shoulder-tap walkthrough.

Arguments: `$ARGUMENTS`
- First token(s) = the teammate, by name or email (required). If missing, ask
  who to hand off to.
- Remaining text = an optional handoff title. If omitted, derive a short one
  from what this session worked on (e.g. "Flush hook OAuth migration").

Do exactly this:

1. Resolve the teammate: call `list_teammates` and match name/email
   case-insensitively. If nobody matches or several do, show the candidates
   and ask — never guess between two people.

2. Create the handoff container: `create_agent_brain` with
   `name: "Handoff: <title>"` and a one-line `description` naming who it's
   from, who it's for, and the topic. Omit `workspace_id` (your own workspace
   — as creator you keep the contributor access that sharing requires).

3. Write the handoff brief and save it with `save_artifact` into that agent
   brain (`agent_brain_id` from step 2, `artifact_type: "document"`,
   `tags: ["handoff"]`, `name: "Handoff brief: <title>"`). Compose it from
   the current conversation — this is the one document the teammate reads
   first, so keep it tight:
   - **Goal** — what the work is trying to achieve and for whom.
   - **Current state** — what's done, what's in flight, what's untouched.
   - **Key decisions** — choices made and the why behind each.
   - **Next steps** — concrete, ordered, smallest-first.
   - **Gotchas** — blockers, dead ends already tried, surprising constraints.
   - **Pointers** — repos, branches, PRs, files, dashboards (absolute
     paths/URLs; the reader is on a different machine).

   Composing this content yourself is the point here — this is NOT the
   file-upload case the save-artifact skill guards against.

4. Share it: `share_agent_brain` with the agent brain id and the teammate's
   `user_id`. Read-only is what you get and all a handoff needs.

5. Ship the full session into the same agent brain via Bash — one command:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
     --session "<current-session-id-or-path>" \
     --agent-brain-id "<id-from-step-2>" \
     --conversation-id "$(uuidgen)" \
     --title "Handoff: <title>"
   ```

   - Current session = the most recently modified `.jsonl` sitting DIRECTLY
     inside the `~/.claude/projects/` directory matching the current working
     directory (top level only — `.jsonl` files in subdirectories are
     subagent/workflow transcripts, not sessions).
   - The fresh `--conversation-id` is REQUIRED: the flush hook has usually
     already imported this session globally under its own id, and re-imports
     dedup per conversation_id — without a fresh id nothing would land in the
     handoff agent brain.
   - Do NOT read or paste transcript content; the script ships any size and
     auto-chunks. It waits on extraction, so it can run for minutes —
     unattended, just let it finish.

6. Report back: the agent brain name, who it's shared with, and the
   receiving line the user can send their teammate verbatim — e.g.:

   > Ask your agent: *search the "Handoff: <title>" agent brain in memhub*

   Note that the handoff brief is readable immediately, while facts, episodes,
   artifacts, and the session gist from the full import land over the next few
   minutes as extraction completes.

If `share_agent_brain` fails on permissions, you don't have contributor
access to the agent brain — this happens when reusing someone else's agent
brain instead of creating one in step 2; create your own and retry.
