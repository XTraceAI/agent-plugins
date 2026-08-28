---
description: Use when the user asks what the team knows, decided, discussed, or saved about a topic, or wants to check MemHub/team memory (e.g. "what do we know about X", "did we decide on Y", "search memhub for Z", "is there a spec for W"). Read-only — searches facts, episodes, artifacts, and documents.
argument-hint: <what to look for>
allowed-tools: mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_tags, mcp__plugin_memhub-staging_memhub__list_tags, Bash
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Search MemHub team memory and report what it holds about the user's topic.
Read-only: this skill never writes or modifies memory.

Arguments: `$ARGUMENTS` — what to look for, in natural language. If empty,
derive the query from what the user just asked.

Do exactly this:

1. Call the `search_memory` MCP tool with a natural-language `query` (phrase it
   as the thing you want to find, not keywords). Useful parameters:
   - `memory_type`: `"all"` (default) | `"facts"` | `"artifacts"` |
     `"episodes"` | `"documents"`. Use `"artifacts"` when the user wants a
     saved doc/spec; `"documents"` to search inside the chunked text of
     ingested files.
   - `top_k`: raise from the default 8 (max 50) when the user wants everything
     on a topic.
   - `agent_brain_id`: **in a repo with an agent brain, search that brain
     first.** The SessionStart brief names it (`MemHub: this repo's agent brain
     is …`); `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show` prints
     the id if the brief is not in context. That brain is where this repo's
     sessions are captured, so it is where the answer usually is — omitting it
     searches personal memory instead and reads as "the team never wrote that
     down".
     Then run the SAME query again WITHOUT `agent_brain_id` and merge: widen,
     never replace. Personal workspace memory holds things the repo brain does
     not, and silently dropping it is the failure this default exists to fix,
     in the other direction. Skip the second call only when the user asked
     about the repo/team specifically.
     When the user names a DIFFERENT brain, resolve it via `list_agent_brains`
     and search that one instead.
   - `tags` (+ `match`: `"all"`/`"any"`): narrows to artifacts carrying the
     tag(s) — check the vocabulary with `list_tags` first. Note that a tag
     filter restricts results to artifacts only. Tags are stored normalised
     (lowercase, non-alphanumeric runs → `_`, 64 chars max): a spec saved
     with `spec:retry-policy` is listed as `spec_retry_policy`. The filter
     normalises your input the same way, so either spelling matches.
   - `created_after` / `created_before`: ISO-8601 bounds on when the memory
     was *captured* (not when the underlying event happened).
2. If the first search comes back thin, retry once or twice with a rephrased
   query or a different `memory_type` before concluding the memory isn't there.
3. Answer the user's question from the results, citing which memories support
   it (type + a short quote). Mention the returned `scope` so they know where
   the search ran — and when you searched both, say which hits came from the
   repo's brain and which from personal memory, because "the team decided this"
   and "I noted this once" are different claims. If nothing relevant exists,
   say so plainly — do not pad with loosely related hits.

Plain-English output only: never surface internal ids, scores, or field names
unless the user asks for them.
