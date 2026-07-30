---
description: Use when the user asks what the team knows, decided, discussed, or saved about a topic, or wants to check MemHub/team memory (e.g. "what do we know about X", "did we decide on Y", "search memhub for Z", "is there a spec for W"). Read-only — searches facts, episodes, artifacts, and documents.
argument-hint: <what to look for>
allowed-tools: mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_tags, mcp__plugin_memhub-staging_memhub__list_tags
---

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
   - `agent_brain_id`: only when the user names a specific agent brain —
     resolve it via `list_agent_brains` first. Omit to search their own
     workspace memory.
   - `tags` (+ `match`: `"all"`/`"any"`): narrows to artifacts carrying the
     tag(s) — check the vocabulary with `list_tags` first. Note that a tag
     filter restricts results to artifacts only.
   - `created_after` / `created_before`: ISO-8601 bounds on when the memory
     was *captured* (not when the underlying event happened).
2. If the first search comes back thin, retry once or twice with a rephrased
   query or a different `memory_type` before concluding the memory isn't there.
3. Answer the user's question from the results, citing which memories support
   it (type + a short quote). Mention the returned `scope` so they know where
   the search ran. If nothing relevant exists, say so plainly — do not pad with
   loosely related hits.

Plain-English output only: never surface internal ids, scores, or field names
unless the user asks for them.
