---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake"). Pins a when-X-then-Y sentence, drafts a deterministic check, and files it as a draft through the memhub `create_rule` tool — always advisory; a reviewer activates it.
argument-hint: [--brain "<rulebook brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion
---

You are creating a **Rulebook rule**: a human-authored, team-owned rule stored
in MemHub, fetched by every teammate's coding agent once per session, and
measured on every fire. Rules are data, not prose in a doc — and a rule with a
loose check fires on innocent commands more often than not, so the check is
where the care goes.

There is no local rule file. The write path is the memhub **`create_rule`**
MCP tool; the rule reaches teammates when a reviewer activates it.

Arguments: `$ARGUMENTS`
- `--brain "<name>"` (optional) → the rulebook to write into (`agent_brain_id`
  on `create_rule`). Omit to use the repo's own rulebook.
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. A conditional shape is what
makes a rule actionable; a bare observation is not a rule. If the user gave a
war story, extract the conditional from it and confirm your reading.

### 2. Duplicate check

Call the memhub `list_rules` tool for the target rulebook and compare the new
rule against every title and statement. Overlap → propose tightening the
existing rule (re-file under its title with a `source_ref`; the server turns
that into a `proposed` update) instead of adding a twin.

### 3. Draft the rule — one delivery, one engine block

| the rule is… | `delivery` | engine block |
|---|---|---|
| a Bash command with a checkable form | `agent_hook` | `matcher: {event: "bash", command_rx, command_not_rx?, warn_once_per}` |
| an edit/write to certain paths or content | `agent_hook` | `matcher: {event: "edit", path_rx, path_not_rx?, content_rx?}` |
| a failing or noteworthy tool output | `agent_hook` | `matcher: {event: "output", content_rx, command_rx?, content_not_rx?}` |
| "run X after edits, before Y" | `agent_hook` | `ordering: {required_command_rx, gated_command_rx, armed_by_events, min_edits, display_name}` |
| applies when a file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server decides relevance per call |
| worldview with no trigger at all | `session_context` | none — at most 15 such rules per repo scope are shown at session start; prefer a checkable shape when one exists, because advice shown in-flight is acted on far more often than advice shown at session start |

Plus on every rule: `title` (short, imperative; the server allows up to 200
chars but aim for under 60), `statement` (the advisory line and the nuance a
reviewer needs: sanctioned forms, exemptions), `scope_repos` (`["<repo>"]` or
`[]` for all), `scope_paths` / `scope_exclude_paths` (globs).

**Matcher-authoring rules:**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and are the main false-fire class.
  Set `match_heredoc_body: true` **together with** `body_rx` only if the rule
  targets what a heredoc says.
- Patterns must be **shape-specific**: match the violating *form* (`git push
  [-f|--force]`), never a keyword that also appears in innocent content.
- Every known-legitimate exemption goes in `command_not_rx` now, not after it
  fires. Give bash rules a `command_not_rx` that exempts commands which merely
  mention the pattern (`python -c`, `grep`).
- Default `warn_once_per: "session"` — a rule that nags every call gets ignored.
  `turn` is for rules where each occurrence matters (e.g. force-push).

Sanity-check every regex against two or three real commands from this repo's
history (`git log`, your own shell history) — one that should fire and one
that should not — before filing.

### 4. Confirm, then file

Show the user: the rule sentence, the delivery + engine block, and the sample
commands it does and doesn't match. On approval call the memhub
**`create_rule`** tool with `title`, `statement`, `delivery`, the engine
block, `scope_repos`, `source_ref` (e.g. `xmem/CLAUDE.md@<sha>#<heading>` or
`user correction, session <id>`), and `agent_brain_id` when `--brain` was
given. The reply is `status: "draft"` (or `proposed` with `supersedes_rule_id`
when a `source_ref` matched an existing rule; `unchanged` when it is identical).

**New rules always land draft / advise.** Activation and any blocking tier are
reviewer decisions this skill never makes. Never call an activation path.

If the rule is better as a plain suggestion than a check — the user doesn't
want to write a detector — call **`nominate_rule`** with the sentence instead;
it lands as `proposed` for a reviewer.

### 5. Report

Tell the user: the rule is filed as a draft and what happens next — the rule's
owner or an admin activates it in MemHub, every teammate's coding agent picks
it up on their next session, and its firing history accrues in MemHub as the
evidence that later decides whether to keep, narrow, or retire it.
