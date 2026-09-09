---
description: Use when the user wants rules for the Rulebook from what their team actually does — "/memhub:rules-from-sessions", "mine our sessions for rules", "turn our CLAUDE.md into rules", "what should be in the rulebook", "backtest this rule", "did the new rules reduce friction" — or right after a Claude Code /insights run. One run reads the repo's CLAUDE.md AND the local Claude Code / Codex / Cursor transcripts, replays every candidate through the real hook, and proposes rules that each state why they exist (the CLAUDE.md sentence, or the sessions and the user's own words), what they cost, and what changes with them on. Hook rules first, session-start notes last. Files survivors as proposed; never activates anything.
argument-hint: [--repo <name>] [--claude-md <path>] [--baseline-date YYYY-MM-DD] [--rulebook "<name or id>"] [--dry-run]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_rulebooks, mcp__plugin_memhub_memhub__create_rulebook, mcp__plugin_memhub_memhub__list_skills, mcp__plugin_memhub_memhub__create_skill, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rulebooks, mcp__plugin_memhub-staging_memhub__create_rulebook, mcp__plugin_memhub-staging_memhub__list_skills, mcp__plugin_memhub-staging_memhub__create_skill
---

# Rules from sessions (and CLAUDE.md) — one run

The user runs this once. Two inputs, one table, one yes:

```
CLAUDE.md sentences ──┐
                      ├─► every candidate gets a check ─► replayed over the user's sessions ─► one table ─► yes ─► create_rule (proposed)
past sessions ────────┘   (at the command · on the error · when a name comes up · note last)
```

Every proposed rule answers three questions, in this order — a candidate
that cannot answer the first is not proposed:

