# S0 scorecard — harness-tied memory, offline replay

> Slice **S0** of `harness-tied-memory-spec.md` v0.3 (MemHub-Backend
> `docs/specs/`). S0 is the offline proving ground that decides whether the
> extraction pipeline is good enough to run live in S1. **S1 is not built.**
> Nothing in this run fired a rule, activated a rule, or wrote to staging.
>
> Measured 2026-09-09 on plugin 0.53.0, branch `fm-feat/harness-replay-s0`.

## What was measured, and on what

**24 correction-bearing sessions from 5 engineers**, 949 human turns, pulled
read-only from staging `team_memory_messages` — the first numbers in this
project that are not one engineer's laptop. Sessions were picked round-robin by
engineer, not "the N biggest": the biggest sessions all belong to the same two
people.

Engineers are labelled `eng-<hash>`. 44 minutes wall clock, 14 sessions in
parallel; 584 judge calls and 229 author calls.

## The scorecard

| metric | target | measured | verdict |
|---|---|---|---|
| judge recall on `clf_gold.json` | ≥ 0.85 | **0.85** among completed calls · **0.77** end-to-end | **MISS** end-to-end |
| rows a human would activate ÷ rows drafted | ≥ 0.5 | **0.68** (21 / 31) | **PASS** |
| duplicate rows within a run | 0 | **0** lexical · **2 pairs** semantic | **MISS** on the metric that matters |
| drafts per session | ≤ 8 | max **5**, mean **1.29** | **PASS** |
| router regex precision | report it | **0.015** (1 row from 68 authored hits) | reported — see Finding 2 |

**Two of five gates miss.** Both misses are mechanism problems with identified
causes, not "the model is not good enough" — see Findings.

### Supporting numbers

| | |
|---|---|
| judge precision (**not a target**, do not tune) | 0.66 (was 0.62) |
| regex baseline on the same 140 items | precision 0.46 · recall 0.74 |
| judge latency | median 15.1 s · p90 25.7 s |
| judge calls lost to the spec's 30 s bound | 105 / 689 = **15%** (corpus) · 29 / 140 = **21%** (gold) |
| author calls that produced a row | 31 / 229 = 0.14 |
| rows by origin | judge **30** · router **1** |

## The rubric I judged by

Each drafted row was read **as a set member**, not in isolation. A row is
**activatable** only if all five hold:

1. **It would change an action.** A reader can name the next command, edit or
   read it would alter. Not a summary, not an observation.
2. **The trigger can actually match.** The regex or anchors would fire on the
   shape of a real future action, and would *not* fire on every member of that
   family. A trigger that matches nothing, and a trigger that matches
   everything, are the same reject.
3. **It is not derivable.** A new engineer would not learn it by reading the
   repo, its tests, its docs, or CLAUDE.md.
4. **It is not project state.** Not "PR #1233 does X", not "we decided Y for
   this ticket", not a restatement of an error message.
5. **It outlives the session.** Still true next month; not tied to one line
   number, one branch, or one PR that will merge.

Reject reasons: `no_action`, `unmatchable`, `derivable`, `project_state`,
`one_off`, `duplicate`, `wrong`.

The state stamp is **not** one of the five. Repo-stamp accuracy is reported
separately (Finding 4), because a wrong `scope_repos` is a one-click fix for a
reviewer while an unmatchable trigger is not.

**One judge, not two.** The spec's gate asks for two people judging
independently. This is one pass, by the agent that built the pipeline — the
weakest possible reviewer, and worth saying plainly. The second judge is
outstanding and the gate is not discharged without it. Every row is in
`judge_sheet.md` with its verdict so a second pass is a read, not a re-run.

### The 10 rejects

