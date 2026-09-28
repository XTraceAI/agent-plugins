---
description: Use when the user asks what the team knows, decided, discussed, or saved about a topic, or wants to check MemHub/team memory (e.g. "what do we know about X", "did we decide on Y", "search memhub for Z", "is there a spec for W"). Read-only — searches facts, episodes, artifacts, and documents.
argument-hint: <what to look for>
allowed-tools: mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__read_memory, mcp__plugin_memhub-staging_memhub__read_memory, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_tags, mcp__plugin_memhub-staging_memhub__list_tags, mcp__plugin_memhub_memhub__search_all_brains, mcp__plugin_memhub-staging_memhub__search_all_brains, Bash
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
   - `memory_type`: `"artifacts"` (**the default**) | `"facts"` | `"episodes"`
     | `"documents"` | `"all"`. Artifacts are the documents the team wrote —
     specs, briefs, runbooks, review records — and are what a question about
     how something works is usually answered by, which is why they are the
     default. Ask for another kind when the question is about that kind:
     `"episodes"` for what happened in a past session, `"facts"` for a
     remembered lesson or decision, `"documents"` for the chunked text of an
     ingested file, `"all"` to search every kind (interleaved one rank at a
     time). **Omitting `memory_type` does NOT search everything** — a question
     about a past decision needs `"facts"` or `"all"` named explicitly.
   - `top_k`: raise from the default 8 (max 50) when the user wants everything
     on a topic.
   - `agent_brain_id`: **in a repo with an agent brain, search that brain
     first.** The SessionStart brief names it (`MemHub: this repo's agent brain
     is …`); `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show --json`
     prints the cached entry (`brain_id`, and `org_id` when known) if the brief
     is not in context. Pass that `org_id` alongside `agent_brain_id` — here and
     on `read_memory`: a brain is resolved inside one org, and without it a room
     outside the default org fails with "Agent brain not found". That brain is
     where this repo's sessions are captured, so it is where the answer usually
     is — omitting it searches personal memory instead and reads as "the team
     never wrote that down".
     Then run the SAME query again WITHOUT `agent_brain_id` and merge: widen,
     never replace. Personal workspace memory holds things the repo brain does
     not, and silently dropping it is the failure this default exists to fix,
     in the other direction. Skip the second call only when the user asked
     about the repo/team specifically.
     When the user names a DIFFERENT brain, resolve it via `list_agent_brains`
     and search that one instead. When you don't know which brain holds it,
     `search_all_brains` searches every brain you can read in one call.
   - `tags` (+ `match`: `"all"`/`"any"`): narrows to artifacts carrying the
     tag(s) — check the vocabulary with `list_tags` first. Note that a tag
     filter restricts results to artifacts only. Tags are stored normalised
     (lowercase, non-alphanumeric runs → `_`, 64 chars max): a spec saved
     with `spec:retry-policy` is listed as `spec_retry_policy`. The filter
     normalises your input the same way, so either spelling matches.
   - `created_after` / `created_before`: ISO-8601 bounds on when the memory
     was *captured* (not when the underlying event happened).
   - `group` / `author`: artifact filters that mirror the brain's Index (see
     `get_brain_overview`) — `group` is one of `"Specs"`, `"Runbooks"`,
     `"Design"`, `"Briefs"`, `"Routine output"`, `"Documents"`; `author` is a
     teammate's display name. Both narrow to artifacts. `author` naming anyone
     but you needs `agent_brain_id` — personal memory holds only your own, so
     the call errors rather than returning nothing.
   - **Browsing** — leave `query` EMPTY to list rather than search: with the
     artifacts default (or `group`/`tags`) you get the matching artifacts
     newest first, `top_k` per page, `offset=top_k` for the next page. This is
     how you walk an Index group past the `… +N more` line. A page shorter than
     `top_k` is the last one. `offset` WITH a query is an error — a search is
     ranked, not paged.
2. **Read the item shape you got back — there are two of them.** Either
   `{id, type, content, score}`, where `content` is the body (16,000-char cap,
   with `truncated`), or a POINTER: `{id, kind, title, abstract, author,
   as_of, status, links, tags, score}` plus, for an artifact, up to 3 ranked
   `sections`. Tell them apart by whether `title` is present, and branch on
   `kind`/`title` rather than on `type` — `type` means the kind in the first
   shape and the item's own type word ("spec", "lesson") in the second. A
   browse always returns the pointer shape.

   A pointer carries no body, so **open the one you picked with `read_memory`**
   — `read_memory(id, agent_brain_id=…)` works for a fact, an episode, an
   artifact or a document chunk. For a long artifact call it once bare for the
   OUTLINE (one line per section with a `section_id`), then again with
   `section_id=…` for the one section that answers the question; that is
   normally a tenth of the tokens of the whole document. Do not answer from
   abstracts alone, and do not reach for `include_content=True` to dodge the
   second call — it brings every hit's body back at once, which is what
   pointers exist to avoid. It is the right escape hatch only when you truly
   need several bodies at speed.
3. If the first search comes back thin, retry once or twice with a rephrased
   query or a different `memory_type` before concluding the memory isn't there.
4. Answer the user's question from the results, citing which memories support
   it (type + a short quote from what you actually opened). Mention the
   returned `scope` so they know where the search ran — and when you searched
   both, say which hits came from the
   repo's brain and which from personal memory, because "the team decided this"
   and "I noted this once" are different claims. If nothing relevant exists,
   say so plainly — do not pad with loosely related hits.

Plain-English output only: never surface internal ids, scores, or field names
unless the user asks for them.
