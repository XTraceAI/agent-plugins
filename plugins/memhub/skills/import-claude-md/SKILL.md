---
description: Use when the user wants to turn a CLAUDE.md (or any conventions doc) into Rulebook rules — "/memhub:import-claude-md", "import our CLAUDE.md as rules", "re-import the rules from CLAUDE.md". Reads the file HERE in the repo, decomposes it in-agent, and files each rule as a draft through the memhub `create_rule` tool. A re-import updates rules instead of duplicating them.
argument-hint: [path to the doc, default ./CLAUDE.md] [--brain "<rulebook brain name>"] [--dry-run]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__nominate_rule, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__nominate_rule
---

You are importing a conventions document into the team **Rulebook**. This runs
**in the coding agent, not on the server**, on purpose: decomposing a doc into
rules needs the repo (which paths a rule scopes to, whether a pattern matches
real commands here) and the checkout's sha for provenance — neither of which
the server has. You are already reading the file; a second model call
server-side would only lose that context. The server's one job is the
**re-import identity**: it recognises a rule it already holds.

Arguments: `$ARGUMENTS`
- Path (optional, default `./CLAUDE.md`). Must be inside the current repo.
- `--brain "<name>"` (optional) → the rulebook to write into (`agent_brain_id`
  on `create_rule`). Omit to use the repo's own rulebook.
- `--dry-run` → do everything except the `create_rule` calls; show the table.

## The flow — every step is mandatory

### 1. Read the document and pin its provenance

```bash
git rev-parse --show-toplevel; git rev-parse --short HEAD
```

Read the file. Build `source_ref = "<path relative to repo root>@<sha>#<heading-slug>"`
for each rule you derive — the part before `@` is the re-import key, so keep
the path stable across runs (no absolute paths). Repo name = the basename of
the toplevel (e.g. `xmem`) → `scope_repos: ["<repo>"]`.

### 2. Decompose — in your own reading, no model call

Walk the document heading by heading. A **rule** is a conditional a teammate
can violate: *when X, do / never Y, because Z*. Skip narrative, architecture
description, and anything the code already enforces (a linter, a CI check —
say "already enforced by <what>" in the report). For each rule pick ONE
delivery, per the table in `/memhub:create-rule` step 3:

| shape of the rule | delivery | engine block |
|---|---|---|
| a command / edit / tool output has a checkable form | `agent_hook` | `matcher` (`event: bash \| edit \| write \| output`, `command_rx` / `path_rx` / `content_rx`, `*_not_rx`, `warn_once_per`) |
| "run X after edits, before Y" | `agent_hook` | `ordering` |
| applies when a named file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server decides relevance |
| worldview with no trigger at all | `session_context` | none — at most 15 such rules per repo scope are shown at session start; only if no checkable shape exists |

Title = the heading or a short noun phrase (aim for under 60 chars); **one rule
per title** within a run. Statement = the full sentence including the nuance a
reviewer needs (sanctioned forms, exemptions); keep it to a few sentences.

Apply the matcher-authoring rules from `/memhub:create-rule` step 3
(pre-heredoc matching, shape-specific patterns, exemptions in `command_not_rx`
up front, `warn_once_per: "session"` by default), and sanity-check each regex
against a real command from this repo that should fire and one that should not.

### 3. Conflict check against the book — before anything is filed

The server's re-import identity is (rulebook, `source_ref` path before `@`,
title). It adopts a rule filed under the same title with **no** `source_ref`
(an authored draft you are now importing), but it never adopts a rule owned
by another document — a colliding title or matcher from a different source
lands as a **second draft, silently**. So the check happens here, in your
hands, with the candidates and the book both in view.

1. Call the memhub `list_rules` tool (the target brain, every status) and save
   the reply to a file. Write the candidate `create_rule` bodies to another.
2. Run the deterministic pass:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
     --candidates <candidates.json> --existing <list_rules.json> --repo "<repo>"
   ```
   It flags `same_title` (any status, case/punctuation-insensitive),
   `same_matcher` (same event + same primary pattern as an **active** rule —
   two of those fire on the same call) and `anchors_overlap` (with the shared
   identifiers listed). The active book comes from the server's hook view;
   if it is unreachable the script says `active book: unavailable` and you
   still get the title pass.
3. Then the semantic pass, in your own reading — no model call: the script
   prints `judge_by_statement`, every live rule with no deterministic hit, with
   its statement. Read each candidate against that list and mark
   `duplicate` (same trigger, same advice), `contradicts` (same trigger,
   opposite advice) or `distinct`.

What each verdict does to the row:
- `same_title` / `duplicate` on a rule with **no** `source_ref` → re-file under
  that exact title; the server returns `unchanged` or a `proposed` update.
- `same_title` / `same_matcher` / `duplicate` on a rule that **has** a
  `source_ref` from another document → do NOT file a twin by default; show
  the collision and let the user choose (drop the candidate, or file it and
  retire the other by hand).
- `contradicts` → file it as a draft, but say in the table and the report
  which rule it fights and whether that rule is active — the reviewer must
  retire one before activating the other.
- `anchors_overlap` on its own is a hint, not a verdict — judge the
  statements.

### 4. Show the table, then file

Show the user one table: title · delivery · engine · **conflicts** (rule
title + reason + your verdict, or "none") · source_ref. Get a yes (or drop
rows). With `--dry-run`, stop here.

On approval call the memhub **`create_rule`** tool once per row:
`title`, `statement`, `delivery`, the engine block (`matcher` / `ordering` /
`anchors`), `scope_repos`, `source="claude_md_import"`, `source_ref`, and
`agent_brain_id` when `--brain` was given. Read each reply:

- `unchanged: true` → already in the book, nothing written.
- `status: "proposed"` + `supersedes_rule_id` → an update to an existing rule;
  it replaces the old one when a reviewer activates it.
- `status: "draft"` → new.

Every row lands **draft/proposed, advise** — nothing imported is active until
the rule's owner or an admin activates it in MemHub. Never call any activation
path from this skill.

### 5. Report

Per row: filed as draft / proposed (with what it supersedes) / unchanged /
skipped (why: narrative, already enforced, collides with `<rule>`). List every
`contradicts` verdict again under a **Conflicts to resolve** heading — the
reviewer has to retire one side before activating the other, and nothing in
the server will tell them. Then the follow-up the user owns: review the drafts
(`list_rules`, status `draft` / `proposed`) and activate the ones they want
live.