| # | title | reason |
|---|---|---|
| 1 | Worktree isolation check refuses long chained bash | `unmatchable` — `(;.*){2,}` fires on most multi-statement commands |
| 6 | Don't block push on a full-suite wait-loop | `unmatchable` — trigger matches the *implementation* of the wait, not the behaviour |
| 9 | Stale local checkout during cross-repo audit | `unmatchable` — gated pattern includes `cat\s|grep\s`, i.e. most reads |
| 17 | Forked sessions share no context | `derivable` — the Agent/SendMessage tool descriptions say this |
| 20 | Unprompted memory-file writes | `duplicate` of 19, same moment, different engine |
| 25 | Passive memory doc vs enforceable rulebook | `one_off` — trigger is one filename; justified by a stat from its own session |
| 27 | Project-scoped skill not callable by name | `one_off` — depends on one session's available-skills listing |
| 28 | Claiming review threads resolved | `unmatchable` — `armed_by_events: ["bash"]`, an event no lane emits |
| 30 | Working in main checkout not worktree | `unmatchable` — trigger hardcodes one engineer's `~/dev` layout |
| 31 | Cross-user filesystem search | `wrong` — regex hardcodes a colleague's username (Finding 1) |

Six of ten rejects are criterion 2. **The pipeline's weak stage is the trigger,
not the sentence**: read as prose, most rejected rows say something true. They
fail because no engine can act on them, or because the engine would act on
everything.

## Findings

### 1. Untrusted tool output shaped a rule, exactly as §4.2 predicts

Row 31 was drafted with the regex `/Users/(?!felixmeng)[A-Za-z0-9_]+/(dev|…)` —
a colleague's username, negative-lookahead'd, baked into a team rule. It did not
leak from the replaying machine. It came from **tool output inside the
replayed session**: an org-members query printed `user_name`, `user_email` and
`user_id` for the whole team, and the author model picked a name out of it.

§4.2 step 1 says tool output "is in the window and is **untrusted**: it can
shape a draft, which is why a draft is a proposal a human reads and never a
fire." This is that sentence, measured. The design holds — a human reads every
row — but two things follow:

- drafted rows can carry teammates' names, emails and home paths (rows 30 and
  31 both do), so **the drafts file is not shareable output**; and
- the window should run through the existing `redact.py` before reaching the
  author. Not done here; it is the first thing S1 should carry.

### 2. The deterministic router is ~all false positives; the judge does the work

Of 292 router hits over 949 turns, 223 are `claim_no_receipt`, which by §1/§5.1
is a built-in Stop check and is not authored. Of the remaining **68 hits that
reached the author, 1 produced a row** — precision **0.015**. Thirty of the 31
rows came from the model judge.

`wrong_target` alone contributed 36 hits and 0 rows: it fires on any mention of
"on staging", including "new commit on staging: 0bc41ff" and "is this factually
accurate: … on staging".

The router's value is therefore **not** precision. It is the 223 claim moments
it detects for free, and the ~28% of turns it lets skip a model call. As a
source of authored rows it currently costs 68 Sonnet calls to buy one. The
honest options are to drop the non-claim regexes and let the judge see those
turns too, or to keep them only as a judge hint. **Not tuned here** — tuning
regexes against the corpus they were measured on is how the number stops
meaning anything.

### 3. The spec's 30 s judge bound loses 15-21% of judgements

At the spec's `judge 30 s` bound the headless CLI path lost **105 of 689**
corpus calls and **29 of 140** gold calls. Median latency is 15.1 s and p90 is
25.7 s, so the bound sits just above p90 and every slow call dies.

This is why recall has two numbers. Among calls that completed, recall is
**0.846** — it clears the ≥0.85 target on rounding. Counting the 4 gold
positives whose calls timed out as the misses they would be in production,
end-to-end recall is **0.767**. The second number is the one that describes
what an engineer would get.

The bound is a property of the CLI path, not of the model: §4.2 specifies the
direct API when `ANTHROPIC_API_KEY` is present, and that path has no ~5 s of
process startup. **This gate is not failed by the judge; it is failed by the
transport S0 measured.** Re-measure on the API path before changing the prompt.

