---
description: Use when the user wants rules proposed from what their coding agents actually did — "/memhub:rules-from-sessions", "mine our sessions for rules", "mine rules from sessions", "what should be in the rulebook", "propose skills from what we keep doing", "backtest this rule", "did the new rules reduce friction" — or right after a Claude Code /insights run. Reads local Claude Code, Codex, and Cursor transcripts plus CLAUDE.md, scores every candidate against the transcripts (applies-in N/M, precision, samples), and files survivors as proposed rules / skills; hooks land as settings snippets. Never activates anything.
argument-hint: [--repo <name>] [--claude-md <path>] [--baseline-date YYYY-MM-DD] [--brain "<rulebook brain name>"]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_skills, mcp__plugin_memhub_memhub__create_skill, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_skills, mcp__plugin_memhub-staging_memhub__create_skill
---

# Rules from sessions

The rulebook delivers a rule in one of four ways. Every proposal this skill
makes lands in exactly one of them, and the report is grouped that way, so a
row reads as "this is when it would have fired, this is how often it would
have been right":

| fires… | `delivery` | what the rule needs | the row's numbers mean |
|---|---|---|---|
| **At session start** | `session_context` | nothing checkable — a sentence shown once when the session opens | how many sessions showed the friction (from facets) |
| **Before a command or edit** | `agent_hook` + `matcher {event: bash \| edit}` or `ordering` | a pattern over the command / edit body, or "green X after edits, before Y" | *would have fired in N of M sessions; K of those were real misses* |
| **After an error** | `agent_hook` + `matcher {event: output}` | a pattern over the tool result | *would have fired in N of M sessions* |
| **When a name comes up** | `anchor_recall` + `anchors: [...]` | identifiers; the server matches and judges relevance | not replayable offline — listed as unmeasured |

Session-start is the fallback, not the goal: advice shown 40 turns before it
matters is acted on far less than advice shown at the violating command. A row
goes there only when the behaviour has no command shape, or when a matcher
exists but would mostly hit sessions that had already complied (the report
calls that "would nag" and demotes it for you).

```
transcripts (local: Claude Code · Codex · Cursor, via the memhub plugin readers)
   │
   ├─ digests ──► YOU write facets.json: goal, outcome, friction, corrections per session
   │              (what went wrong — no other tool to run first; works in every host)
   ├─ CLAUDE.md ──► the imperative sentences (what we SAY we do)
   ▼
mine_sessions.py ──► one row per candidate, replayed with the real hook evaluate()
   │   before a command / edit   matcher or ordering → would have fired N/M, real misses K
   │   after an error            output pattern      → would have fired N/M
   │   at session start          demoted matchers + facet clusters with no command shape
   │   when a name comes up      anchors             → listed, unmeasured
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
`after_error` / `on_identifier`), `delivery`, `predicate`, fired sessions by
host, real misses, 3 samples, a one-line `verdict`, the suggested
`source_ref`, and for block candidates a PreToolUse settings snippet — plus
`mine-out/corpus.json`.

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
- **RULES ALREADY IN THE BOOK** — the cached active book replayed. A rule
  that never fired across the corpus is a retire candidate.
- **PROPOSED RULES**, grouped by trigger:
  - *Before a command or edit* — each matcher: `applies-in N/M sessions`,
    and for "do X before Y" rules (`requires_prior_rx`) `precision=K/N real
    misses`. Below 50 % the verdict says **would nag** and the row is
    demoted to session start. Orderings print three numbers: sessions that
    reached the gate after edits, how many had no *receipt-grade* run (the
    engine's semantics: last segment, unpiped), and of those how many ran
    the command *never* vs *only piped* (`pytest | tail` — exit code lost).
    A session-armed ordering (`armed_by_events: ["session"]`, e.g. "fetch
    before reading origin/*") is replayed too, but the shipped engine is
    edit-armed only — its verdict says **needs the session-armed ordering
    mode**; file it as a session note until that plugin change lands, and
    carry the number as the evidence for the change.
  - *After an error* — output patterns; anchor them to the line start, or
    prose that merely mentions the error becomes the main false hit.
  - *At session start* — demoted matchers (with their numbers) and the
    facet clusters you write by hand.
  - *When a name comes up* — anchor bodies, listed unmeasured: the server
    matches and judges relevance, so there is nothing to replay.
  Each row ends in a **verdict**: `file`, `would nag → session note`,
  `rare (<3 sessions) → skip unless one miss is expensive`, `no evidence`,
  `needs the engine mode`, `unmeasured`.
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
`--rule-file` bodies; every entry is a predicate, so it is backtested the
same way.

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
3. Show the user one table, grouped by trigger, one row per proposal:
   **title · fires when (the four triggers) · would have fired in N of M
   (by host) · real misses · verdict · exists / conflicts · source_ref**.
   Session-start rows show the facet session count instead of a replay.
   Get a yes.
4. File: `create_rule` with the row's `delivery` (`agent_hook` +
   `matcher`/`ordering`, `session_context`, or `anchor_recall` + `anchors`;
   lands `proposed`; never pass `activate` from this skill), `create_skill`
   into the repo brain, block snippets left in `proposals.json` for a
   settings/plugin PR.

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
