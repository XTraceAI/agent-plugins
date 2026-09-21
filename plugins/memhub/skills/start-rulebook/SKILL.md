---
description: Use when the user wants rules for the Rulebook — a first set for a repo that has none, or rules from what their team actually does. "/memhub:start-rulebook", "set up a rulebook for this repo", "what rules should we start with", "give us the default rules", "bootstrap rules for a new client", "mine our sessions for rules", "turn our CLAUDE.md into rules", "what should be in the rulebook", "backtest this rule", "did the new rules reduce friction" — or right after a Claude Code /insights run. Asks first which the person wants: STARTER rules (a tested catalog of universal coding-agent rules, filled in from a scan of this repo — seconds) and/or rules MINED from their own CLAUDE.md and the last 30 days of local Claude Code / Codex / Cursor sessions (minutes — it reads their sessions). Every candidate is replayed through the real hook and says why it exists, what it cost, and what changes with it on. Hook rules first, session-start notes last. Files survivors as proposed; never activates anything.
argument-hint: [--starter | --mine] [--days N | --all] [--repo <name>] [--claude-md <path>] [--baseline-date YYYY-MM-DD] [--rulebook "<name or id>"] [--dry-run]
allowed-tools: Bash, Read, Write, Agent, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_rulebooks, mcp__plugin_memhub_memhub__create_rulebook, mcp__plugin_memhub_memhub__list_skills, mcp__plugin_memhub_memhub__create_skill, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rulebooks, mcp__plugin_memhub-staging_memhub__create_rulebook, mcp__plugin_memhub-staging_memhub__list_skills, mcp__plugin_memhub-staging_memhub__create_skill
---

# Rules for the Rulebook — a starter set, their own, or both. One run

The user runs this once. Up to three inputs, one table, one yes:

```
starter catalog ◄── scan of the repo ──┐                                                                  (seconds)
CLAUDE.md sentences ───────────────────┼─► every candidate gets a check ─► replayed over the user's sessions ─► one table ─► yes ─► create_rule (proposed)
past sessions (last 30 days) ──────────┘   (at the command · on the error · when a name comes up · note last)   (minutes)
```

**Ask which before doing anything (step 0a).** The two sources answer
different questions. *Starter* rules are what every team running a coding
agent wants — irreversible git, secrets, the suite before push, big files read
as slices — written and tested once, then filled in with this repo's own
branch, test command, manifests and migrations. They need nothing but a
checkout, which is why they are what a new team runs on day one. *Mined*
rules are this team's own: what their CLAUDE.md declares and what their
sessions show going wrong. They need history, and they take time.

Every proposed rule answers three questions, in this order — a candidate
that cannot answer the first is not proposed:

1. **Why does it exist?** One of four origins, nothing else:
   - **starter** — *a universal rule, fitted to your repo* (the value that
     came from the scan — "`main`, from origin/HEAD" — and, where the replay
     ran, what it would have fired on in your own sessions),
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

- Script: `${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py`
  (in Codex / Cursor: `scripts/mine_sessions.py` relative to this skill). It
  finds the plugin's `scripts/` next to it — no env var.
- Starter script: `${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/starter_rulebook.py`
  with the catalog beside the skill (`catalog.json`). `all --repo . --out DIR`
  runs `scan` → `seed` → `verify`; each is also a subcommand.
