---
description: Use when the user wants to turn a CLAUDE.md (or any conventions doc) into Rulebook rules — "/memhub:import-claude-md", "import our CLAUDE.md as rules", "re-import the rules from CLAUDE.md". Reads the file HERE in the repo, decomposes it in-agent, backtests every candidate against past sessions, and files the survivors as drafts through the memhub `create_rule` tool. A re-import updates rules instead of duplicating them.
argument-hint: [path to the doc, default ./CLAUDE.md] [--brain "<rulebook brain name>"] [--dry-run]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. If unset, export it
first — it is the ancestor directory of this skill file containing `.claude-plugin/`.

You are importing a conventions document into the team **Rulebook**. This runs
**in the coding agent, not on the server**, on purpose: decomposing a doc into
rules needs the repo (which paths a rule scopes to, whether a pattern matches
real commands here), the local transcripts to **backtest** each candidate, and
the checkout's sha for provenance — none of which the server has. You are
already reading the file; a second model call server-side would only lose that
context. The server's one job is the **re-import identity**: it recognises a
rule it already holds (spec §4.5).

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
for each rule you derive. The server's re-import key is **(rulebook, the
`source_ref` path before `@`, normalised title)** — so keep the path stable
across runs (no absolute paths) and the title stable across runs; either one
changing files a twin instead of an update. Repo name = the basename of the
toplevel (e.g. `xmem`) → `scope_repos: ["<repo>"]`.

### 2. Decompose — in your own reading, no model call

Walk the document heading by heading. A **rule** is a conditional a teammate
can violate: *when X, do / never Y, because Z*. Skip narrative, architecture
description, and anything the code already enforces (a linter, a CI check —
say "already enforced by <what>" in the report). For each rule pick ONE
delivery, per the table in `/memhub:create-rule` step 3:

| shape of the rule | delivery | engine block |
|---|---|---|
| a command / edit / tool result has a checkable form | `agent_hook` | `matcher` — `event: "bash"` + `command_rx` (`command_not_rx?`); `event: "edit"` / `"write"` + `path_rx` / `content_rx` (`path_not_rx?`); `event: "output"` + `content_rx` over the result text (`content_not_rx?`, `command_rx?` gates on the command); plus `warn_once_per`. `match_heredoc_body: true` requires `body_rx`. |
| "run X after edits, before Y" | `agent_hook` | `ordering` |
| applies when a named file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server's SLM judge decides relevance |
| worldview with no trigger at all | `session_context` | none — spec budget: 15 rules / ~2k tokens per repo scope; only if no shape exists |

Title = the heading or a ≤ 60-char (3-8 word) noun phrase; **one rule per
title** within a run. Statement = the full sentence including the nuance a
judge needs (sanctioned forms, exemptions), ≤ 500 chars. (The server allows
200 / 4000; the tighter limits are ours — a rule is read in a hook line.)

### 3. Duplicate check against the book

Call the memhub `list_rules` tool (the target brain) and compare each candidate
against existing titles and statements. Same subject → prefer **re-importing
under the existing title AND the existing `source_ref` path** (the server
then reports `unchanged` or files a `proposed` update) over adding a twin
under a new name. A rule that was authored without a `source_ref` (e.g. via
`/memhub:create-rule`) has an empty path in its key: giving it one now
creates a twin — file it with no `source_ref`, or accept the twin knowingly.

### 4. Backtest — the arming gate, per candidate

```bash
# this session's id, for --exclude-session (newest transcript of this cwd):
basename "$(ls -t ~/.claude/projects/$(pwd | sed 's#/#-#g')/*.jsonl | head -1)" .jsonl
# matcher rules (bash / edit / write / output): pass the SAME JSON you will
# send to create_rule — the full body or just the matcher dict
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --rule '{"delivery":"agent_hook","matcher":{"event":"bash","command_rx":"..."},"scope_repos":["<repo>"]}' \
  --days 30 --exclude-session "<this session id>"
# anchor rules: replay the anchors as triggers
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --triggers "<anchor1>,<anchor2>" --days 30 --exclude-session "<this session id>"
```

Always pass `--exclude-session` (this transcript contains the candidate text).
**Read every excerpt** and judge each session-hit true / false positive; tighten
`command_rx` / add `command_not_rx` and re-run until the excerpts are clean.
Zero fires in the window is allowed for rare, high-blast rules — say so.
`session_context` rules skip the backtest (nothing to match).

Anchor (`--triggers`) backtests are case-insensitive **substring** matches
over commands, paths, and edited content — not the server's judge — so their
hit count is an **upper bound**: a MEMORY.md note or a scratchpad file that
merely mentions the symbol counts as a hit.

The script ends with a `"backtest": {...}` verdict summary per candidate
(`sessions`, `hits`, `days`, `judged_tp: 0`, `judged_fp: 0`). Fill
`judged_tp` / `judged_fp` from your reading — that verdict goes in the table
and the report (step 5/6). The backtest is the **client-side arming gate**;
the server does not store it and `create_rule` does not take it.

### 5. Show the table, then file

Show the user one table: title · delivery · engine · backtest verdict
(`N sessions hit / M scanned, judged TP/FP`) · source_ref. Get a yes (or drop
rows). With `--dry-run`, stop here.

On approval call the memhub **`create_rule`** tool once per row:
`title`, `statement`, `delivery`, the engine block (`matcher` / `ordering` /
`anchors`), `scope_repos`, `source="claude_md_import"`, `source_ref`, and
`agent_brain_id` when `--brain` was given. Do not pass the backtest verdict —
it is not a `create_rule` field; put it in the report instead. Read each reply:

- `unchanged: true` → already in the book, nothing written.
- `status: "proposed"` + `supersedes_rule_id` → an update to an existing rule;
  it replaces the old one when a human activates it.
- `status: "draft"` → new.

Every row lands **draft/proposed, advise** — nothing imported is active until a
human reviews it and activates it (`POST /rules/{id}/activate` needs a
backtest for that version). Never call any activation path from this skill.

### 6. Report

Per row: filed as draft / proposed (with what it supersedes) / unchanged /
skipped (why: narrative, already enforced, failed backtest). Then the follow-up
the user owns: review the drafts (`list_rules`, status `draft` / `proposed`)
and activate the ones they want live.
