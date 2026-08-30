---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake"). Pins a when-X-then-Y sentence, drafts a deterministic check, and files it as a draft through the memhub `create_rule` tool — always advisory; a reviewer activates it.
argument-hint: [--brain "<rulebook brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule
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

### 1b. Evidence: how often would it have applied?

A rule is worth the team's attention in proportion to how often the
situation actually occurs. Write the candidate `create_rule` body to a file
and replay it over the local transcripts (Claude Code, Codex, Cursor):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/mine-proposals/scripts/mine_sessions.py" \
  --rule-file /tmp/cand.json --out /tmp/mine
```

Read the candidate's line: `applies-in N/M sessions` (by host) and 3 sample
commands. For a "do X before Y" rule add `"requires_prior_rx": "<X>"` to the
body — the line then shows `precision = fired-with-no-prior-X / fired`;
below ~50 % the matcher would nag people who already complied, so use the
`ordering` shape (step 3) or make it `session_context`. Carry the numbers
into `source_ref` in step 5 (`…|applies N/M|precision P`). If N is 0 across
all hosts, say so to the user before filing — it may still be right
(insurance for a new teammate) but it is not lift. For deriving many rules
at once from sessions, use `/memhub:mine-proposals` instead.

### 2. Duplicate check — by eye now, deterministically in step 5

Call the memhub `list_rules` tool for the target rulebook (every status) and
read the new rule against every title and statement. Same subject → plan to
replace the existing rule instead of adding a twin: note its `rule_id` for
`supersedes_rule_id` in step 5. The server does no title matching — you
decide what a rule replaces. Keep the `list_rules` reply: step 5 runs the
deterministic check over it.

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

### 4. Prove it fires — and prove it stops

A rule that matches the command you had in mind can still be wrong in three
ways that only show up once the whole team has it. Run the candidate through
the engine that will actually run it:

```bash
cat > /tmp/cand.json <<'JSON'
{"title": "...", "statement": "...", "delivery": "agent_hook",
 "matcher": {"event": "bash", "command_rx": "...", "command_not_rx": "..."}}
JSON
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires 'the real command that should trigger it' \
  --silent 'the same situation once someone has complied'
```

For an `edit` / `write` rule a case is `path::content`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires '/repo/src/db.py::conn = connect(url, verify=False)' \
  --silent '/repo/src/db.py::conn = connect(url)'
```

It exits non-zero until every case behaves. **Do not file a rule while it
exits non-zero, and show the table to the user.** What each line means:

- **LOAD** — whether the hook would load the rule at all. A pattern over 400
  characters, one that does not compile, or one that backtracks is dropped
  *silently* on every teammate's machine: the rule exists, is active, and
  never fires. This line is the only warning you get.
- **FIRES** — your `--fires` cases. At least one is required; without it
  nothing has shown the rule can trigger.
- **SILENT** — your `--silent` cases, plus two generated for you: `grep` and
  `python -c` quoting the rule's own trigger. Add one more yourself: the
  trigger inside a quoted argument (`--allowedTools 'Bash(git push:*)'`,
  `echo "…"`, a commit message) — measured live, this mention-in-args form
  is the largest false-fire class after `grep`. Searching for a rule's trigger
  is how people investigate it, and firing there is the largest false-fire
  class we have measured. If those two fail, add a `command_not_rx`.

**Always give at least one `--silent` case for the complied-with form** — the
code *after* someone does what the rule asks. This is the check authors skip
and the one that matters most: a rule that keeps firing once you have fixed
the problem cannot tell a violation from a fix, so people learn to ignore it.
If you cannot write a `--silent` case that the rule passes, the rule is not
expressible as a pattern — make it `anchor_recall` or a `session_context`
note instead of shipping a nag.

### 5. Conflict check, confirm, then file

Before showing the rule, check it against the book — the server files a
colliding title or matcher as a silent second draft unless you name what it
replaces, so this is the only place it gets caught. Call `list_rules` (every status),
save the reply, write the candidate `create_rule` body to a file, and run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
  --candidates <candidate.json> --existing <list_rules.json> --repo "<repo>"
```

`same_title` / `same_matcher` (an **active** rule fires on the same call) /
`anchors_overlap` are deterministic; then read the `judge_by_statement` list
it prints and mark the candidate `duplicate`, `contradicts` or `distinct`
against each (the script prints the exact `supersedes_rule_id` value under
each hit). `duplicate` (or a `same_title` / `same_matcher` hit you judge to
be the same rule) → file with `supersedes_rule_id: <that rule's rule_id>`;
the server files it as `proposed` and activation replaces exactly that rule.
`same_matcher` against an **active** rule that is NOT the same rule → do not
file; tell the user. `contradicts` → file as a draft WITHOUT
`supersedes_rule_id`, but name the rule it fights in the report; a reviewer
retires one side before activating the other.

Show the user: the rule sentence, the delivery + engine block, the sample
commands it does and doesn't match, and the conflict verdict. On approval call the memhub
**`create_rule`** tool with `title`, `statement`, `delivery`, the engine
block, `scope_repos`, `source_ref` (e.g. `<path/to/CLAUDE.md>@<sha>#<heading>` or
`user correction, session <id>`, with the step-1b numbers appended:
`|applies N/M|precision P`), `supersedes_rule_id` when it replaces a
rule, and `agent_brain_id` when `--brain` was given. Read the reply:

- `unchanged: true` → identical content is already in the book (a retried
  call with the same `source_ref` path and title); nothing written.
- `status: "proposed"` + `supersedes_rule_id` → filed as a replacement for
  the rule you named; it retires that rule when a reviewer activates it.
- `status: "draft"` → new.

**New rules always land draft / advise.** Activation and any blocking tier are
reviewer decisions this skill never makes. Never call an activation path.

If the rule is better as a plain suggestion than a check — the user doesn't
want to write a detector — file it the same way with
`source="nomination"` and no engine block; it lands as `proposed` for a
reviewer.

### 6. Report

Tell the user: the rule is filed as a draft — or as `proposed`, naming the
rule it replaces by title — and what happens next: the rule's owner or an
admin activates it in MemHub (a `proposed` rule retires the one it replaces), every teammate's coding agent picks
it up on their next session, and its firing history accrues in MemHub as the
evidence that later decides whether to keep, narrow, or retire it.
