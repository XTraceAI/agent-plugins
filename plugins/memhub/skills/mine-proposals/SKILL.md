---
description: Use when the user wants to derive team rules, skills, or hooks from past coding sessions — "/memhub:mine-proposals", "mine our sessions for rules", "what should be in the rulebook", "propose skills from what we keep doing", "backtest this rule", "did the new rules reduce friction" — or right after a Claude Code /insights run. Reads local Claude Code, Codex, and Cursor transcripts plus CLAUDE.md, scores every candidate against the transcripts (applies-in N/M, precision, samples), and files survivors as proposed rules / skills; hooks land as settings snippets. Never activates anything.
argument-hint: [--repo <name>] [--claude-md <path>] [--baseline-date YYYY-MM-DD] [--brain "<rulebook brain name>"]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_skills, mcp__plugin_memhub_memhub__create_skill, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_skills, mcp__plugin_memhub-staging_memhub__create_skill
---

# Mine proposals from past sessions

```
transcripts (local: Claude Code · Codex · Cursor, via the memhub plugin readers)
   │
   ├─ digests ──► YOU write facets.json: goal, outcome, friction, corrections per session
   │              (what went wrong — no other tool to run first; works in every host)
   ├─ CLAUDE.md ──► declared imperative sentences (what we SAY we do)
   ▼
proposal miner (scripts/mine_sessions.py) ──► three lanes, each with a checkable "would-apply" predicate
   │   rule   → matcher over tool calls           (replayed with the real hook evaluate())
   │            or ordering: green X after edits, before Y  (violation rate per session)
   │   skill  → user-intent pattern over turns    (intent N / invoked K → propose or adoption gap)
   │   hook   → trigger → later outcome → repair  (same session: "had to be undone")
   ▼
backtest over the same traces ──► applies-in N/M sessions (by host), precision, sample sessions
   ▼
file as proposed, source_ref = <seed>@<date>#<cluster>|applies N/M|precision P
   │   rule  → create_rule  (rulebook brain)         — never activated here
   │   skill → create_skill (repo brain, host-agnostic SKILL.md)
   │   hook  → settings snippet in proposals.json / plugin PR
   ▼
reviewer activates ──► next run with --baseline-date measures whether friction shrank
facets.json ──► saved to the repo brain as an artifact, so the team's friction accrues in one place
```

A proposal without a would-apply predicate cannot be backtested and is not
filed. Every filed row carries the sessions that justify it.

## 0. Prerequisites

- Script path: `${CLAUDE_PLUGIN_ROOT}/skills/mine-proposals/scripts/mine_sessions.py`
  in Claude Code; in Codex / Cursor the same file relative to this skill's
  directory (`scripts/mine_sessions.py`). It finds the plugin's `scripts/`
  next to it, so no env var is needed when shipped inside the plugin.
- `--rule-file <create_rule body>` (repeatable) backtests ONE candidate —
  this is what `create-rule` and `import-claude-md` call to get
  `applies-in N/M` before filing.

- The memhub plugin installed (any host). The script reuses its
  `scripts/readers/` (Claude/Codex/Cursor → one record shape) and
  `scripts/rulebook_hook.py` (`to_hook_rule`, `evaluate`). Auto-detected via
  `$MEMHUB_PLUGIN_SCRIPTS`, `$CLAUDE_PLUGIN_ROOT/scripts`, or the installed
  plugin cache; several copies → newest, with a stderr warning.
- memhub tools `list_rules`, `create_rule`, `list_skills`, `create_skill`.
- Nothing else to run first. (If Claude Code's `/insights` was ever run, its
  facets under `~/.claude/usage-data/facets/` are picked up as an extra seed.)

## 1. Deterministic pass — every session, no model call

```bash
# save list_skills (all statuses) to skills.json for skill-lane dedup, then:
python3 scripts/mine_sessions.py --out mine-out --skills-file skills.json \
  --claude-md ./CLAUDE.md [--claude-md <workspace>/CLAUDE.md] [--repo <name>] [--baseline-date YYYY-MM-DD]
```