### 4. The per-action state stamp names the wrong repo on cross-repo turns

`state.repo` is resolved per action (§4.2 step 1) from the action the authored
engine matched. On a turn that touches two repos, that is routinely the wrong
one: rows 9, 10 and 11 are about the plugin repo and PR workflow and are all
stamped `repo: xmem`, because the matching Bash call happened to `cd` there.
Filed as-is they would land in the wrong rulebook with the wrong `scope_repos`.

Fixed here to the extent code can: the stamp now carries `touched_repos`
whenever a turn touched more than one repo, so a reviewer sees the ambiguity
instead of one confident wrong value. Choosing which repo a lesson is *about*
is a judgement the stamp cannot make; the spec's own answer is the same one.

A further **19 author-accepted rows were dropped** with `state_missing_repo` —
a teammate's session has no local checkout, and its conversation row carried no
`agentic_namespace` to fall back to. That is 19 candidate lessons lost to
bookkeeping, not to quality.

### 5. The twin check is lexical and misses semantic twins

`duplicate_pairs: 0` by statement similarity, and 0 twins were dropped in-run.
By hand there are **2 duplicate pairs**: rows 1/14 (both "the worktree guard
refuses compound bash", different regexes) and rows 19/20 (both from one
moment about stale stats in memory files, one an `edit` matcher and one a
`bash` matcher). Jaccard over word sets does not see either.

The server-side twin check (§5.1) is statement similarity too, so it will miss
the same pairs across teammates. Worth knowing before it is the only defence.

### 6. Corpus shape limits what this says

Nineteen of 31 rows come from one engineer (`eng-aa1d15`), and one engineer in
the corpus produced none. The corpus also skews toward spec and design sessions
— `project_state` is the single largest refusal reason at **99 of 198**, which
is what the author correctly says about a session spent arguing about a design
document. A corpus of debugging sessions would likely draft more and refuse
less. The ≥2-engineer bar is met; a *balanced* corpus is not.

## What S0 says about S1

**The pipeline is good enough to be worth continuing, and not yet good enough
to turn on.** 21 of 31 drafted rows are rows a reviewer would plausibly
activate — better than the prototype's 1-of-5 on a single session, and above
the 0.5 bar — at ~8 drafts per working session's worth of turns and no
same-session firing. The refusal machinery does most of the work: 198 of 229
author calls end in a refusal with a reason, which is the pipeline behaving as
designed.

Before S1 is flagged on, in this order:

1. **Redact the window** (`redact.py`) before the author sees it. Finding 1 is a
   privacy bug, and it is cheap to fix.
2. **Re-measure recall on the direct-API path.** Finding 3 says the gate miss is
   transport, not judgement; that has to be confirmed, not assumed.
3. **Decide the router's job.** Finding 2: either drop the non-claim regexes or
   demote them to a judge hint. Do not tune them on this corpus.
4. **Get the second judge.** One pass by the agent that wrote the pipeline is
   the weakest review this gate could have.

Items 1-3 are mechanism fixes with known causes. None of them is "the model
needs to be better", which is the outcome that would have argued for stopping.

## Reproducing this

```bash
PY=~/xtrace/MemHub-Backend/.venv/bin/python   # any python with psycopg2
S=plugins/memhub/skills/rules-from-sessions/scripts

$PY $S/staging_sessions.py --since 2026-07-01 --min-turns 8 \
    corpus --out corpus --sessions 24 --min-corrections 2
python3 $S/score_s0.py router --corpus corpus                 # free, no model
python3 $S/score_s0.py gold   --jobs 10 --out gold.json
python3 $S/score_s0.py corpus --corpus corpus --out s0-run --jobs 14
```

`s0-run/judge_sheet.md` is the hand-judgement sheet the rubric above was
applied to. The corpus and the drafts are **not** committed: they are other
engineers' session content, and Finding 1 is why.
