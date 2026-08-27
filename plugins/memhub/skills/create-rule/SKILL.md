---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake"). Drafts a deterministic matcher, BACKTESTS it against past sessions, and only then files it as a draft through the memhub `create_rule` tool — always advisory; a human activates it.
argument-hint: [--brain "<rulebook brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. If unset, export it
first — it is the ancestor directory of this skill file containing `.claude-plugin/`.

You are creating a **Rulebook rule**: a human-authored, team-owned rule stored
on the server (`team_memory_rules`), fetched by every teammate's hook once per
session, and measured on every fire. Rules are data, not prose in a doc — and a
rule that was never backtested is a false-fire generator (the pilot measured
~60% organic false-fire from unhardened matchers).

There is no local rule file. The write path is the memhub **`create_rule`**
MCP tool; the rule reaches teammates when a human activates it.

Arguments: `$ARGUMENTS`
- `--brain "<name>"` (optional) → the rulebook to write into (`agent_brain_id`
  on `create_rule`). Omit to use the repo's own rulebook.
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. Conditional shape is the top
measured predictor of rule usefulness; a bare observation is not a rule. If the
user gave a war story, extract the conditional from it and confirm your reading.

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
| a failing tool result | `agent_hook` | `matcher: {event: "result", result_rx, command_rx?}` |
| "run X after edits, before Y" | `agent_hook` | `ordering: {required_command_rx, gated_command_rx, armed_by_events, min_edits, display_name}` |
| applies when a file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server's SLM judge decides relevance per call |
| worldview with no trigger at all | `session_context` | none — spec budget: 15 rules / ~2k tokens per repo scope; session start is the weakest attention slot (4% vs 88% in-flight), so prefer a checkable shape when one exists |

Plus on every rule: `title` (≤ 60 chars), `statement` (the advisory line and
the nuance a judge needs: sanctioned forms, exemptions), `scope_repos`
(`["<repo>"]` or `[]` for all), `scope_paths` / `scope_exclude_paths` (globs).

**Matcher-authoring rules, learned the hard way (pilot-3 + the D2 re-scan):**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and were the week's whole
  false-fire class. Set `match_heredoc_body` only if the rule targets heredocs.
- Anchors must be **shape-specific**: match the violating *form* (`git push
  [-f|--force]`), never a keyword that also appears in innocent content.
- Every known-legitimate exemption goes in `command_not_rx` now, not after it fires.
- Default `warn_once_per: "session"` — a rule that nags every call gets ignored.
  `turn` is for rules where each occurrence matters (e.g. force-push).
- `result` rules cannot be backtested from transcripts (results aren't replayed)
  — they arm from live advisory data instead; say so.

### 4. Backtest — the arming gate

```bash
# matcher / ordering rules (hook shape: on/rx/not_rx/path_rx…, same regexes)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --rule '<candidate JSON>' --days 30 --exclude-session "<current session id>"
# anchor rules: replay the anchors as triggers
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --triggers "git push,--force" --days 30 --exclude-session "<current session id>"
```

Always pass `--exclude-session` (this session's transcript contains the
candidate regex and will self-match). Then **read every excerpt** and judge
each session-hit true or false positive yourself; show the user the tally and
the borderline excerpts. Iterate the matcher until the excerpts are clean:

- False positives → tighten `command_rx` / add `command_not_rx`, re-run.
- Zero fires in 30 days → still fileable for rare, high-blast tripwires
  (consumer-contract edits, force-push), but state "zero-fire in the window"
  out loud so the user decides with that fact.
- `session_context` rules skip the backtest (nothing to match).

### 5. Confirm, then file

Show the user: the rule sentence, the delivery + engine block, and the backtest
verdict (`N sessions hit / M scanned, judged TP/FP`). On approval call the
memhub **`create_rule`** tool with `title`, `statement`, `delivery`, the engine
block, `scope_repos`, `source_ref` (e.g. `xmem/CLAUDE.md@<sha>#<heading>` or
`user correction, session <id>`), and `agent_brain_id` when `--brain` was
given. The reply is `status: "draft"` (or `proposed` with `supersedes_rule_id`
when a `source_ref` matched an existing rule; `unchanged` when it is identical).

**New rules always land draft / advise.** Activation (`POST /rules/{id}/activate`,
which requires a backtest for that version) and any gating tier are human,
admin-side decisions this skill never makes. Never call an activation path.

### 6. Report

Tell the user: the rule is filed as a draft, which sessions it would have fired
in, and what happens next — a reviewer activates it, every teammate's hook picks
it up on their next session, and its evidence accrues in the fires ledger
(`GET /fires`, `GET /stats`) — the evidence that later decides promote / demote /
retire.
