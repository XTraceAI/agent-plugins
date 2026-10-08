---
description: Use when the user wants to hand their current work to one or more teammates so they can pick it up (e.g. "hand this off to Alice", "hand off this session to Bob", "pass this work to Carol and Dan", "share my context with Alice so she can continue", "write a handoff for Bob"). Writes a handoff brief — goal, current state, decisions, next steps, gotchas — into a shared agent brain the teammates search from their own agent. The same set of people always reuses one standing handoff brain, so the history between them stays in one place; a separate one-off brain only when asked. The session transcript itself is never shared.
argument-hint: "<teammate>[, <teammate>...] [title...] [--new]"
allowed-tools: 'Bash(git config user.name), Bash(git remote get-url origin), mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__list_agent_brain_access, mcp__plugin_memhub-staging_memhub__list_agent_brain_access, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__save_artifact, mcp__plugin_memhub-staging_memhub__save_artifact, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__share_agent_brain, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs'
---

A handoff is one artifact: a brief you write from this conversation, uploaded
into the **handoff channel** for exactly the people receiving it. The
teammate's agent finds it by searching that brain. Nothing else moves — never
import or share the session (a session is never brain content), so the brief
must stand on its own.

**One channel per set of people.** You↔Alice is one brain; You↔Alice↔Bob is
another. Every handoff between the same people lands in the same brain as its
own brief, so the channel keeps the running history between them instead of a
pile of one-shot brains. The channel is
shared as **contributor**, so either side hands work back through it.

Arguments: `$ARGUMENTS`
- Leading name(s) = the teammate(s), by name or email (required), separated by
  commas, `+` or "and". Missing → ask who. Can't tell where the names end and
  the title begins → ask.
- Remaining text = an optional title. Omitted → derive a short one from what
  this session worked on (e.g. "Flush hook OAuth migration").
- `--new`, or the user asking for a separate / dedicated brain → skip the
  channel and make a one-off brain (step 2b).

Do exactly this. A brain and its people live in exactly one org. Every call
that takes an `agent_brain_id` works the org out from that id — never pass an
`org_id` alongside one. For a single-org account that is the whole story. In
several orgs (`list_orgs`), pick the one the teammates are in (ask if it's
unclear) and pass its `org_id` to the calls that have no brain id yet —
`list_teammates`, `list_agent_brains`, `create_agent_brain` — wherever the
tool's schema offers it.

1. **Resolve the teammates.** `list_teammates` (it never lists you), match
   each name/email case-insensitively. No match or several → show the
   candidates and ask; never guess between two people. Their `user_id`s are
   the set **T**.

2. **Find or create the brain.**

   a. *Find the channel* (skip with `--new`). `list_agent_brains`; the
      candidates are brains named `Handoffs: …`. For each, call
      `list_agent_brain_access`. Its reply names the brain's creator as
      `created_by` (the creator holds no grant, so is not in `access`) and
      its grantees in `access`. The brain's members **M** are the
      `created_by` user (when not null — null means the creator has left the
      org) plus every `access` user. `list_teammates` never lists you, so a
      member id it does not list is yours. The brain is the channel when
      **M** is exactly you plus **T**: every id in **T** is in **M**, and the
      one other id in **M** is yours.
      Decide on members, not on the name after the prefix — the other person
      may have created it under their own spelling. Several match → use the
      oldest and mention the others. A match where you are not its creator
      and your own `access` row is `viewer` can't take the brief — tell the
      user to ask its creator or an admin for contributor, or use `--new`.

      If a reply has no `created_by` key at all (an older MemHub server that
      does not name a brain's creator), members can't be known, so don't
      guess: tell the user an existing handoff channel can't be matched on
      this server, then create a new channel (step 2b).

   b. *Create* (no channel found, or `--new`). `create_agent_brain`, omitting
      `workspace_id` (your own workspace; as creator you can share it):
      - channel → `name: "Handoffs: <you> ↔ <teammate> [↔ <teammate>…]"`,
        first names, yours from `git config user.name` (a label only), and
        `description: "Standing handoff channel between <full names>: one
        brief per handed-off task."`
      - `--new` → `name: "Handoff: <title>"` and a one-line description
        naming who it's from, who it's for, and the topic.

      Then share it with everyone in **T** in ONE call:
      `share_agent_brain(agent_brain_id, permission, teammates=[…every
      user_id in T…])` — `permission` is `"contributor"` for a channel,
      `"viewer"` for a `--new` brain. Never one call per person. A reused
      channel needs no sharing — matching it proved they already have it.

3. **Upload the brief** with `save_artifact` into that brain:
   `name: "Handoff brief: <title>"`, `artifact_type: "document"`,
   `tags: ["handoff"]`, `topic: <the subject area of the work>` — an existing
   topic of the channel when one fits, a new one for a new area of work, else
   `"unsorted"`; a refusal lists the channel's topics
   (`${CLAUDE_PLUGIN_ROOT}/references/topics.md`). A title already in the channel VERSIONS that brief —
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
