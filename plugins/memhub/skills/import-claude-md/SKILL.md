---
description: Use when the user wants to turn a CLAUDE.md (or any conventions doc) into Rulebook rules — "/memhub:import-claude-md", "import our CLAUDE.md as rules", "re-import the rules from CLAUDE.md". Reads the file HERE in the repo, decomposes it in-agent, and files each rule for review through the memhub `create_rule` tool. Nothing imported goes live without a person reading it; a re-import replaces rules instead of duplicating them.
argument-hint: [path to the doc, default ./CLAUDE.md] [--brain "<rulebook brain name>"] [--dry-run]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule
---

You are importing a conventions document into the team **Rulebook**. This runs
**in the coding agent, not on the server**, on purpose: decomposing a doc into
rules needs the repo (which paths a rule scopes to, whether a pattern matches
real commands here) and the checkout's sha for provenance — neither of which
the server has. You are already reading the file; a second model call
server-side would only lose that context. The server's one job is filing:
it dedupes a retried identical import and, when you tell it which rule a
candidate replaces, links the two so activation swaps them.

**Nothing this skill files goes live.** An imported rule always lands for
review, whoever runs the import — a document is decomposed by a model, not
written sentence by sentence by a person, so the per-rule consent that the
live-authoring path assumes is not really there. Never pass `activate`; the
server would refuse it anyway, and asking implies a choice that doesn't exist.

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
for each rule you derive. Keep the path stable across runs (no absolute
paths): the server treats a retried import with the same path and title and
identical content as a no-op (`unchanged`), and because a `source_ref` is a
real document anchor, two teammates importing the same file converge on one
rule instead of filing twins. Repo name = the basename of the toplevel
(e.g. `xmem`) → `scope_repos: ["<repo>"]`.

### 2. Decompose — in your own reading, no model call

Walk the document heading by heading. A **rule** is a conditional a teammate
can violate: *when X, do / never Y, because Z*. Skip narrative, architecture
description, and anything the code already enforces (a linter, a CI check —
say "already enforced by <what>" in the report). For each rule pick ONE
delivery, per the table in `/memhub:create-rule` step 3:

| shape of the rule | delivery | engine block |
|---|---|---|
| a command / edit / tool output has a checkable form | `agent_hook` | `matcher` (`event: bash \| edit \| output`; `command_rx` for bash, `path_rx` for edit, `content_rx` for output, the `*_not_rx` exemptions, `warn_once_per`) |
| "run X after edits, before Y" | `agent_hook` | `ordering` (`required_command_rx` and `gated_command_rx` both required) |
| applies when a named file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server decides relevance |
| worldview with no trigger at all | `session_context` | none — shown once at session start; only if no checkable shape exists |

Title = the heading or a short noun phrase (aim for under 60 chars); **one rule
per title** within a run. Statement = the advisory line plus the nuance a
reviewer needs (sanctioned forms, exemptions) — **within 400 characters**, a
hard refusal rather than a trim, so a row whose statement runs long is not
filed at all.

The matcher vocabulary is closed and is spelled out in `/memhub:create-rule`
step 3 — the same list, exactly. In particular: the events are `bash`, `edit`
and `output`; there is no `result` event and no `result_rx` key, so a rule
about a *failing command* is `{event: "output", content_rx: ...}`. `write` is a
legacy alias the server stores as `edit` — author `edit`. Every `edit` matcher
needs a `path_rx` (the server would take `content_rx` or `min_chars` alone, but
the hook never fires such a rule); `min_chars` is not worth writing at all. Do
not offer `warn_once_per: "file"`; the hook maps it to `session`.

Apply the matcher-authoring rules from `/memhub:create-rule` step 3
(pre-heredoc matching, shape-specific patterns, exemptions in `command_not_rx`
up front, patterns under 400 chars, no quantified alternation group / `(.*)` /
`.*.*`, `warn_once_per: "session"` by default), and sanity-check each regex
against a real command from this repo that should fire and one that should
not.

### 3. Conflict check against the book — before anything is filed

The server does no title matching: a candidate that collides with an
existing rule lands as a **second rule, silently** unless you tell the
server which rule it replaces. So the check happens here, in your hands,
with the candidates and the book both in view.

