---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "turn this CLAUDE.md line into a rule", "make a rule for this mistake"). Drafts a deterministic matcher, BACKTESTS it against past sessions, and only then files it — always at the advisory tier.
argument-hint: [--brain "<brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. If unset, export it
first — it is the ancestor directory of this skill file containing `.claude-plugin/`.

You are creating a **Rulebook rule**: a deterministic matcher that fires inside
teammates' agent sessions at the moment a rule is about to be violated. Rules are
data, not prose in a doc — and a rule that was never backtested is a false-fire
generator (the pilot measured ~60% organic false-fire from unhardened matchers).

Arguments: `$ARGUMENTS`
- `--brain "<name>"` (optional) → the destination brain. Until server authoring
  ships, it maps to scope: a repo brain like `Repo: XTraceAI/xmem` →
  `repo_scope: "xmem"`; anything org-wide → `repo_scope: "any"`. Record the
  brain name verbatim in the rule's `brain` field (provenance for the server
  migration).
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. Conditional shape is the top
measured predictor of rule usefulness; a bare observation is not a rule. If the
user gave a war story, extract the conditional from it and confirm your reading.

### 2. Duplicate check

Read `~/.claude/scripts/rulebook/rulebook.json` and compare the new rule against
every existing `id`, `text`, and matcher. Overlap → propose tightening the
existing rule instead of adding a twin. Show the user the collision if there is
one.

### 3. Draft the matcher

The hook (`~/.claude/scripts/rulebook/rulebook_hook.py`) evaluates these shapes:

| `on` | fires when | required | optional |
|---|---|---|---|
| `bash` | a Bash command matches `rx` | `rx` | `not_rx`, `match_heredoc_body` |
| `edit` | an Edit/Write path matches `path_rx` | `path_rx` | `path_not_rx`, `content_rx` (against the new content) |
| `result` | a tool RESULT matches `rx` (PostToolUse) | `rx` | `cmd_rx`, `exclude_rx` |

Plus on every rule: `id` (kebab), `text` (≤160 chars, the advisory line),
`why` (one sentence, shown in parentheses), `fire_scope`
(`call` | `session` | `branch` | `counter:N`), `repo_scope` (`xmem` | `any`).

**Matcher-authoring rules, learned the hard way (pilot-3 + the D2 re-scan):**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and were the week's whole
  false-fire class. Set `match_heredoc_body` only if the rule targets heredocs.
- Anchors must be **shape-specific**: match the violating *form* (`git push
  [-f|--force]`), never a keyword that also appears in innocent content.
- Every known-legitimate exemption goes in `not_rx` now, not after it fires.
- Default `fire_scope: "session"` — a rule that nags every call gets ignored.
  `call` is for rules where each occurrence matters (e.g. force-push).
- `result` rules cannot be backtested from transcripts (results aren't replayed)
  — they arm from live advisory data instead; say so.

### 4. Backtest — the arming gate

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --rule '<the candidate JSON>' --days 30 --exclude-session "<current session id>"
```

Always pass `--exclude-session` (this session's transcript contains the
candidate regex and will self-match). Then **read every excerpt** and judge
each session-hit true or false positive yourself; show the user the tally and
the borderline excerpts. Iterate the matcher until the excerpts are clean:

- False positives → tighten `rx` / add `not_rx`, re-run.
- Zero fires in 30 days → still armable for rare, high-blast tripwires
  (consumer-contract edits, force-push), but state "zero-fire in the window"
  out loud so the user decides with that fact.

### 5. Confirm, then file

Show the user: the final rule JSON, the backtest verdict (`N sessions hit /
M scanned, X judged TP, Y FP`), and where it will fire. On approval:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_add.py" --rule '<final JSON>'
```

Include provenance fields on the rule: `brain` (from `--brain`), `source_ref`
(e.g. `xmem/CLAUDE.md § <section>` or `user correction, session <id>`), and
`backtest` (`{"days": 30, "sessions_scanned": N, "session_hits": M,
"judged_tp": X, "judged_fp": Y}`).

The add script refuses duplicates, validates every regex, backs up the
rulebook, and writes atomically. **New rules always land advisory** — the hook
never blocks; promotion to a gating tier is a separate, admin-only,
evidence-gated decision that this skill never makes.

### 6. Report

Tell the user: the rule is live immediately (the hook re-reads the rulebook on
every tool call), which sessions it would have fired in, and that its real
fires will accrue in `~/.claude/scripts/rulebook/ledger/fires.jsonl` — the
evidence that later decides promote / demote / retire.
