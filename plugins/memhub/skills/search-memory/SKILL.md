---
description: Use when the user asks what the team knows, decided or wrote down about something, or what they themselves did in an earlier session (e.g. "what do we know about X", "did we decide on Y", "is there a spec for Z", "what did I do on this last week", "search memhub for W"). Read-only. Looks in the team's shared brain and in the user's own memory, opens the best match, and says which one the answer came from — or plainly that nothing was found.
argument-hint: "<what to look for>"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show *), mcp__plugin_memhub_memhub__get_brain_overview, mcp__plugin_memhub-staging_memhub__get_brain_overview, mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__read_memory, mcp__plugin_memhub-staging_memhub__read_memory, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_sessions, mcp__plugin_memhub-staging_memhub__list_sessions'
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Answer `$ARGUMENTS` (or what the user just asked) from MemHub. Read-only. The
tool schemas document every parameter; this is the judgment they don't carry.
Climb a ladder, cheapest rung first, and never answer from a rung that only
points at the answer:

1. **Find the repo's brain, then read its map.** The session-start brief names
   it (`MemHub: this repo's agent brain is …`); otherwise
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show --json` prints the
   cached `brain_id`, or `list_agent_brains(repo="<org>/<name>")` finds it. For
   another brain the user names, `list_agent_brains(query="…")`; when you don't
   know which brain holds it, search with `all_brains=true`. Call
   `get_brain_overview(agent_brain_id)` — a question its Index already answers
   needs no search.
2. **Search for pointers.** `search_memory` returns POINTERS only, never a
   body; `kind`: `"artifact"` is the default. Run the SAME query twice and
   merge: once with `agent_brain_id`, once without it (personal memory). Every
   captured session's gist and episodes live in personal memory, never in a
   brain — skip that call and the answer reads as "the team never wrote that
   down". **Episodes are never searched with `agent_brain_id`** (refused), so
   `kind="episode"` goes in the personal call. For "what did I do last week /
   on that PR", start from `list_sessions`. Browsing (empty `query`) is for
   walking an Index group past its `… +N more` line, not for "what's new":
   `as_of` stamps can be refreshed by batch jobs, so newest-first is not
   recent work. A thin result: rephrase once or twice before concluding.
3. **Open the one you picked with `read_memory(id)`.** Open ONE; never answer
   from an abstract. For a long artifact call it bare first for the outline,
   then again with the one `section_id` that answers the question. Don't use
   `include_content=true` to skip this step.

Answer from what you opened, with a short quote, and say where each piece came
from — the team's brain or the user's personal memory: "the team decided" and
"I noted this once" are different claims. If nothing relevant exists, say so
plainly; don't pad with loosely related hits. Plain English: no ids, scores or
field names unless asked.
