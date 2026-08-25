---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "turn this CLAUDE.md line into a rule", "make a rule for this mistake"). Drafts a deterministic matcher, BACKTESTS it against past sessions, and only then files it — always at the advisory tier.
argument-hint: [--brain "<brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. If unset, export it
first — it is the ancestor directory of this skill file containing `.claude-plugin/`.

You are creating a **Rulebook rule**: a **human-authored lesson** stored in
MemHub's directive substrate, so it is immediately shareable, portable, and
served by the same `recall_directives` pipeline that serves auto-learned
lessons — same triggers, same brains, same lanes, but human provenance and
human trust. Rules are data, not prose in a doc — and a rule that was never
backtested is a false-fire generator (the pilot measured ~60% organic
false-fire from unhardened matchers).

The precision model differs from a regex engine: the **trigger is the
tripwire** (broad is OK — it decides what reaches the gate) and the **LLM
applicability gate** downstream provides precision. The backtest measures
tripwire volume; the content carries the nuance the gate applies (e.g. "the
with-lease form on your own branch is sanctioned").

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
| `session` | SessionStart — a POSTURE rule: worldview served as context, no matcher, never enforced | — | — |

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
- `session` (posture) rules skip the backtest — there is nothing to match. They
  ride the context window every session, so hold a hard budget: at most 3 per
  repo scope, and only for worldview a matcher cannot express. Session start is
  the weakest attention slot (measured 4% vs 88% in-flight) — anything with a
  checkable shape belongs in a `bash`/`edit`/`result` rule instead.

### 4. Backtest — the arming gate

Directive rules: backtest the **triggers** (offline approximation: substring
replay over past commands, paths, and edited content):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_backtest.py" \
  --triggers "git push,--force" --days 30 --exclude-session "<current session id>"
```

Compiled-tier rules (deterministic matchers): backtest the matcher itself:

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

### 5. Confirm, then file — as a human-authored lesson

Show the user: the rule sentence, the triggers, and the backtest verdict
(`N sessions hit / M scanned, judged TP/FP`). On approval, the **primary write
is the MemHub directive** — call the `add_directive` MCP tool:

- `content`: prefix with `RULE (human-authored, team convention):`, then the
  full sentence including the nuance the LLM gate needs (sanctioned forms,
  exemptions — they live in prose here, not in `not_rx`).
- `triggers`: the concrete identifiers, **including flag-shaped and
  phrase-shaped ones** (`--force`, `git push`) — these are what the backtest
  validated.
- `fact_type`: `"lesson"` (or `"procedure"` with `steps` for a recipe).
- `scope`: the repo (from `--brain`, e.g. `Repo: XTraceAI/xmem` → `xmem`).

Known limits, say them out loud: authoring is **personal-partition only** for
now (shared-brain authoring needs the backend's review gate — the rule reaches
teammates once that lands, or via brain-level import); and serve-side entity
extraction currently misses flag/phrase-shaped triggers from raw commands, so
the fire is reliable via explicit `entities` until the plugin's recall payload
carries them.

**Optional compiled tier**: only for a rule whose ledger later proves it and
whose shape is exactly checkable, ALSO compile it to a deterministic matcher in
the local rulebook (the enforcement ladder's gate-candidate cache):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_add.py" --rule '<compiled JSON>'
```

Include provenance either way: `source_ref` (e.g. `xmem/CLAUDE.md § <section>`
or `user correction, session <id>`) and the backtest verdict. **New rules
always land advisory** — promotion to any gating tier is a separate,
admin-only, evidence-gated decision this skill never makes.

### 6. Report

Tell the user: the rule is live immediately (`recall_directives` serves it on
the next matching action; a compiled copy fires via the local hook with zero
latency), which sessions it would have fired in, and where its evidence will
accrue (the serving ledger server-side; `ledger/fires.jsonl` for compiled
rules) — the evidence that later decides promote / demote / retire.
