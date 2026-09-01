---
description: Use when the user wants to hand off the current session/work to a teammate via MemHub (e.g. "hand this off to Alice", "handoff this session to Bob", "share my context with Carol so she can pick this up", "pass this work to X"). Creates a shareable agent brain holding a handoff brief, and shares it read-only with the teammate along with the repo room where per-turn capture already extracted the session.
argument-hint: <teammate> [title...]
allowed-tools: mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__save_artifact, mcp__plugin_memhub-staging_memhub__save_artifact, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__share_agent_brain, Bash
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Hand the current session off to a teammate: write a concise handoff brief into
a shareable agent brain and share it read-only, alongside the repo room where
per-turn capture has already extracted this session. The teammate's agent picks
it up by searching — no transcript pasting, no re-import, no shoulder-tap
walkthrough.

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
   from, who it's for, and the topic. Omit `workspace_id` — that chooses where
   the brain lives, not who can read it, and you are its creator (so admin on
   it) wherever it is homed. Either way it is readable by you alone until
   step 4 shares it; putting it in a shared workspace would NOT share it.

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

4. Share it: `share_agent_brain` with the agent brain id and
   `teammate_user_id` = the teammate's `user_id` from `list_teammates`.
   `permission` defaults to `viewer` — read-only, all a handoff needs.

5. Give the teammate the session's memory — do NOT import the session.
   Per-turn capture has been shipping this session into the repo's room since
   it started, and the server has already extracted it. Re-importing would
   re-upload the transcript and, into a second brain, extract every fact and
   episode a second time — the same session's memory in two places, competing
   in retrieval.

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show
   ```

   - A room is cached → `share_agent_brain` that room with the teammate too,
     read-only. That is where the session's facts, episodes and gist already
     live; the handoff brain carries the brief that points into it.
   - No room cached (`/memhub:onboard` never run here), or the session ran
     before capture was working → say so plainly and tell the user to run
     `/memhub:import-session`. That is the ONE skill that backfills a session,
     and it imports under the session's own id so nothing is duplicated.

6. Report back: the agent brain name, who it's shared with (brief brain and
   repo room), and the receiving line the user can send their teammate
   verbatim — e.g.:

   > Ask your agent: *search the "Handoff: <title>" agent brain in memhub*

   Both are readable immediately — the brief because you just wrote it, the
   session's memory because capture extracted it as the session ran.

If `share_agent_brain` fails on permissions, you lack contributor access to
whichever brain you were sharing — and the fix differs by which one:

- **Step 4 (the handoff brain)** — you created it, so this should not happen;
  if it does, you reused someone else's brain instead of creating one in step
  2. Create your own and retry.
- **Step 5 (the repo room)** — expected, and NOT something to work around.
  The room is usually a teammate's, shared with you read-only. Do not create a
  second one: minting another `Repo: <org>/<name>` forks the repo's memory.
  Say you could only share the handoff brain, and tell the user the room's
  owner (or `/memhub:onboard`) can share the room itself.
