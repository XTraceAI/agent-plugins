---
description: Use when the user wants to hand off the current session/work to a teammate via MemHub (e.g. "hand this off to Alice", "handoff this session to Bob", "share my context with Carol so she can pick this up", "pass this work to X", "hand this to Sebastian and Tristan"). Uploads a handoff brief as an artifact into the standing handoff channel — one shared agent brain per set of people, reused for every handoff between them. A one-off brain only when the user asks for one.
argument-hint: <teammate>[, <teammate>...] [title...] [--new]
allowed-tools: mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_agent_brain_access, mcp__plugin_memhub-staging_memhub__list_agent_brain_access, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__save_artifact, mcp__plugin_memhub-staging_memhub__save_artifact, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__share_agent_brain, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs, Bash
---

A handoff is one artifact: a brief you write from this conversation, uploaded
into the **handoff channel** for exactly the people receiving it. The
teammate's agent finds it by searching that brain. Nothing else moves — never
import or share the session (a session is never brain content), so the brief
must stand on its own.

**One channel per set of people.** Felix↔Sebastian is one brain;
Felix↔Sebastian↔Tristan is another. Every handoff between the same people
lands in the same brain as its own brief, so the channel keeps the running
history between them instead of a pile of one-shot brains. The channel is
shared as **contributor**, so either side hands work back through it.

Arguments: `$ARGUMENTS`
- Leading name(s) = the teammate(s), by name or email (required), separated by
  commas, `+` or "and". Missing → ask who. Can't tell where the names end and
  the title begins → ask.
- Remaining text = an optional title. Omitted → derive a short one from what
  this session worked on (e.g. "Flush hook OAuth migration").
- `--new`, or the user asking for a separate / dedicated brain → skip the
  channel and make a one-off brain (step 2b).

Do exactly this. Every call below runs in ONE org: omit `org_id` for a
single-org account; in several orgs (`list_orgs`), use the one the teammates
are in (ask if it's unclear) and pass that `org_id` to every call — a brain
and its people live in exactly one org.

1. **Resolve the teammates.** `list_teammates` (it never lists you), match
   each name/email case-insensitively. No match or several → show the
   candidates and ask; never guess between two people. Their `user_id`s are
   the set **T**.

2. **Find or create the brain.**

   a. *Find the channel* (skip with `--new`). `list_agent_brains`; the
      candidates are brains named `Handoffs: …`. For each, take the grantee
      ids from `list_agent_brain_access` — active org members only, and never
      the creator, who holds no grant. Drop yourself from them if present
      (the grantee id not in `list_teammates`); call the rest **O**. The
      brain's other members are **O** plus its creator, whom no tool names,
      so match like this:
      - `shared_by` null → you created it: match when **O** = **T**.
      - `shared_by` set → someone else created it (`shared_by` is who
        granted *you* access — the creator or a contributor who re-shared,
        not necessarily the creator). Match when **O** ⊆ **T** and exactly
        one person in **T** is not in **O** — that one is the creator. If
        `shared_by` is a teammate who is not in **O**, they had access
        without a grant, so they are the creator and must be that one
        person.
      Decide on members, not on the name after the prefix — the other person
      may have created it under their own spelling. Several match → use the
      oldest and mention the others. A match where you are only a `viewer`
      can't take the brief — tell the user to ask its creator for
      contributor, or use `--new`.

   b. *Create* (no channel found, or `--new`). `create_agent_brain`, omitting
      `workspace_id` (your own workspace; as creator you can share it):
      - channel → `name: "Handoffs: <you> ↔ <teammate> [↔ <teammate>…]"`,
        first names, yours from `git config user.name` (a label only), and
        `description: "Standing handoff channel between <full names>: one
        brief per handed-off task."`
      - `--new` → `name: "Handoff: <title>"` and a one-line description
        naming who it's from, who it's for, and the topic.

      Then share it with everyone in **T** via `share_agent_brain`:
      `"contributor"` for a channel, `"viewer"` for a `--new` brain. A reused
      channel needs no sharing — matching it proved they already have it.

3. **Upload the brief** with `save_artifact` into that brain:
   `name: "Handoff brief: <title>"`, `artifact_type: "document"`,
   `tags: ["handoff"]`. A title already in the channel VERSIONS that brief —
   right for a follow-up on the same work, wrong for anything else, so make
   the title specific. First line: `From <you> to <teammate(s)>, <YYYY-MM-DD>`
   (plus `, repo <org>/<name>` in a repo). Then, tight:
   - **Goal** — what the work is trying to achieve and for whom.
   - **Current state** — what's done, what's in flight, what's untouched.
   - **Key decisions** — choices made and the why behind each.
   - **Next steps** — concrete, ordered, smallest-first.
   - **Gotchas** — blockers, dead ends already tried, surprising constraints.
   - **Pointers** — repos, branches, PRs, files, dashboards (absolute
     paths/URLs; the reader is on a different machine).

   You compose this content — it is not the file-upload case the
   save-artifact skill guards against.

4. **Report back:** the brain's name and whether it was reused or created,
   who it is shared with and at what level, and a line the user can send
   verbatim:

   > Ask your agent: *search the "<brain name>" agent brain in memhub for "Handoff brief: <title>"*

   The brief is readable the moment `save_artifact` returns.
