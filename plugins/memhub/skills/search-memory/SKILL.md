---
description: Use when the user asks what the team knows, decided, discussed, or saved about a topic, or wants to check MemHub/team memory (e.g. "what do we know about X", "did we decide on Y", "search memhub for Z", "is there a spec for W"). Read-only — walks a brain's overview, searches artifacts, sessions and documents, and opens what it finds.
argument-hint: "<what to look for>"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show *), mcp__plugin_memhub_memhub__get_brain_overview, mcp__plugin_memhub-staging_memhub__get_brain_overview, mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub_memhub__read_memory, mcp__plugin_memhub-staging_memhub__read_memory, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_tags, mcp__plugin_memhub-staging_memhub__list_tags, mcp__plugin_memhub_memhub__list_sessions, mcp__plugin_memhub-staging_memhub__list_sessions'
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Search MemHub team memory and report what it holds about the user's topic.
Read-only: this skill never writes or modifies memory.

Arguments: `$ARGUMENTS` — what to look for, in natural language. If empty,
derive the query from what the user just asked.

Do exactly this — a ladder, cheapest rung first: **overview → pointers →
open one**. Never answer from a rung that only points at the answer.

1. **Find the brain, then read its map.** In a repo with an agent brain,
   search that brain first. The SessionStart brief names it (`MemHub: this
   repo's agent brain is …`); `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show --json`
   prints the cached `brain_id` if the brief is not in context, and
   `list_agent_brains(repo="<org>/<name>")` finds it on the server. When the
   user names a DIFFERENT brain, `list_agent_brains(query="…")` ranks the
   brains you can read. Then call `get_brain_overview(agent_brain_id)`: its
   Index groups the brain's artifacts (Specs, Runbooks, Design, …) with
   counts, and its lines say `read_memory(id)` for the ones worth opening
   directly. A question the Index already answers needs no search.
2. **Search for pointers.** Call `search_memory` with a natural-language
   `query` (phrase it as the thing you want to find, not keywords). It returns
   POINTERS only — `{id, kind, title, abstract, author, as_of, links, tags,
   score}` plus, for an artifact, up to 3 ranked `sections` — never a body.
   Useful parameters:
   - `kind`: `"artifact"` (**the default**) | `"document"` | `"episode"` |
     `"all"`. Artifacts are the documents the team wrote — specs, briefs,
     runbooks, review records — and are what a question about how something
     works is usually answered by. `"episode"` for what happened in one of
     YOUR past sessions, `"document"` for the chunked text of an ingested
     file, `"all"` for every kind. There are no facts to search.
   - `agent_brain_id`: the brain from step 1. Omitting it searches your
     personal memory instead and reads as "the team never wrote that down".
     Then run the SAME query again WITHOUT `agent_brain_id` and merge: widen,
     never replace. Personal memory holds what the repo brain does not —
     including every captured session's gist and episodes, which go to
     personal memory, never to a brain. **Episodes are never searched with
     `agent_brain_id`** (the call is refused): `kind="episode"` always goes
     in the personal call. Skip the second call only when the user asked
     about the repo/team specifically.
   - `all_brains=true`: when you don't know which brain holds it, searches
     every brain you can read in one call (use instead of `agent_brain_id`).
     `folder` narrows that to the brains filed under one folder.
   - `top_k`: raise from the default 8 (max 50) when the user wants everything
     on a topic.
   - `tags` (+ `match`: `"all"`/`"any"`): narrows to artifacts carrying the
     tag(s) — check the vocabulary with `list_tags` first. Tags are stored
     normalised (lowercase, non-alphanumeric runs → `_`, 64 chars max): a spec
     saved with `spec:retry-policy` is listed as `spec_retry_policy`. The
     filter normalises your input the same way, so either spelling matches.
   - `created_after` / `created_before`: ISO-8601 bounds on when the memory
     was *captured* (not when the underlying event happened).
   - `group` / `author`: artifact filters that mirror the brain's Index —
     `group` is one of `"Specs"`, `"Runbooks"`, `"Design"`, `"Briefs"`,
     `"Routine output"`, `"Documents"`; `author` is a teammate's display name.
     `author` naming anyone but you needs `agent_brain_id` — personal memory
     holds only your own.
   - `session_id`: the episodes of one session. For "what did I do last
     week / in that repo / on that PR", list the sessions first with
     `list_sessions(since=…, until=…, repo=…, pr_url=…)` and open one with
     `read_memory(<session id>)`.
   - **Browsing** — leave `query` EMPTY to list rather than search: you get
     the matching items of any `kind` newest first, `top_k` per page,
     `offset=top_k` for the next page. This is how you walk an Index group
     past the `… +N more` line. A page shorter than `top_k` is the last one.
     `offset` WITH a query is an error — a search is ranked, not paged.
3. **Open the one you picked with `read_memory(id)`.** A pointer carries no
   body. The kind comes from the id — the same call opens an artifact (with
   its version header), a document chunk, an episode, or a whole session by
   session id; include `agent_brain_id` when the id came from a brain search. For
   a long artifact call it once bare for the OUTLINE (one line per section
   with a `section_id`), then again with `section_id=…` for the one section
   that answers the question; that is normally a tenth of the tokens of the
   whole document. Do not answer from abstracts alone, and do not reach for
   `include_content=true` on the search to dodge this step — it brings every
   hit's body back at once, which is what pointers exist to avoid. It is the
   right escape hatch only when you truly need several bodies at speed.
4. If the first search comes back thin, retry once or twice with a rephrased
   query or a different `kind` before concluding the memory isn't there.
5. Answer the user's question from what you opened, citing which memories
   support it (kind + a short quote). Say where each hit came from — the
   repo's brain or your personal memory — because "the team decided this"
   and "I noted this once" are different claims. If nothing relevant exists,
   say so plainly — do not pad with loosely related hits.

Plain-English output only: never surface internal ids, scores, or field names
unless the user asks for them.
