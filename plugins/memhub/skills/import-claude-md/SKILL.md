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

### 3. Duplicate check against the book

Call the memhub `list_rules` tool (the target brain) and compare each candidate
against existing titles and statements. Same subject → prefer **re-importing
under the existing title** (the server then reports `unchanged` or files a
`proposed` update) over adding a twin under a new name.

### 4. Show the table, then file

Show the user one table: title · delivery · engine · source_ref. Get a yes (or
drop rows). With `--dry-run`, stop here.

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
skipped (why: narrative, already enforced). Then the follow-up the user owns:
review the drafts (`list_rules`, status `draft` / `proposed`) and activate the
ones they want live.
