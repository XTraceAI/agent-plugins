---
description: Use when the user wants rules proposed from what their coding agents actually did — "/memhub:rules-from-sessions", "mine our sessions for rules", "mine rules from sessions", "what should be in the rulebook", "propose skills from what we keep doing", "backtest this rule", "did the new rules reduce friction" — or right after a Claude Code /insights run. Reads local Claude Code, Codex, and Cursor transcripts plus CLAUDE.md, scores every candidate against the transcripts (applies-in N/M, precision, samples), and files survivors as proposed rules / skills; hooks land as settings snippets. Never activates anything.
argument-hint: [--repo <name>] [--claude-md <path>] [--baseline-date YYYY-MM-DD] [--brain "<rulebook brain name>"]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_skills, mcp__plugin_memhub_memhub__create_skill, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_skills, mcp__plugin_memhub-staging_memhub__create_skill
---

# Rules from sessions

Every rule this skill proposes answers three questions in the user's own
terms, in this order — and a candidate that cannot answer the first is not
proposed:

1. **Why does it exist?** One of two origins, nothing else: *your CLAUDE.md
   already says this* (the sentence is quoted, with how often it was broken
   anyway), or *your past sessions* (how many, plus the user's own words from
   a session where it bit).
2. **What did it cost?** In the sessions it would have fired in: how many had
   the user correcting Claude, how many had a revert, and the friction the
   facets recorded there.
3. **What changes with it on?** One sentence, plus *when* it fires — one of
   the rulebook's four triggers, in plain words:

| fires… | rulebook `delivery` | what the rule needs |
|---|---|---|
| **fires at the command** | `agent_hook` + `matcher {event: bash \| edit}` or `ordering` | a pattern over the command / edit, or "green X after edits, before Y" |
| **fires on the error** | `agent_hook` + `matcher {event: output}` | a pattern over the tool result |
| **shown at session start** | `session_context` | nothing checkable — a sentence Claude sees once |
| **fires when the name comes up** | `anchor_recall` + `anchors: [...]` | identifiers; the server matches and judges relevance — not replayable |

Each row ends in a decision: **Turn on** · **Turn on as a session-start
note** (a hook would nag, or the engine can't fire it yet) · **Skip** (too
rare, or never seen). Session-start is the fallback, not the goal: advice
shown 40 turns before it matters is acted on far less than advice at the
violating command.

Words that never reach the user: *precision, applies-in, gated, receipt,
demoted, matcher, ordering, predicate, delivery*. They live in
`proposals.json` and on the one `evidence:` line per row that `create-rule`
and `import-claude-md` parse.

```
transcripts (local: Claude Code · Codex · Cursor, via the memhub plugin readers)
   │
   ├─ digests ──► YOU write facets.json: goal, outcome, friction, corrections per session
   │              (what went wrong — no other tool to run first; works in every host)
   ├─ CLAUDE.md ──► the imperative sentences (what we SAY we do)
   ▼
mine_sessions.py ──► one row per candidate, replayed with the real hook evaluate()
   │   Why   — CLAUDE.md sentence quoted (broken N×) | past sessions (N, user's words)
   │   Cost  — corrections / reverts / facet friction in the sessions it would have fired in
   │   With it on — one sentence + when it fires (at the command · on the error · at session start)
   │   → Turn on | Turn on as a session-start note | Skip
   │   + skills users retype by hand, + block candidates (a command later undone / questioned)
   ▼
file as proposed, source_ref = sessions@<date>#<title>|applies N/M|precision K/N
   │   rule  → create_rule  (rulebook brain)  — never activated here
   │   skill → create_skill (repo brain, host-agnostic SKILL.md)
   │   block → settings snippet in proposals.json / plugin PR
   ▼
reviewer activates ──► next run with --baseline-date measures whether friction shrank
facets.json ──► saved to the repo brain as an artifact, so the team's friction accrues in one place
```

A proposal without a checkable predicate cannot be backtested and is not
filed as a hook rule. Every filed row carries the sessions that justify it.

## 0. Prerequisites

- Script path: `${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py`
  in Claude Code; in Codex / Cursor the same file relative to this skill's
  directory (`scripts/mine_sessions.py`). It finds the plugin's `scripts/`
  next to it, so no env var is needed when shipped inside the plugin.
- `--rule-file <create_rule body>` (repeatable) backtests ONE candidate —
  this is what `create-rule` and `import-claude-md` call to get
  `applies-in N/M` before filing. A body with `matcher` joins "before a
  command / edit" or "after an error"; one with `ordering` is replayed with
  the engine's receipt semantics; one with `anchors` is listed unmeasured.