- **The window: the last 30 days, unless the person asks for more.**
  `mine_sessions.py` reads only sessions active in the last 30 days (by each
  transcript's last activity). Pass `--days N` when they name a period ("the
  last quarter" → `--days 90`). Pass `--all` **only when they ask for all of
  it in their own words** ("everything", "my whole history") — never on your
  own initiative, and never to make a thin report look fuller. A month is the
  right default for a reason: a rule should answer to how the team works
  *now*, and a habit they dropped in the spring still "fires" in March's
  transcripts. With `--baseline-date` and no `--days`, the window widens by
  itself to keep 30 days before the baseline. The report's `window:` line says
  what was read and how many older transcripts were not — repeat it to the
  user. (Claude Code itself deletes transcripts after about 30 days unless
  `cleanupPeriodDays` was raised, so for most people `--all` adds little.)
- Inputs it takes: `--days N` / `--all`, `--claude-md <path>` (repeatable), `--candidates <json
  list>` (repeatable: the checks you derive in step 2), `--rule-file <body>`
  (one check — what `create-rule` calls for its backtest), `--facets <file
  or dir>` (repeatable), `--skills-file`, `--repo`, `--baseline-date`,
  `--digest-top`, `--digest-batch`, `--cache-dir`.
- The memhub plugin installed (any host): the script reuses its
  `scripts/readers/` and `scripts/rulebook_hook.py` (`to_hook_rule`,
  `evaluate`, `shell_only`) — the real hook, never a re-implementation.
- memhub tools `list_rulebooks`, `list_rules`, `create_rule`, `create_rulebook`,
  `list_skills`, `create_skill`.
- Arguments: `--rulebook "<name or id>"` → the destination `rulebook_id`
  (`--brain` is still accepted for it); `--starter` / `--mine` → answers step
  0a without asking; `--days N` / `--all` → the window; `--dry-run` →
  everything except the `create_rule` calls.

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
If the server has no `list_rulebooks`, it predates rulebook containers: file
with no `rulebook_id` and carry on. Never pass `agent_brain_id` to
`create_rule` — the parameter no longer exists and the call fails outright.

**When the create is refused.** `create_rulebook` validates the creator as an
active org member, so it can answer `rulebook_member_not_in_org` naming *the
user themselves* — even though you named nobody. That is not a bug to retry:
their org membership is inactive, and no rulebook can be created until someone
fixes it in MemHub. Say that plainly and stop. (`rulebook_name_too_long` means
the name exceeded 200 characters — shorten it and retry once.)

## 0a. Ask what they want — first, in plain words

Assume the person installed MemHub this week. They may not know what a rule
is, that there are two places rules can come from, or that one of them is
slow. So before any scan or any session is read, say this (your own words are
fine; the content is not optional):

> **A rule is a short instruction your coding agent gets at the exact moment
> it matters** — "you're about to force-push", "this file is 4,000 lines, read
> a slice" — instead of a line in a doc it read once and forgot. A rulebook is
> your team's set of them. I can build yours two ways:

Then ask with AskUserQuestion — **one question, single-select, these three
options in this order**:

| option | label | description to show |
|---|---|---|
| 1 | **Starter rules** | "A tested set of rules every team wants — no force-pushes, no reading secrets, run the tests before pushing, don't read huge files whole. I scan this repo and fit them to it (your branch, your test command). **About a minute.** Best if you're just getting started." |
| 2 | **Rules from my own work** | "I read your CLAUDE.md and your last 30 days of coding sessions to find what actually goes wrong for you, and propose rules for that. **Takes 10–20 minutes** — I'm going through your sessions one by one. Best once you've been using your agent for a few weeks." |
| 3 | **Both** | "Starter rules now, plus your own on top, de-duplicated into one list. **10–20 minutes**, almost all of it the session reading." |

Mark the recommended one from what you can see, and say why in one clause:
- no local sessions in the window for this repo **and** no CLAUDE.md →
  recommend **1**, and say that 2 and 3 would find nothing yet ("come back in
  a couple of weeks — I'll have sessions to learn from");
- sessions exist and the rulebook is empty → recommend **3**;
- the rulebook already holds `starter-rulebook@` rows → recommend **2**.

Skip the question only when they already answered it: `--starter` / `--mine`,
or their own words ("just the defaults" → 1; "mine our sessions", "turn our
CLAUDE.md into rules" → 2). "Set up our rulebook" is NOT an answer — ask.

Whatever they pick, close the loop in one sentence before you start: **nothing
I file turns on by itself — every rule lands as a proposal, and you (or your
admin) switch on the ones you want in MemHub.** A new user's first fear is
that this will start blocking their work. It won't, and they should hear that
before the wait, not after.

**Before the slow part starts, say so.** If they chose 2 or 3, tell them
right before step 1 — and again if the facet pass (step 3) is large:

> This part takes a while — I'm going through your last 30 days of sessions
> to learn how you work and discover rules worth having. Roughly 10–20
> minutes for a month of daily use. You can keep working; I'll come back with
> one list for you to say yes or no to.

Give the real numbers as soon as step 1 prints them ("142 sessions, 30 of them
worth a close read") so the wait has a shape. Never go silent for minutes on a
new user: one line when the first pass finishes, one when the readers are out,
one when they are back.

Then route:

| they chose | run |
|---|---|
| 1 Starter | S1 → S2 → S3, then 5 → 6. **Skip 1–4 entirely** — no facet pass, no readers. |
| 2 Their own | 1 → 2 → 3 → 4 → 5 → 6 (the S steps are skipped). |
| 3 Both | S1 first (seconds), then 1 → 4 with the starter candidates passed into the same replay, S2 → S3 for the starter half, then 5 → 6 once, over everything. |

## S1. Starter: scan the repo, fill the catalog, prove every rule

```bash
OUT="${TMPDIR:-/tmp}/start-rulebook"
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/starter_rulebook.py" all \
  --repo . --out "$OUT"; echo "rc=$?"
```

Read-only over tracked files, no network, writes nothing in the repo. It
leaves:

- `signals.json` — what it found and where: default branch, toolchain, test
  and lint commands, manifests and lock tool, test layout, slow markers,
  migrations, dev server, generated files, the largest files, `.gitignore`
  exclusions and secrets, protected paths, CI and production workflows, infra.
- `candidates.json` — one `create_rule` body per seeded rule (`body`), with
  `category`, `designed_mode`, `seeded_from`, `evidence`, `cases`.
- `dropped.json` — every catalog rule left out and why, in the client's words
  ("this repo has no migrations directory"). **Dropping is the feature:** a
  rule about alembic in a repo without it is day-one noise. A rule whose
  signal is missing is never filed with a guessed value.
- `verified.json` — each candidate run through `rulebook_verify.verify`, the
  live hook's own engine, against the catalog's `fires` / `silent` cases
  re-seeded with this repo's values, plus the `grep` / `python -c`
  self-mention cases.

**Show the WHAT THE SCAN FOUND block and let them correct it.** It is
heuristics over file names. The two it gets wrong most: the default branch
when the team merges somewhere other than `origin/HEAD` (a `staging` flow),
and the test command when the repo wraps it (`make check`, `nox`, `tox`). On
a correction, edit the slot in `signals.json` and re-run `seed` then `verify`
— never hand-edit a candidate's regex.

**`rc=1` means a candidate failed; it is not offered and not filed.** Say
which and why. A failure after seeding is a repo value the catalog's pattern
did not expect — a catalog bug to report, not something to patch in-session.

**If they chose Both**, or chose Starter and have sessions in the window,
replay the starter candidates over their sessions — it is the only evidence
of *usefulness* there is on day one, and it costs under a minute (no readers,
no facet pass):

```bash
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
bodies = [dict(c["body"], title=c["id"]) for c in json.load(open(out + "/candidates.json"))
          if c["body"].get("delivery") == "agent_hook"]
json.dump(bodies, open(out + "/starter-bodies.json", "w"))
PY
# Starter only: its own quick replay. Both: add this --candidates to the step-4 run instead.
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" \
  --out "$OUT/mine" --candidates "$OUT/starter-bodies.json" --repo "<repo>" --digest-top 0   # + --days N / --all if they asked
```

Three things to know before reading a number off that replay:

- **A rule with a `given` block or `scope_paths` is replayed WITHOUT them** —
  a transcript carries no branch, diff, dirty flag or agent identity. "Never
  push to main" firing in every session that pushed is a count of pushes. For
  those rules the number is a ceiling: say "up to N", or say nothing.
- **Zero is not a verdict on a safety rule.** Wiping a home directory, piping
  a download into a shell, tearing down infrastructure — these are insurance
  and should be zero. Zero on a *budget* or *verification* rule means the team
  does not have that problem: default it off and say so.
- **Read the samples of anything that fired in more than ~10% of sessions.**
  Mostly innocent → do not offer the rule. Designed as a gate → file it as
  advice (S2). A gate that fires weekly on ordinary work is overridden by
  habit within a month, and then it protects nothing.

## S2. Starter: ask which problems are theirs — never show sixty rules

`catalog.json`'s categories each carry an `ask` line written for a person who
has never seen a rulebook. AskUserQuestion, `multiSelect: true`, at most four
options per question, and **leave out any category with no seeded rules**:

1. *Cost and speed* — Context budget · Cheap verification · Runtime traps
2. *Safety* — Safety · Infrastructure pack · Subagent guardrails
3. *Quality and intent* — Test hygiene · Repo hygiene · Intent alignment · Posture and anchors

Each option's description carries its rule count and, where the replay ran,
its evidence ("9 rules · 3 would have come up in your last month"). Then one
single-select question — **how firm should they be on day one?**

- **Remind only, to start (Recommended).** Every rule just shows the agent a
  reminder. After two weeks you'll see which ones are quiet enough to let
  block. Nothing can stop your work by surprise.
- **Block the dangerous ones, remind on the rest.** Safety, Infrastructure and
  Subagent rules stop the command; everything else reminds. Pick this if an
  incident is why you're here.
- **As designed.** Every rule keeps the catalog's mode, including "run X
  before you push" blocks. For a team that has run hooks before.

("Remind" is `mode: advise`, "block" is `mode: gate` — use their words with
the user, the field names in the call.) Whatever they pick, the replay
overrides it downward: a designed gate that came up in more than ~10% of their
sessions is filed as a reminder, and the report says so. Say once what a block
means: it stops that command for **everyone the rulebook binds**, and any of
them can still run it with `RULEBOOK_OVERRIDE='<why>'`, which records why.

## S3. Starter: show the shortlist, let them strike rows

One row per rule in the categories they chose — no regex, no ids unless asked:

```
Safety — 14 rules
  Stop irreversible git operations               blocks the command
    when: git push --force · reset --hard · checkout -- . · clean -f · stash drop
    says: "This rewrites or discards history… use --force-with-lease…"
    here: came up in up to 19 of your 142 sessions (mostly `git checkout -- .`)
  No direct push to the default branch           blocks the command
    when: git push while on `main`                ← from origin/HEAD
```

`← from …` is the rule's `seeded_from` whenever a repo value went in, so they
can see it is theirs and correct it. Ask which rows to drop; striking is cheap,
and one unwanted rule is how a whole book gets switched off.

Read them the catalog's **`not_rules`** too — what this set deliberately does
not try to do with a pattern (secrets also belong in `permissions.deny`;
reward hacking and scope creep need structural controls). And if they asked
why a rule they expected is missing, the catalog's **`cut`** list is the honest
answer: rules it used to carry, and the replay evidence that removed each.

## 1. First pass — every session, no model call

**Say the wait out loud first** (step 0a's notice) — this is where it starts.

```bash
git rev-parse --show-toplevel; git rev-parse --short HEAD     # provenance for the CLAUDE.md rows
# The SELECTION — which sessions this run is about. Set it once; every miner call below takes it.
SEL=(--repo "<name>")                      # add as they apply:  --days N | --all    --baseline-date YYYY-MM-DD
# save list_skills (all statuses) to skills.json for skill dedup, then:
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" --out mine-out "${SEL[@]}" \
  --skills-file skills.json --claude-md ./CLAUDE.md [--claude-md <workspace>/CLAUDE.md]
```

**Every miner invocation in this run carries the same `"${SEL[@]}"`** — this
one, the S1 starter replay, and the step-4 second pass. They are not
defaults to re-derive: drop `--repo` from the second pass and it rebuilds the
report from every repo on the machine; drop `--days` / `--all` and the session
counts change under a user who already read digests from the other corpus;
drop `--baseline-date` and "did friction shrink?" silently disappears. If the
two passes print different `sessions read` / `window:` lines, stop — the
selection drifted, and the table would not be about the sessions they reviewed.

No `--days` means the last 30 days; add `--days N` or `--all` only per step
0. Repeat the report's `window:` line to the user.

It prints the report (§4) with the built-in checks replayed, writes
`mine-out/proposals.json` and `mine-out/corpus.json`, and writes
`mine-out/digests/<session>.json` for the top sessions not yet faceted
(`--digest-top`, default 30) ranked by correction turns, errors and reverts,
split into `mine-out/digest_batches.json` (`--digest-batch`, default 5). Its
**WHAT CLAUDE.MD DECLARES** section lists every imperative sentence with
its heading — the input to step 2.

**A session is read once.** Facets from earlier runs live in
`~/.config/memhub-plugin/rules-from-sessions/facets.json` and count in every
later report without being passed; a session is offered for reading again only when it has grown
since its facet was written. `mine-out/facets.merged.json` holds every facet
for this run's sessions, earlier runs' included. Delete `facets.json` to read
everything again — after changing the facet schema, say.

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

## 3. Facet pass — one reader per batch, in parallel

`mine-out/digest_batches.json` lists the digests still to read, in batches.
If it is `[]`, every session with signal is already faceted: go straight to
the clustering below.

Each batch gets one reader. In Claude Code, send one Agent call per batch,
all in a single message so they run at once; on a host without subagents,
read the batches yourself in turn. A reader's prompt carries its digest
paths, the schema and the rules below, and its one output path,
`mine-out/facets/batch-<n>.json` — and tells it: read only those digests
(first prompt, user turns with corrections marked, errors, reverts — not the
transcript), write only that file, run no git or any other command that
changes state, and reply with the path alone. When they return, check each
file exists and parses; a batch that failed stays unfaceted, and so does a
facet missing its `friction` list or `outcome` (the script warns and skips
it) — the next run offers those sessions again. The script empties
`mine-out/facets/` when it writes the batches, so a file there is always this
round's and a reader that wrote nothing leaves none — which is why step 1 is
run once per round, not again between the readers and step 4. Each file holds one object per session, `session_id` and
`stamp` copied from its digest:

```json
[{"session_id": "…", "stamp": "…", "host": "claude", "repo": "…",
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
mined instead of guessed; leave it out rather than flatter.

Then cluster the details — this run's in `mine-out/facets/*.json` and
earlier runs' in `mine-out/facets.merged.json` — by eye, and give each
cluster a check the same way as step 2 (hook
lanes first): a `git` / `pytest` / `sed` form → `matcher`; an error
signature → `matcher {event: output}`; an identifier (a repo name,
`arxiv.org`, `README.md`) → `anchors`; none of those → a session-start
note, written by hand with the session count and the user's words as its
origin. Add the checkable ones to the same candidates list.

## 4. Second pass — everything replayed, one report

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" --out mine-out "${SEL[@]}" \
  --candidates mine-out/candidates.json --facets mine-out/facets \
  --skills-file skills.json --claude-md ./CLAUDE.md          # the SAME selection as step 1
# they chose Both → one replay for everything: add  --candidates "$OUT/starter-bodies.json"
```

**Both: de-duplicate before anything is shown.** Three of this script's
built-in checks cover the same ground as a starter rule under another title
and another pattern, so the conflict script (step 5) cannot see the pair. The
catalog names each in `same_as`: `suite-before-push` ↔ `tests-before-push`,
`fetch-before-origin` ↔ `fetch-before-origin-read`, `git-irreversible` ↔
`no-force-push`. When both are in this run, **show one row, not two**: keep
the starter rule (it is the tested pattern, fitted to their repo) and put the
mined row's evidence on it — its session count and the user's own words are
the "why it matters *here*" the starter rule otherwise lacks. A mined row that
is *narrower* in a way their sessions justify (they only ever force-push, never
reset) is still one row: the starter rule, with that noted.

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
  ["session"]`, e.g. fetch before reading `origin/*`) is replayed AND armed by
  the shipped engine: `armed_by_events` takes `session` and `prompt` (the
  latter needs `armed_by_rx`) as well as the edit family. Propose it in that
  shape rather than demoting it to a note, and let `/memhub:create-rule`
  step 3 set `min_hook_version`, so a teammate on an older hook gets it as
  advice naming the version it wanted instead of silence.
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
   and name the rule it fights in the report. The `same_as` pairs above
   apply against the BOOK too, in both directions: a starter rule about to be
   filed whose counterpart is already in the book from an earlier mined run
   (or a mined built-in whose starter counterpart is already there, its
   `source_ref` starting `starter-rulebook@`) is a `duplicate` the script
   will not flag — look for those titles in the `list_rules` reply yourself.
   Keep what is in the book and add the new evidence to the report, unless the
   incoming rule is strictly wider (`git-irreversible` over `no-force-push`),
   which is filed with `supersedes_rule_id`. Retired counterpart → someone
   already said no; skip it. A hit marked **`cross_book`**
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
   - **Starter rules**: pass the candidate's `body` from `candidates.json` as
     it is (`source: "authored"`, `source_ref: starter-rulebook@<catalog
     version>#<id>` — keep titles stable, that pair is what makes a re-run
     after a catalog update supersede instead of twin), plus `rulebook_id`.
     `mode` is the one field you set: per the S2 answer and the replay's
     downward override; omit it on `session_context` / `anchor_recall` rows
     (the server refuses one there, and the seeder already left it off).
     Only rows that verified (S1) and that they did not strike (S3).
     Session-start notes are capped at 15 per repo scope server-side and the
     catalog ships four — count what the book already holds first.
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
  --file mine-out/facets.merged.json --name "session-facets" --agent-brain-id <repo brain id>
```

Same name every time, so it versions. That is what makes friction a TEAM
number: it carries every facet for these sessions, earlier runs' included, so
each version is the whole picture rather than one run's slice, and the fires ledger shares
`session_id` with it, so "rule fired, friction still happened" is a join.

**Write the report for someone in their first week.** Lead with the three
things they need, in this order, before any table:

1. **What you have now** — "N rules proposed in *<rulebook>*, which reaches
   <you / your N teammates>": how many starter, how many from their own work.
2. **Nothing is on yet, and how to turn it on** — proposals do nothing until
   someone activates them in MemHub; suggest switching on the reminders in one
   pass and any blocking rule one at a time. For a blocking starter rule say
   plainly that it passed every engine case with this repo's values but has
   not been tried in a live session: run `/memhub:create-rule` §4b on it
   before arming it, and offer to do that now for the two or three they care
   about most.
3. **What to do next, and when** — if they took only the starter set: "use
   your agent normally for two weeks, then run this again and pick *Rules from
   my own work* — by then I'll have sessions to learn from." If they mined:
   the activation date, and that the next run with `--baseline-date <that
   date>` shows whether friction actually fell. If the scan found CI steps
   that run locally (`signals.ci.local_checks`), each is a "run X before push"
   rule worth adding with `/memhub:create-rule`; secrets also belong in
   `permissions.deny`.

Then what was left out and why — `dropped.json` ("no migrations directory"),
verification failures, their own strikes — so an absent rule reads as a
decision, not an oversight.

Report per row: filed (with its trigger, and into which rulebook — name who
that book binds) / replaces which rule / unchanged / skipped-why; `contradicts` verdicts under **Conflicts to resolve**; rules
already on with zero historical fires (retire candidates); skills with
intent ≫ invoked; block candidates with a high bad-outcome rate;
session- and prompt-armed orderings and the `min_hook_version` each carries.
Note the activation
date — the next run with `--baseline-date <that date>` over fresh sessions
(with a new facet pass) measures whether the friction shrank. Identical
re-files are no-ops on the server, so re-running is safe.

## Maintaining the starter catalog

`catalog.json` is data with a test (`tests/starter_rulebook_test.py` seeds it
against Python, Node and bare fixture repos and fails if any rule stops loading
or any case flips). When you change a pattern:

- match at command position with `{{CMD}}` / `{{GIT}}`, never `^` — real
  commands arrive as `cd x && git push`;
- match paths as `(?:^|/)dir/` — the hook is handed absolute paths, so
  `^tests/` never fires on an Edit;
- no quantified group containing `+`, `*` or `|` (`(\s+-\S+)*`) — the hook's
  load lint drops the whole rule, silently;
- `scope_paths` are `fnmatch` globs: `alembic/versions/*`, not the directory;
- ship a `fires` case in chained form and a `silent` case for the
  complied-with form, and replay the change over a real corpus before trusting
  it. Every entry in `cut` and half the `command_not_rx` values came from a
  replay, not from thinking about it.