1. Call the memhub `list_rules` tool (the target brain, every status) and save
   the reply to a file. Write the candidate `create_rule` bodies to another.
2. Run the deterministic pass:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
     --candidates <candidates.json> --existing <list_rules.json> --repo "<repo>"
   ```
   It flags `same_title` (any live status, case/punctuation-insensitive),
   `same_matcher` (same event + same primary pattern as an **active** rule —
   two of those fire on the same call) and `anchors_overlap` (with the shared
   identifiers listed). Under each hit it prints the exact
   `supersedes_rule_id` value to copy if you judge it the same rule. The
   active book comes from the server's hook view; if it is unreachable the
   script says `active book: unavailable` and you still get the title pass.
3. Then the semantic pass, in your own reading — no model call: the script
   prints `judge_by_statement`, every live rule with no deterministic hit, with
   its statement. Read each candidate against that list and mark
   `duplicate` (same trigger, same advice), `contradicts` (same trigger,
   opposite advice) or `distinct`.

What each verdict does to the row — **you** decide what a candidate
replaces; the server never guesses:
- `duplicate` (or a `same_title` / `same_matcher` hit you judge to be the
  same rule) → file it with `supersedes_rule_id: <that rule's rule_id>`. The
  server files it for review with the link persisted, and activation retires
  exactly that rule.
- `same_matcher` against an **active** rule that is NOT the same rule → do
  not file; tell the user (two active rules would fire on the same call).
- `contradicts` → file it WITHOUT `supersedes_rule_id`, and say in the table
  and the report which rule it fights and whether that rule is active — the
  reviewer must retire one before activating the other.
- `anchors_overlap` on its own is a hint, not a verdict — judge the
  statements.

### 4. Show the table, confirm, then file

Show the user one table: title · delivery · engine · **conflicts** (rule
title + reason + your verdict, or "none") · source_ref. Then stop and ask,
once for the run:

```
Filing <N> rules.

  • Rulebook: <rulebook name> (org <org>)
  • Scope: <repo>
  • Replacing: <count> existing rules · new: <count>

Every one is filed for review — nothing here goes live until a person
activates it in MemHub.

File them? (yes / drop rows / cancel)
```

With `--dry-run`, stop here. Never file in the background or as a side effect
of another skill.

On approval call the memhub **`create_rule`** tool once per row:
`title`, `statement`, `delivery`, the engine block (`matcher` / `ordering` /
`anchors`), `scope_repos`, `source="claude_md_import"`, `source_ref`,
`supersedes_rule_id` for the rows that replace a rule, and `agent_brain_id`
when `--brain` was given. `title` and `delivery` are required on every row —
the server derives neither.

Read each reply:

| reply | what happened |
|---|---|
| `status: "proposed"` | filed for review; it fires once someone activates it |
| `unchanged: true` | identical content is already in the book (a retried import); nothing written |
| `supersedes_rule_id` | what this row will retire when someone activates it |

The reply also names the destination — `rulebook` and `org`. With no `org_id`
that is the user's default org, so report it: an import into the wrong team's
rulebook is otherwise invisible.

**Two refusals, both meaning that row was not written** — so retrying that row
is safe. They arrive as a tool error carrying only the sentence; the reason code
behind each (`supersedes_unknown`, `target_already_replaced`) is not on the
wire, so recognise them by their text:

- *"The rule this one replaces isn't a live rule in this rulebook — check
  supersedes_rule_id."* → `supersedes_unknown`. Re-read `list_rules`, re-target,
  call again for that row.
- *"The rule this one replaces has already been replaced by someone else's."* →
  `target_already_replaced`. Re-target the rule that is in the book now and call
  again, or file that row without `supersedes_rule_id` and say so in the
  report.

### 5. Report

Per row: filed for review (name the rule it replaces, by title) / unchanged /
skipped (why: narrative, already enforced, collides with `<rule>`) / refused
(which refusal, and what you did about it). Where a row's reply carries a
`message` that says anything beyond "filed for review", give it as it stands —
it is written for the user, and it is the only place the server explains an
outcome you did not expect. List every `contradicts` verdict
again under a **Conflicts to resolve** heading — the reviewer has to retire one
side before activating the other, and nothing in the server will tell them.
Then the follow-up the user owns: review the proposals (`list_rules`, status
`proposed`) and activate the ones they want live.