- The memhub plugin installed (any host). The script reuses its
  `scripts/readers/` (Claude/Codex/Cursor → one record shape) and
  `scripts/rulebook_hook.py` (`to_hook_rule`, `evaluate`, `shell_only`).
- memhub tools `list_rules`, `create_rule`, `list_skills`, `create_skill`.
- Nothing else to run first. (If Claude Code's `/insights` was ever run, its
  facets under `~/.claude/usage-data/facets/` are picked up as an extra seed.)

## 1. Deterministic pass — every session, no model call

```bash
# save list_skills (all statuses) to skills.json for skill dedup, then:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py" --out mine-out \
  --skills-file skills.json --claude-md ./CLAUDE.md [--claude-md <workspace>/CLAUDE.md] \
  [--repo <name>] [--baseline-date YYYY-MM-DD]
```

It prints the report (sections in §2) and writes `mine-out/proposals.json`
— one row per candidate with `trigger` (`session_start` / `before_action` /
`after_error` / `on_identifier`), `delivery`, `predicate`, `why` (the origin
sentence), `claude_md` (the quoted sentence, or null), `evidence`
(corrections / reverts / friction in the fired sessions, on-topic quotes),
`what`, a ready `statement` ("<what>. Why: <why>") for `create_rule`, fired
sessions by host, samples, `verdict`, `source_ref`
(`claude_md@…` or `sessions@…`), and for block candidates a PreToolUse
settings snippet — plus `mine-out/corpus.json`.

It also writes `mine-out/digests/<session>.json` for the top sessions
(`--digest-top`, default 30) ranked by correction turns, errors, and
reverts — the sessions worth a model's attention.

## 2. Facet pass — you read the digests, you write facets.json

This is the step Claude Code's `/insights` does with a hidden model call;
here YOU are the model, so it works the same in Codex and Cursor and costs
the user nothing beyond this session. Read each digest (first prompt,
user turns with corrections marked, errors, reverts — not the transcript)
and write one object per session to `mine-out/facets.json`:

```json
[{"session_id": "…", "host": "claude", "repo": "…",
  "underlying_goal": "one sentence",
  "outcome": "achieved | mostly | partial | not",
  "friction": [{"category": "wrong_approach | misunderstood_request | buggy_code | unverified_claim | wrong_environment | wrong_source | autonomy_overreach | environment_issue | tool_failure",
                "detail": "one sentence: what happened and what the agent should have done",
                "evidence_turn": 4}],
  "corrections": ["the user's own words, verbatim, ≤120 chars"]}]
```

Rules: the friction vocabulary is FIXED (the script rejects other labels —
free labels are why `/insights` ends up with both `environment_issue` and
`environment_issues`); `detail` must be a conditional a rule could enforce;
a session with no correction, error or revert is `friction: []` — do not
invent one. Then re-run with the facets:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py" --out mine-out \
  --facets mine-out/facets.json --skills-file skills.json --claude-md ./CLAUDE.md