It prints and writes `mine-out/proposals.json` (one row per candidate:
lane, title, predicate, sessions by host, precision/rate, 3 samples,
suggested `source_ref`, suggested action, and for hooks a PreToolUse
settings snippet) plus `mine-out/corpus.json`.

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
python3 "${CLAUDE_PLUGIN_ROOT}/skills/mine-proposals/scripts/mine_sessions.py" --out mine-out \
  --facets mine-out/facets.json --skills-file skills.json --claude-md ./CLAUDE.md
```

Sections, in order:
- **FACETS SEED** — friction counts + every session's `friction_detail`.
  Cluster these by eye into candidate sentences; each cluster needs a
  predicate before it can enter a lane.
- **CLAUDE.MD SEED** (with `--claude-md`) — every imperative sentence
  (never / always / must / before …) with its heading. Two directions:
  (1) declared → measured: map a sentence to a predicate in `RULE_CANDS` /
  `ORDERING_CANDS`, re-run, and the replay says how often the declared rule
  is actually violated — that number goes into the `source_ref` when you
  file it through `import-claude-md` (identity stays `(path, title)`, so a
  re-import replaces, never twins); (2) measured → declared: a transcript
  finding with no CLAUDE.md sentence is a CLAUDE.md gap — propose the
  sentence in the report (a PR, never a direct edit).
- **FRICTION DELTA** (with `--baseline-date`) — facet friction per session
  before vs after the date a rule set went live. The outcome metric.
- **LANE 1 · RULES** — live-book replay (a rule with ZERO historical fires is
  a retire candidate) then `RULE_CANDS` / `OUTPUT_CANDS`. For "do X before
  Y" rules set `requires_prior_rx`: precision = fired-with-no-prior-X /
  fired. Below ~50 % the rule is a nag — make it posture (`session_context`)
  or wait for an ordering-engine mode that isn't edit-armed. `ORDERING_CANDS`
  replays "green unpiped X after edits, before Y" with the engine's own
  receipt semantics (last segment, not piped) — use it when a matcher shows
  few fires but the hook lane shows the trigger and the gate in DIFFERENT
  calls (that is the ordering shape, not a matcher).
- **LANE 2 · SKILLS** — `SKILL_INTENTS`: sessions whose user turns match the
  intent vs sessions where that skill was invoked. Verdicts: `PROPOSE skill`
  (no such skill), `adoption gap` (exists, retyped), `covered`.
- **LANE 3 · HOOKS** — `HOOK_CANDS`: trigger → outcome → optional repair,
  all later in the same session. Rate = sessions where the trigger was
  followed by the bad outcome. Advise-tier → a rulebook rule; block-tier →
  the emitted PreToolUse snippet (Claude `settings.json`; Codex/Cursor via
  the plugin's hook bridges) or a plugin PR.

Add hypotheses by editing the three lists at the top of each lane; each
entry is a predicate, so it is backtested the same way.

## 3. Verify, dedup, file

1. Rules: every `agent_hook` candidate through the plugin's
   `rulebook_verify.py --rule-file … --fires … --silent …` using real
   commands from `corpus.json`; always include a `--silent` case that merely
   MENTIONS the trigger (`grep "…"`, quoted args) — the largest measured
   false-fire class. Then `rulebook_conflicts.py --candidates … --existing
   <list_rules.json> --repo <repo>`; judge `same_title`/`same_matcher`, set
   `supersedes_rule_id` when it replaces a rule.
2. Skills: only `PROPOSE skill` rows. Write the SKILL.md host-agnostic —
   shell + files, no host-only tools — and cite the session counts in it.
3. Show the user one table: lane · title · applies-in N/M (by host) ·
   precision/rate · exists/conflicts · source_ref. Get a yes.
4. File: `create_rule` (lands `proposed`; never pass `activate` from this
   skill), `create_skill` into the repo brain, hook snippets left in
   `proposals.json` for a settings/plugin PR.

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

Per row: filed / superseded-what / skipped-why. Then: live rules with zero
historical fires (retire candidates); skills with intent ≫ invoked
(adoption work); hooks with a high bad-outcome rate (block-tier
candidates). Note the activation date — the next run with
`--baseline-date <that date>` over fresh sessions (with a new facet pass)
is the measurement of whether the friction shrank. Identical re-files are
no-ops on the server, so re-running is safe.