1. **Why does it exist?** One of three origins, nothing else:
   - **declared** — *your CLAUDE.md says "…"* (the sentence quoted, its
     heading, and how often it was broken anyway),
   - **observed** — *from your sessions* (how many, plus the user's own
     on-topic words from a session where it bit), or
   - **asserted** — *a human stated an engineering standard* ("always TTL
     new tables", "LLM calls go through the metered path"). The frequency
     bar does NOT apply to asserted standards: one assertion with blast
     radius is enough — a standard is usually said once, in a design or
     review discussion, and then silently violated.

**Two kinds of rules, and the second is the org-valuable one.** Friction
mining produces *how-the-agent-works* rules (fetch first, don't pipe
tests). Asserted standards produce *how-we-build* rules — data growth,
cost paths, scheduling, tenancy — and they are almost always edit- or
anchor-shaped, firing at the change that violates them:

| standard (as asserted) | rule shape |
|---|---|
| "never unbounded growth in tables — always TTL" | edit: migration adds `create_table` with no TTL/retention/partition column |
| "always make LLM calls through the metered path" | edit: a direct provider client (`AsyncOpenAI(`, `anthropic.`) outside the metered module |
| "don't schedule nightly crons at the same time — pace them" | edit: a new cron entry at an already-used hour in the schedule file |

When the user says "org-wide", this is what they mean: hunt the digests'
`standard: true` turns and the facets' `standards` lists, and shape those —
do not lead with machine-local environment friction. Every row carries an
`audience` (org / repo / machine); the report prints org-wide first and
labels machine-local rows so a teammate's run isn't a page of one
laptop's quirks.
2. **What did it cost?** In the sessions it would have fired in: how many
   had the user correcting Claude, how many had a revert, the friction the
   facets recorded there.
3. **What changes with it on?** One sentence, plus *when* it fires.

## The four ways a rule fires — try them in this order

| fires… | rulebook `delivery` | what the rule needs |
|---|---|---|
| **fires at the command** | `agent_hook` + `matcher {event: bash \| edit}` or `ordering` | a pattern over the command / edit, or "green X after edits, before Y" |
| **fires on the error** | `agent_hook` + `matcher {event: output}` | a pattern over the tool result |
| **fires when the name comes up** | `anchor_recall` + `anchors: [...]` | identifiers (a repo name, `arxiv.org`, `README.md`); the server matches and judges relevance — not replayable |
| **shown at session start** | `session_context` | nothing checkable — a sentence Claude sees once |

**Hook lanes first, notes last — this is the rule of the skill, not a
preference.** A session-start note is what CLAUDE.md already is: read once,
forgotten by the time it matters. For every candidate — from CLAUDE.md or
from a friction cluster — ask in order: is there a command shape (`git`,
`pytest`, `sed`, a URL fetch)? an error signature? an identifier? Only when
all three are "no" does it become a note, and the report caps notes at 5;
past the cap, find a shape or drop it.

Each row ends in a decision: **Turn on** · **Turn on as a session-start
note** (a hook would nag, or the engine can't fire it yet) · **Skip** (too
rare, or never seen) · **Declared in CLAUDE.md, not broken here** (0–2
fires — offered in bulk at the end, default no, so they don't dilute the
table).

Words that never reach the user: *precision, applies-in, gated, receipt,
demoted, matcher, ordering, predicate, delivery*. They live in
`proposals.json` and on the one `evidence:` line per row that `create-rule`
parses.

## 0. Prerequisites

- Script: `${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py`
  (in Codex / Cursor: `scripts/mine_sessions.py` relative to this skill). It
  finds the plugin's `scripts/` next to it — no env var.
- Inputs it takes: `--claude-md <path>` (repeatable), `--candidates <json
  list>` (repeatable: the checks you derive in step 2), `--rule-file <body>`
  (one check — what `create-rule` calls for its backtest), `--facets`,
  `--skills-file`, `--repo`, `--baseline-date`, `--digest-top`.
- The memhub plugin installed (any host): the script reuses its
  `scripts/readers/` and `scripts/rulebook_hook.py` (`to_hook_rule`,
  `evaluate`, `shell_only`) — the real hook, never a re-implementation.
- memhub tools `list_rulebooks`, `list_rules`, `create_rule`, `create_rulebook`,
  `list_skills`, `create_skill`.
- Arguments: `--rulebook "<name or id>"` → the destination `rulebook_id`
  (`--brain` is still accepted for it); `--dry-run` → everything except the
  `create_rule` calls.

**Resolve the rulebook before you file anything.** A rulebook is a container
with its own membership — every member's agent is bound by its rules — and one
person can be in several. Call `list_rulebooks` (rows carry `rulebook_id`,
`name`, `scope`, `member_count`, `rule_count`, `bound`, `is_admin`). Match
`--rulebook` by id then by name; with it omitted, one visible book is the
destination and several means **ask** (AskUserQuestion, one option per book
labelled with who it binds) rather than guess. No books at all → offer
`create_rulebook(name: "<repo> rules", scope: "explicit")`, which binds only
the user, and create it only on a yes; never pass `scope: "all_org"` or name
another member — both are org-admin acts. Every proposal you show the user
names the book it would land in, because that is who the rule would reach.
If the server has no `list_rulebooks`, it predates rulebook containers: fall
back to `agent_brain_id` and carry on.

**When the create is refused.** `create_rulebook` validates the creator as an
active org member, so it can answer `rulebook_member_not_in_org` naming *the
user themselves* — even though you named nobody. That is not a bug to retry:
their org membership is inactive, and no rulebook can be created until someone
fixes it in MemHub. Say that plainly and stop. (`rulebook_name_too_long` means
the name exceeded 200 characters — shorten it and retry once.)

## 1. First pass — every session, no model call

```bash
git rev-parse --show-toplevel; git rev-parse --short HEAD     # provenance for the CLAUDE.md rows
# save list_skills (all statuses) to skills.json for skill dedup, then:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py" --out mine-out \
  --skills-file skills.json --claude-md ./CLAUDE.md [--claude-md <workspace>/CLAUDE.md] \
  [--repo <name>] [--baseline-date YYYY-MM-DD]
```

It prints the report (§4) with the built-in checks replayed, writes
`mine-out/proposals.json` and `mine-out/corpus.json`, and writes
`mine-out/digests/<session>.json` for the top sessions (`--digest-top`,
default 30) ranked by correction turns, errors and reverts. Its
**WHAT CLAUDE.MD DECLARES** section lists every imperative sentence with
its heading — the input to step 2.

## 2. Give CLAUDE.md its checks — in your own reading, no model call

Walk the declared sentences. A **rule** is a conditional a teammate can
violate: *when X, do / never Y, because Z*. Skip narrative, architecture
description, and anything a linter or CI already enforces (say "already
enforced by <what>" in the report). For each rule pick ONE delivery, hook
lanes first (table above), and write a `create_rule` body into a JSON list:

```json
[{"title": "dotenv-not-source",
  "delivery": "agent_hook",
  "matcher": {"event": "bash", "command_rx": "(^|[;&|(]\\s*)(source|\\.)\\s+\\S*\\.env\\b",
              "command_not_rx": "python3?\\s+-c\\b|\\brulebook\\b", "warn_once_per": "session"},
  "claude_md": {"heading": "Loading environment variables", "text": "`source .env` will mis-parse it and leak secrets into stderr. Always load it via python-dotenv."},
  "did": "Claude sourced .env", "what": "Claude is warned at `source .env` and pointed to python-dotenv",
  "quote_rx": "\\.env|secret|dotenv",
  "scope_repos": ["<repo>"], "source": "claude_md_import",
  "source_ref": "CLAUDE.md@<sha>#loading-environment-variables"}]
```

- `claude_md` is the origin sentence itself — pass it, don't make the
  script guess. `did` = what Claude did (past tense); `what` = what changes
  with the rule on; `quote_rx` = which user corrections count as on-topic.
- `source_ref = "<path relative to repo root>@<sha>#<heading-slug>"`. The
  identity of a CLAUDE.md rule is **(path, title)**: a re-run with the same
  path and title and identical content is a server no-op (`unchanged`), and
  a changed one is filed with `supersedes_rule_id` (step 5) — so a re-run
  replaces, never twins. Keep the path stable (no absolute paths).
- Title = the heading or a short noun phrase (under 60 chars); one rule per
  title. Statement is composed by the script ("<what>. Why: <origin>") and
  kept under the server's 400-character cap — a longer statement is refused
  by `create_rule`, not truncated, so tighten `what` rather than pad it.
- Apply the matcher rules from `/memhub:create-rule` step 3: match the
  pre-heredoc segment, shape-specific patterns, exemptions in
  `command_not_rx` up front (`python -c`, `grep`), `warn_once_per:
  "session"` by default. Output rules must be anchored to the line start —
  prose that merely mentions the error is the main false hit.

## 3. Facet pass — you read the digests, you write facets.json

Read each digest (first prompt, user turns with corrections marked, errors,
reverts — not the transcript) and write one object per session to
`mine-out/facets.json`:

```json
[{"session_id": "…", "host": "claude", "repo": "…",
  "underlying_goal": "one sentence",
  "outcome": "achieved | mostly | partial | not",
  "friction": [{"category": "wrong_approach | misunderstood_request | buggy_code | unverified_claim | wrong_environment | wrong_source | autonomy_overreach | environment_issue | tool_failure",
                "detail": "one sentence: what happened and what the agent should have done",
                "evidence_turn": 4}],
  "standards": [{"statement": "the engineering standard as a sentence a rule could enforce",
                 "quote": "the human's own words, verbatim", "scope": "org | repo"}],
  "worked_well": "one sentence on what pattern made this session land, if one did",
  "corrections": ["the user's own words, verbatim, ≤120 chars"]}]
```

The friction vocabulary is FIXED (the script rejects other labels); `detail`
must be a conditional a rule could enforce; a session with no correction,
error or revert is `friction: []` — do not invent one. `standards` is where
org rules come from: any turn where a human asserts how the system must be
built (the digests mark candidate turns with `standard: true` — read those
even in sessions with no friction). `worked_well` is how strengths get
mined instead of guessed; leave it out rather than flatter. Then cluster the
details by eye and give each cluster a check the same way as step 2 (hook
lanes first): a `git` / `pytest` / `sed` form → `matcher`; an error
signature → `matcher {event: output}`; an identifier (a repo name,
`arxiv.org`, `README.md`) → `anchors`; none of those → a session-start
note, written by hand with the session count and the user's words as its
origin. Add the checkable ones to the same candidates list.

## 4. Second pass — everything replayed, one report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py" --out mine-out \
  --candidates mine-out/candidates.json --facets mine-out/facets.json \
  --skills-file skills.json --claude-md ./CLAUDE.md
```

The report, in order:

- **ENGINEERING STANDARDS ASSERTED** — the org-rule material, first.
- **WHAT WORKED** — the patterns to keep (skill material).
- **WHAT WENT WRONG** — friction counts and every session's details.
- **WHAT CLAUDE.MD DECLARES** — the sentences (already used in step 2).
- **DID FRICTION SHRINK?** (with `--baseline-date`) — facet friction per
  session before vs after the date a rule set went live. The outcome metric.
- **RULES ALREADY ON** — the cached active book replayed; a rule that never
  fired across the corpus is a retire candidate.
- **WHAT THESE RULES WOULD HAVE CHANGED** — the summary the user reads
  first: how many rules would have caught a mistake and in how many
  session-moments; how many are already in CLAUDE.md *and were still
  broken*; how many guard things CLAUDE.md never mentions; in how many
  sessions the user had to correct Claude; what was skipped; how many are
  declared-but-unbroken; a warning if session-start notes exceed the cap;
  and **coverage** — of the friction items in your facets, how many sit in a
  session one of these rules would have fired in, and the ones left over by
  kind with their details, which are the next candidates (give each a
  shape, or accept it as a one-off).
- **PROPOSED RULES**, grouped by when they fire. Every row is the same five
  lines: `Why:` (origin), `Cost:`, `With it on:`, `→` decision, and one
  `evidence:` line with the machine tokens. A "do X before Y" matcher whose
  fires were mostly in sessions that had already done X is moved to session
  start with its numbers. A session-armed ordering (`armed_by_events:
  ["session"]`, e.g. fetch before reading `origin/*`) is replayed but the
  shipped engine is edit-armed only — its decision says so; the
  fires-at-the-command form (once per session) is the alternative to offer.
- **DECLARED IN CLAUDE.MD, NOT BROKEN HERE** — the 0–2-fire declared checks,
  with their sentences. Offer them in bulk ("also file these as declared
  rules?"), default no.
- **SKILLS** — sessions whose user turns match an intent vs sessions where
  that skill was invoked: `PROPOSE this skill` / `retyped by hand` /
  `covered`.
- **BLOCK CANDIDATES** — a command followed, in the same session, by an
  undo or by the user questioning it. Block-tier → the emitted PreToolUse
  snippet (Claude `settings.json`; Codex/Cursor via the plugin's hook
  bridges) or a plugin PR.
- **REPEATED WORKFLOWS** — the shell chains sessions retype (worktree
  setup, venv bootstrap, test baseline, PR open/watch, repeated heredoc
  analysis), counted per session: Makefile / setup-skill material.

The run also writes **`mine-out/grabs/`** — everything copyable, generated
rather than described: `claude-md-additions.md` (one section per
sessions-origin rule with its evidence line — the human-readable half of
each fired rule), `hooks.settings.json` (the verified block-tier hooks),
and `Makefile.suggested` (targets for workflows used in ≥5 sessions).

Built-in hypotheses live in `RULE_CANDS`, `OUTPUT_CANDS`, `ORDERING_CANDS`,
`SKILL_INTENTS`, `HOOK_CANDS` at the top of each section; each carries
`did`, `what`, `claude_md_rx` (explicit — the script never guesses an
origin) and `quote_rx`.

## 5. Verify, check conflicts, show the table, file

1. Every `agent_hook` candidate through `rulebook_verify.py --rule-file …
   --fires … --silent …` using real commands from `corpus.json`; always a
   `--silent` case that merely MENTIONS the trigger (`grep "…"`) — the
   largest measured false-fire class. Drop a candidate you cannot get to a
   clean run.
2. Conflicts, before anything is filed — the server does no title
   matching, so a collision lands as a second draft silently. Save
   `list_rules` (**`include_retired=True, limit=200`**, and **no
   `rulebook_id`**, so it spans every book the user can see in every state) to
   a file, the candidate bodies to another, and run:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
     --candidates <candidates.json> --existing <list_rules.json> --repo "<repo>" \
     --rulebook-id <the destination rulebook_id>
   ```
   Omit `--rulebook-id` entirely when there is no id (an older backend);
   passing it empty is an argparse error and you get no report at all.
   `include_retired=True` matters: a rule someone already
   dismissed is exactly the twin you must not re-file, and the default view
   hides retired rules. `limit` is 200 at most — if the reply says `has_more`,
   ask again with `offset` and concatenate `rules` before running the check, or
   the comparison silently misses whatever fell off the first page.
   `same_title` / `same_matcher` / `anchors_overlap` hits print the exact
   `supersedes_rule_id` to copy. Then the semantic pass in your own reading
   over `judge_by_statement`: `duplicate` → file with `supersedes_rule_id`;
   `same_matcher` against an active rule that is NOT the same rule → do not
   file, tell the user; `contradicts` → file without `supersedes_rule_id`
   and name the rule it fights in the report. A hit marked **`cross_book`**
   is in another rulebook: `supersedes_rule_id` cannot reach it and both
   rules will fire on the same call, so it goes to the user as a decision,
   never absorbed silently.
3. Skills: only `PROPOSE this skill` rows; host-agnostic SKILL.md citing
   the session counts.
4. Show the user the summary block, then one entry per proposal in the
   report's five-line shape, grouped by when it fires, plus *conflicts* and
   the `source_ref`; then the declared-but-unbroken list as one bulk
   question. A proposal with no origin line is not shown. Get a yes. With
   `--dry-run`, stop here.
5. File — every channel ends as something filed or grabbable, never only
   described:
   - **Rules**: `create_rule` once per row with the destination `rulebook_id`,
     the row's `delivery` and engine block, the row's `statement` (statements
     are capped at 400 chars server-side), `scope_repos`, `source`
     (`claude_md_import` for declared, `authored` for observed and asserted),
     `source_ref` (an asserted standard's ref names the asserter's session),
     and `supersedes_rule_id` where step 2 said so. Everything lands
     `proposed`, advise — never pass `activate` from this skill, not even on
     a book that binds only the user.
   - **CLAUDE.md**: open a PR adding `mine-out/grabs/claude-md-additions.md`'s
     chosen sections to the repo's CLAUDE.md — a PR, never a direct edit.
   - **Skills**: write the full SKILL.md for `PROPOSE this skill` rows and
     for strong `worked_well` patterns, and `create_skill` it into the repo
     brain — an adoption-gap verdict alone files nothing.
   - **Hooks / Makefile**: offer `grabs/hooks.settings.json` and
     `grabs/Makefile.suggested` as a repo PR (`.claude/settings.json`,
     `Makefile`) or leave them for the user to paste.

## 6. Send the facets to the team, then report

```bash
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
  --file mine-out/facets.json --name "session-facets" --agent-brain-id <repo brain id>
```

Same name every time, so it versions. That is what makes friction a TEAM
number: the next run (anyone's) can pull it, and the fires ledger shares
`session_id` with it, so "rule fired, friction still happened" is a join.

Report per row: filed (with its trigger, and into which rulebook — name who
that book binds) / replaces which rule / unchanged / skipped-why; `contradicts` verdicts under **Conflicts to resolve**; rules
already on with zero historical fires (retire candidates); skills with
intent ≫ invoked; block candidates with a high bad-outcome rate;
session-armed orderings waiting on the engine mode. Note the activation
date — the next run with `--baseline-date <that date>` over fresh sessions
(with a new facet pass) measures whether the friction shrank. Identical
re-files are no-ops on the server, so re-running is safe.