```

The report, in order:

- **WHAT WENT WRONG** — friction counts and every session's details.
  Cluster them by eye into candidate sentences. A cluster with a command
  shape (a `git`, `pytest`, `sed` form; an error signature) gets a
  predicate and enters the replay; one without (reasoning from a README,
  claiming "done" without a live run, guessing which repo was meant) becomes
  a session-start note, with the session count as its evidence.
- **WHAT CLAUDE.MD DECLARES** (with `--claude-md`) — every imperative
  sentence with its heading. Two directions: (1) declared → measured: give
  a sentence a predicate (`RULE_CANDS` / `OUTPUT_CANDS` / `ORDERING_CANDS`,
  or a `--rule-file`), re-run, and the replay says how often the declared
  rule is actually broken — that number goes into the `source_ref` when you
  file it through `import-claude-md` (identity stays `(path, title)`, so a
  re-import replaces, never twins); (2) measured → declared: a transcript
  finding with no CLAUDE.md sentence is a CLAUDE.md gap — propose the
  sentence in the report (a PR, never a direct edit).
- **DID FRICTION SHRINK?** (with `--baseline-date`) — facet friction per
  session before vs after the date a rule set went live. The outcome metric.
- **RULES ALREADY ON** — the cached active book replayed. A rule that
  never fired across the corpus is a retire candidate.
- **WHAT THESE RULES WOULD HAVE CHANGED** — the summary the user reads
  first: how many rules would have caught a mistake and in how many
  session-moments; how many are already in CLAUDE.md *and were still
  broken* (CLAUDE.md is read once; a rule fires at the command); how many
  guard things CLAUDE.md never mentions; in how many of those sessions the
  user had to correct Claude; what was skipped as too rare.
- **PROPOSED RULES**, grouped by when they fire (session start · at the
  command · on the error · when the name comes up). Every row is the same
  five lines: `Why:` (origin), `Cost:` (corrections / reverts / friction in
  those sessions, plus the user's on-topic words), `With it on:`, `→`
  decision, and one `evidence:` line with the machine tokens. A "do X
  before Y" matcher whose fires were mostly in sessions that had already
  done X is moved to session start with its numbers. A session-armed
  ordering (`armed_by_events: ["session"]`, e.g. fetch before reading
  `origin/*`) is replayed but the shipped engine is edit-armed only — its
  decision says so; file it as a note and carry the number as evidence for
  the plugin change. Output rules must be anchored to the line start, or
  prose that merely mentions the error becomes the main false hit.
  Session-start rows from the facet clusters (reasoning from a README,
  claiming "done" without a live run, guessing which repo) are written by
  hand with the same five lines: the session count and the user's words are
  the origin.
- **SKILLS** — sessions whose user turns match an intent vs sessions where
  that skill was invoked: `PROPOSE this skill` (none exists), `skill exists
  but was retyped by hand` (adoption gap), `covered`.
- **BLOCK CANDIDATES** — a command followed, in the same session, by an
  undo or by the user questioning it. Rate = sessions where it went bad.
  Advise-tier → a rule above; block-tier → the emitted PreToolUse snippet
  (Claude `settings.json`; Codex/Cursor via the plugin's hook bridges) or a
  plugin PR.

Add hypotheses by editing `RULE_CANDS`, `OUTPUT_CANDS`, `ORDERING_CANDS`,
`SKILL_INTENTS`, `HOOK_CANDS` at the top of each section, or pass them as
`--rule-file` bodies. Give each one `did` (what Claude did, past tense),
`what` (what changes with it on), `claude_md_rx` (how to find the CLAUDE.md
sentence that declares it — explicit, never guessed) and `quote_rx` (which
user corrections count as on-topic). Every entry is a predicate, so it is
backtested the same way.

## 3. Verify, dedup, file

1. Hook rules: every candidate through the plugin's
   `rulebook_verify.py --rule-file … --fires … --silent …` using real
   commands from `corpus.json`; always include a `--silent` case that merely
   MENTIONS the trigger (`grep "…"`, quoted args) — the largest measured
   false-fire class. Then `rulebook_conflicts.py --candidates … --existing
   <list_rules.json> --repo <repo>`; judge `same_title`/`same_matcher`, set
   `supersedes_rule_id` when it replaces a rule.
2. Skills: only `PROPOSE this skill` rows. Write the SKILL.md host-agnostic —
   shell + files, no host-only tools — and cite the session counts in it.
3. Show the user the summary block, then one entry per proposal in the
   report's own five-line shape (Why · Cost · With it on · decision), grouped
   by when it fires, plus *already in the book?* and the `source_ref`. A
   proposal with no origin line is not shown. Get a yes.
4. File: `create_rule` with the row's `delivery` (`agent_hook` +
   `matcher`/`ordering`, `session_context`, or `anchor_recall` + `anchors`)
   and the row's `statement` — it already carries the origin
   ("… Why: your CLAUDE.md says …, broken anyway in N sessions" or "… Why:
   from your sessions, N of M, e.g. <session>"), so a reviewer opening the
   rulebook later sees the reason without this report. Lands `proposed`;
   never pass `activate` from this skill. `create_skill` into the repo
   brain; block snippets left in `proposals.json` for a settings/plugin PR.

## 4. Send the facets to the team, then report

Save `mine-out/facets.json` to the repo's brain as an artifact (one command,
same as the save-artifact skill; re-upload under the same name to version
it, never a second name):

```bash
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
  --file mine-out/facets.json --name "session-facets" --agent-brain-id <repo brain id>
```

That is what makes friction a TEAM number rather than one laptop's: the
next run (yours or a teammate's) can pull the artifact, and the fires
ledger shares `session_id` with it, so "rule fired, friction still
happened" is a join.

## 5. Report and close the loop

Per row: filed (with its trigger) / superseded-what / skipped-why. Then:
rules in the book with zero historical fires (retire candidates); skills
with intent ≫ invoked (adoption work); block candidates with a high
bad-outcome rate; session-armed orderings waiting on the engine mode.
Note the activation date — the next run with `--baseline-date <that date>`
over fresh sessions (with a new facet pass) is the measurement of whether
the friction shrank. Identical re-files are no-ops on the server, so
re-running is safe.
