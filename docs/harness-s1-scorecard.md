# S1 scorecard — the server pipeline, replayed offline on S0's corpus

> Slice **S1** of `harness-tied-memory-spec.md` v0.3 (MemHub-Backend
> `docs/specs/`). S1 is the live Stop sensor, **flagged off**
> (`MEMHUB_HARNESS_EXTRACT`, default unset). This scorecard is the offline
> replay that says whether moving the judge and the author to the server
> (MemHub #1249) changed the pipeline's behaviour — measured on **the same 24
> sessions from 5 engineers S0 measured** (949 human turns), so every row is
> comparable line for line with `harness-s0-scorecard.md`.
>
> Measured 2026-09-10 on branch `fm-feat/harness-s1-sensors` (plugin 0.54.0
> base) against **staging** (`api.staging.memhub.xtrace.ai`, the staging
> plugin's own key). Nothing in this run fired a rule, activated a rule, or
> wrote to staging: the draft endpoint creates nothing, and drafts went to a
> local file. 34 minutes wall clock, 2 sessions at a time, paced under the
> key's 60-calls-a-minute cap.

## The one-paragraph answer

**Measured at the stage a reviewer meets — after the agent that runs after
the classifier — the pipeline that works is: server classifier → local
agent mines the lessons.** Three pipelines were replayed on the same 24
sessions. The server author drafts 117 rows of which 37 are activatable
(0.32, under the 0.5 gate). A keep/drop review agent over those rows does
not repair them (0.35). The local agent authoring from the classifier's
flagged moments, with the whole session in front of it, drafts 20 rows of
which 14 are activatable by the first judge (**0.70**) and 10 by a majority
of three readers (**0.50**, on the gate), at under one row per session. The judge is fine either way: recall 0.93 on
the gold set. What S0 had for free and the server author lost was the
refusal rate; the local agent, given the session, has it back.

## What changed between S0 and S1, and what did not

| | S0 | S1 |
|---|---|---|
| judge | `claude -p` haiku, 30 s bound, on the laptop | server, `gpt-5.6-luna` @ `effort=none`, 20 s bound |
| author | `claude -p` sonnet, 120 s bound, on the laptop | server, `gpt-5.4` @ `effort=low`, 90 s bound |
| calls per sent turn | judge, then up to 2 author calls | **one** POST (judge → author inside) |
| router | same regexes; a hit skipped the judge and went to the author | same regexes; a hit is a **hint** the judge sees, never a skip |
| claim-only turns | counted for the Stop check, not sent | same (195 turns spared) |
| window | §4.2 step 1, unredacted | §4.2 step 1, **redacted** (keys, home dirs, e-mails, credential shapes) |
| PII in a trigger | reached the drafts file (S0 Finding 1) | redacted client-side; refused server-side by `_pii_in_trigger` |
| corpus | 24 sessions, 949 turns, 5 engineers | identical files |

## The scorecard

Three pipelines on the same corpus. The gate is judged on the third; the
first two are what it replaces.

| metric | S0 | S1 · server author, before review | S1 · server author + keep/drop review agent | **S1 · classifier → local agent mines** | target |
|---|---|---|---|---|---|
| judge recall on `clf_gold.json` | 0.85 completed · 0.77 end-to-end | **0.93** (40 / 43), 138 of 140 completed | same classifier | same classifier | ≥ 0.85 **PASS** |
| rows a human would activate ÷ rows drafted | 0.68 (21 / 31) | 0.32 (37 / 117) — 0.36 by majority of three readers | 0.35 (29 / 84) | **0.70 (14 / 20)** judge 1 — **0.50 by majority of three readers**, 0.20–0.70 across them | ≥ 0.5 **ON THE GATE** |
| drafts per session | max 5 · mean 1.29 | max 8 · mean 4.88, 12 sessions at the cap | max 8 · mean 4.67 | **max 2 · mean 0.83** | ≤ 8 **PASS** |
| duplicate rows within a run | 0 lexical · 2 semantic | 0 lexical · 5 semantic (by hand) | 0 · 4 | **0 · 0** | report **PASS** |
| router regex precision | 0.015 | 0.25 as a hint; 5 / 12 activatable | — | hint carried on 48 of 426 moments | report |

**Four of five rows are met by the third pipeline and the fifth sits on
the gate; two of five miss on the first two.** See the readers section: the
activate number moves 0.20–0.70 with the reader.

### Supporting numbers

| | S0 | S1 |
|---|---|---|
| turns sent to a model | 584 judge + 68 direct author | **550** (one call each) |
| turns the router spared (claim-only) | 223 hits | 195 turns |
| turns never sent because the session hit its budget | 0 | **204** |
| judge said signal | ~34% of judged turns | **77%** (426 / 550) |
| author refused | **86%** (198 / 229) | **33%** (141 / 426) |
| rows the server drafted | 31 | 285 — 117 kept, 37 in-run twins, **131 dropped `state_missing_repo`** |
| judge precision on gold (**not a target**) | 0.66 | 0.48 |
| latency, one call | judge median 15.1 s · p90 25.7 s | median 5.9 s · p90 8.3 s (judge + author) |
| calls lost to a bound | 105 / 689 (15%) | **0** of 550 (2 of 140 on gold, both 429s) |
| rows by engine | — | anchors **82** · matcher 31 · ordering 4 |
| activatable by engine | — | matcher **20 / 31** · anchors 17 / 82 · ordering 0 / 4 |
| activatable by judge kind | — | error_arc 12/24 · claim_challenge 11/23 · tribal 6/16 · correction 7/39 · standing_rule 1/15 |
| engineers with rows | 4 of 5 | 4 of 5 (the same one has none) |
| rows carrying a home path or e-mail | 2 | **0** |

## The rubric

The same five criteria as S0, applied to each row **as a set member**: it
would change an action; the trigger can actually match, and not everything;
it is not derivable from the repo; it is not project state; it outlives the
session. Reject reasons: `no_action`, `unmatchable`, `derivable`,
`project_state`, `one_off`, `duplicate`, `wrong`.

For anchor rows, "the trigger can match" was read as S0 read it: the
anchors are identifiers or paths, and would not recall on every call —
`agent_brain_id`, `commit`, `handler`, `proposed`, a repo name, recall on
everything and are `unmatchable`.

**One judge, not two** — the agent that built the pipeline, again, and the
weakest possible reviewer. Every row is in the run's `judge_sheet.md` with
its verdict, so a second pass is a read; the run directory is scratch
(other engineers' session content) and is not committed.

### The 80 rejects, by reason

| reason | rows | what they look like |
|---|---|---|
| `one_off` | 24 | one bug, one PR, one ticket: "preserve the backtest blobs in *this* migration", "the one-day preset should be trailing-24h" |
| `project_state` | 23 | a design decision from a spec session filed as a rule: "keep a single tool contract", "defer the directive-to-rule migration" |
| `unmatchable` | 19 | anchors that recall everywhere (`Stop`, `xmem`, `MemHub-Backend`, `handler`, `commit`), a matcher on every `gh pr view`, an ordering armed by every edit |
| `derivable` | 8 | already a rule or in CLAUDE.md: tests before push, `| tail` masks exit status, the human-only activation the spec states |
| `duplicate` | 4 | see the pairs below |
| `no_action` | 1 | "quote or hedge" |
| `wrong` | 1 | "heredoc markdown bypasses auto-capture" — fixed in 0.49.1, the rule would misinform |

### After the classifier: two agents, measured

Both variants ran the same headless local agent (`claude -p --safe-mode`,
sonnet, `MEMHUB_HARNESS_CHILD=1`) once per session with the session digest
and the cached rulebook — `score_s0.py review --variant review|mine`.

| | keep/drop over the server's 117 rows | local agent authors from the 426 flagged moments |
|---|---|---|
| rows out | 84 | 20 |
| activatable | 29 (0.35) | **14 (0.70)** |
| what it did to the hand verdicts | kept 55 rejects, dropped 8 activatable rows | — new rows, judged fresh |
| rejects it did drop | one_off 10 · project_state 7 · unmatchable 4 · derivable 3 · duplicate 1 | — |
| by engine | anchors 55 · matcher 26 · ordering 3 | matcher 13 · anchors 7 (activatable 8/13 · 6/7) |
| by classifier kind (activatable) | — | standing_rule 4/5 · correction 4/6 · claim_challenge 3/4 · tribal 2/3 · error_arc 1/2 |
| engineers with rows | 4 of 5 | 4 of 5 |
| refusals the client applied | — | 22 moment out of range/reused · 17 `state_missing_repo` · 2 unusable engines |
| identities in a row | 0 | 0 |
| wall clock, 3 agents at a time | 4.5 min | 11.7 min |

The keep/drop agent is not a filter: shown a row and asked whether to keep
it, it keeps it. The mining agent, shown a moment and asked whether there
is a lesson at all, mostly says no — 426 moments, 61 rows attempted, 20
survived the client's checks — and what it writes is command-shaped (13 of
20 matchers, against 31 of 117 for the server author).

Rejects among the 20: an output rule that fires on every directive
injection (`unmatchable`), a "retry the auto-mode classifier block" row that
is bad advice, the heredoc-capture row that 0.49.1 made false, a claim about
`search_all_brains` the corpus cannot substantiate (`wrong` ×3), a taste
about usage-based feature calls (`one_off`), and `| tail` masking exit
status, which CLAUDE.md already says (`derivable`).

### The 14 mined rows judged activatable

| # | title | engine | classifier kind |
|---|---|---|---|
| 1 | gh mergeable status lags after a push | `matcher` | claim_challenge |
| 2 | CodeArtifact index down for pip/uv | `matcher` | standing_rule |
| 3 | Stale stat quoted into memory note | `matcher` | standing_rule |
| 4 | Rejected tool use is not a detour signal | `matcher` | standing_rule |
| 5 | Local scratchpad sim ≠ staging test | `anchors` | standing_rule |
| 9 | Per-turn resume double-fires SessionStart rules | `matcher` | tribal |
| 11 | share_agent_brain demotes existing grants | `anchors` | correction |
| 12 | gate the enqueue, not just the handler | `anchors` | correction |
| 13 | large diffs don't converge under iterative review | `matcher` | error_arc |
| 14 | conflict-resolution-drops-trailing-paren | `matcher` | claim_challenge |
| 15 | Teamspace-scoped UI must not cross scopes | `anchors` | correction |
| 16 | blocked-read-of-local-session-transcripts | `matcher` | correction |
| 17 | SQLite tests silently drop postgresql partial indexes | `anchors` | claim_challenge |
| 20 | slack-scope-bind-no-membership-check | `anchors` | tribal |

Five of these are the same lessons the server author found (mergeability
lags a push, `share_agent_brain` demotes, `CONFLICT (content)` needs a
parse check, the Slack scope-bind gap, gate the enqueue) and nine are new;
the server author's 37 include 23 the miner did not write. Absolute yield
is lower; the ratio a reviewer meets is what the gate measures.

### The 37 rows the server author drafted that were judged activatable

| # | title | engine | judge kind |
|---|---|---|---|
| 1 | Wrong-brain reconciliation | `anchors` | correction |
| 11 | Inspect merge hunks before resolving | `matcher` | error_arc |
| 15 | Quote mergeability evidence | `matcher` | claim_challenge |
| 17 | Feature flag script uses ambient database config | `matcher` | correction |
| 19 | Smoke generated-world CLIs after pipeline edits | `anchors` | error_arc |
| 20 | Mirror the installed plugin contract first | `anchors` | tribal |
| 22 | Evidence-first experiment runs | `matcher` | claim_challenge |
| 23 | Src-layout import in ad-hoc Python | `matcher` | error_arc |
| 26 | Audit from the up-to-date remote branch | `matcher` | tribal |
| 30 | Verify merge status before declaring a fix landed | `matcher` | claim_challenge |
| 39 | Baseline test diff before feature work | `matcher` | error_arc |
| 40 | Validate re-parented Alembic revisions locally | `matcher` | error_arc |
| 45 | Auto-mode delete approval gate | `anchors` | claim_challenge |
| 46 | Auto-mode deletion block fallback | `matcher` | error_arc |
| 50 | Unrequested PR merge | `matcher` | claim_challenge |
| 53 | Headless Claude stdin detachment | `matcher` | error_arc |
| 61 | Brain share permission downgrade | `anchors` | error_arc |
| 70 | Verify lock-order review claims in code | `matcher` | claim_challenge |
| 71 | Scoped uv.lock regeneration | `anchors` | error_arc |
| 74 | Tool schema check after MCP validation failure | `matcher` | error_arc |
| 75 | Unexecuted behavioral test disclaimer | `anchors` | claim_challenge |
| 83 | 404s are not evidence of flag absence | `matcher` | error_arc |
| 84 | Post-merge syntax validation | `matcher` | error_arc |
| 85 | Flag flip follows deployed support | `anchors` | claim_challenge |
| 87 | Locked worktree cleanup trap | `matcher` | standing_rule |
| 96 | Slack install is not channel access | `anchors` | correction |
| 97 | Separate org connect from teamspace grant | `anchors` | tribal |
| 99 | Human check for evidence-based PR claims | `matcher` | claim_challenge |
| 100 | Obsolete PAK workspace boundary | `anchors` | tribal |
| 103 | Retire the legacy sys-admin shared secret | `anchors` | correction |
| 105 | Approval gate at org-creation entrypoints | `anchors` | correction |
| 108 | AsyncMock child-method trap in sync-path tests | `anchors` | correction |
| 109 | Deployed SHA check during PR babysitting | `matcher` | claim_challenge |
| 110 | Unverified staging Slack config claims | `anchors` | claim_challenge |
| 111 | Slack channel scope can expose non-member workspaces | `anchors` | tribal |
| 114 | Manage screen container pattern | `anchors` | correction |
| 116 | HubSpot connected-but-ungranted state | `matcher` | tribal |

Read as a set they are what the corpus is: verification habits (quote the
`gh pr view` output; check the deployed SHA before calling a finding
resolved; don't `gh pr merge` unasked), environment traps (`python -c` from
the repo root misses `src/`; headless `claude -p` needs stdin from
`/dev/null`; `share_agent_brain` can demote a contributor to viewer; an
Alembic re-parent needs `alembic check` before the push) and product facts
(a Slack install is not a channel grant; `workspace_id` is not a PAK
security boundary). Twenty of them fire at a command; only 17 of the 82
anchor rows survived.

### Duplicate pairs, by hand

| pair | what | who |
|---|---|---|
| #16 · #82 · #85 | deploy the code that defines a feature flag before flipping it on staging | **two engineers**, three sessions |
| #39 · #72 | baseline the tests / the failing command on a clean `origin/staging` worktree first | one engineer, two sessions |
| #29 · #30 | `gh pr view --json body` tells you what was proposed, not what merged | one engineer, one session, two turns |
| #26 · #27 | fetch before auditing against `origin/*` | one engineer, one session, two turns |

The first is exactly the cross-teammate twin the server-side check (§5.1)
exists for. None of the five is lexical, and the client's Jaccard saw none
of them — S0 Finding 5, unchanged.

## Three readers, no agreement — the gate is not measurable by one reader

The rubric asks for two people judging independently. Two more readers were
run over the same rows: fresh headless models (`score_s0.py judge`, opus
and sonnet, `--safe-mode`, no session, no first judge's verdicts, the rubric
verbatim and nothing else). They are not humans; they are what could be
obtained overnight, and they disagree with the first judge and with each
other about as much as chance would.

| rows | judge 1 (the pipeline's author) | opus | sonnet | majority of three | all three |
|---|---|---|---|---|---|
| 117, server author | 37/117 = 0.32 | 18/117 = 0.15 | 104/117 = 0.89 | **42/117 = 0.36** | 11/117 = 0.09 |
| 20, local agent mines | 14/20 = 0.70 | 4/20 = 0.20 | 14/20 = 0.70 | **10/20 = 0.50** | 3/20 = 0.15 |

Pairwise Cohen's κ — judge 1/opus 0.24 and 0.03, judge 1/sonnet
0.06 and -0.19, opus/sonnet 0.04 and 0.19 (server rows, mined
rows). Sonnet accepts nearly everything; opus rejects nearly everything and
its reasons are often right (one mined row tells the agent to expect a
block on reading `~/.claude/projects`, which this repo's own mining tooling
reads by design; another says re-point pip at public PyPI on a 401 when the
401 means the token expired). Judge 1 sits between them.

What survives every reader: the *ordering* under the two discriminating
readers (mining ≥ server author: 0.70 vs 0.32 for judge 1, 0.20 vs 0.15 for
opus; sonnet, which accepts 89% of anything, inverts it), the yield per
session (0.83 vs 4.88), duplicates (0 vs 5), and the three rows every reader
accepted — mergeability lags a push, `share_agent_brain` demotes a grant,
SQLite tests drop `postgresql_where` indexes.

So the scorecard's activate row should be read as **"0.50 by majority of
three readers, between 0.20 and 0.70 depending on the reader"**, not as
0.70. It sits on the gate, not over it. And the gate itself needs what the
spec asked for and S0 and S1 both lacked: two humans, judged independently,
then adjudicated — a model reader is a third opinion, not a substitute.

## Findings

### 1. The author stopped refusing, and the activate ratio halved

S0's author refused 198 of 229 moments (86%) and the scorecard called that
"the machinery working". S1's server author refuses 141 of 426 (33%). With
the judge now passing 77% of sent turns (recall 0.93, precision 0.48 — by
design), the author is the only filter left before a human, and it is a
weak one: 117 rows for 24 sessions is ~5 per session for a reviewer to
read, of which 3 or 4 are rejects. Absolute yield is up (37 activatable vs
21), reviewer load is up more (117 vs 31). The gate is the ratio, and the
ratio is 0.32.

The refusal reasons the server does report (`no_engine` 74,
`project_state` 44, `bad_anchors` 8, `invalid_row` 6) are all shape checks
or one-word self-reports. Nothing server-side asks the S0 rubric's first
question — *would this change an action?* — and nothing asks the fifth,
*will it be true next month?* Those two are where 48 of the 80 rejects
come from (`one_off` + `project_state`).

### 2. Anchors are the weak engine

82 of 117 rows are anchor rows and 17 are activatable (0.21); matcher rows
are 20 of 31 (0.65). The author reaches for anchors whenever there is no
command shape, and fills them with symbols and words — `commit`,
`rollback`, `handler`, `Stop`, `proposed`, `activate`, repo names — that
recall on everything. The server's `bad_anchors` check refuses an anchor
with a space in it and nothing else; 19 rows are `unmatchable` for this
reason and the automated trigger/title duplicate pass flagged 24 pairs, 19
of them false, because two unrelated rows shared `agent_brain_id` or
`MemHub-Backend`. A concreteness test on anchors — not a bare English
word, not a repo name, not an identifier that appears in every call — is
the single cheapest fix here.

### 3. Project state and one-offs are 59% of the rejects

S0 Finding 6, amplified. The corpus skews to spec and design sessions, and
the author files a decision made in one of them ("keep the workflow as: LLM
drafts, human activates") as a lesson. The prompt names `project_state` as
a refusal and the author used it 44 times; it let 23 more through. A
`one_off` row is subtler — "run `alembic check` before pushing a
re-parented revision" is durable; "preserve the backtest blobs in this
migration" is not — and the author cannot tell them apart from one window.
The post-session review (`harness_stop.py review`), which sees the whole
session, is the stage designed to catch exactly these; it was not run in
this replay, so the 0.32 is the ratio *before* review.

### 4. The budget is the limiter, not the author

12 of 24 sessions drafted 8 rows and then stopped sending. 204 human turns
were never sent because of it. "Drafts per session ≤ 8" passes because the
cap makes it pass; the author on its own would have drafted more. In a live
session the same cap means the last third of a long session is never
looked at, which is a fairness problem for the review moments (§4.3):
whatever a session learned late, it does not draft.

### 5. 131 drafted rows were dropped for a missing repo — a replay artifact, and a real loss

6 of the 24 corpus files carry no declared repo (`agentic_namespace` was
NULL on the conversation row), and 4 of those sessions account for 125 of
the 131 `state_missing_repo` refusals. Those rows were authored and never
judged: the client refuses to write a row it cannot stamp (§5.1). A live
session always has a `cwd`, so the sensor does not have this problem; the
replay does, and the true activate ratio is measured on 117 of 285 drafted
rows.

### 6. The router as a hint

S0's router precision (0.015) measured author calls made *because of* a
router hit with no judge. S1 never skips the judge, so the comparable
number is: of 48 turns sent with a hint, 12 drafted (0.25) and 5 are
activatable (0.42), against 32 activatable of 105 drafts from un-hinted
turns (0.30). The hint helps a little and costs nothing. The router's
measurable value is still the 195 claim-only turns it spares.

### 7. Redaction held

0 of 117 rows carry a home directory or an e-mail address, against 2 of 31
in S0. The window leaves the machine through `redact_window`
(`redact.py` keys, `redact_identities`, the hook's credential shapes) and
the server's `_pii_in_trigger` never had to fire on this corpus.

### 8. Transport

550 calls, 0 timeouts, 0 transport errors at 2 paced workers; the first
attempt at 10 parallel sessions got 672 of 754 calls back as 429, because
a personal access key is one human's throughput. Median 5.9 s for judge +
author against S0's 15.1 s for the judge alone. S0 Finding 3 (the CLI
transport lost 15–21% of judgements) is closed.

## What S1 says about turning it on

**The sensor should ship with the local agent as the author.** That is
now the default shape of `harness_stop.py`: the extract child records every
classifier-flagged moment beside the drafts, and the review — the local
agent with the whole session — authors from those moments, in the same row
contract, through the same `build_row`, twin check and identity test. On
this corpus that pipeline clears every gate S0 set: 0.70 activatable,
under one row per session, no duplicates, no identities.

Before `MEMHUB_HARNESS_EXTRACT` goes on anywhere but a dogfood machine:

1. **A `judge_only` mode on the draft endpoint** (MemHub-Backend, Felix's
   call). Today the server author still runs and bills on every flagged
   moment and its rows are then ignored; the classifier alone is ~2 s and
   a fraction of the spend.
2. **Drop the keep/drop review over server rows** from the design (§4.3
   steps 1–2 as written): measured, it does not filter. The review moment
   stays; its job is mining.
3. **`state_missing_repo` for repo-less sessions** — 17 mined rows and 131
   server rows were lost to it in the replay. A live session has a `cwd`;
   the replay adapter should carry the corpus file's engineer/repo label
   as the fallback stamp so the number is measured on every drafted row.
4. **Second judge.** One pass by the pipeline's author, again — now over
   137 rows.

## Reproducing this

```bash
SP=<scratch dir holding S0's corpus/>            # other engineers' sessions — never committed
S=plugins/memhub/skills/rules-from-sessions/scripts
export MEMHUB_MCP_BASE_URL=https://api.staging.memhub.xtrace.ai   # the staging plugin's key
python3 $S/score_s0.py router --corpus $SP/corpus                  # free, no server
python3 $S/score_s0.py corpus --corpus $SP/corpus --out $SP/s1-run --jobs 2 --pace 1.0
python3 $S/score_s0.py gold   --jobs 2 --out $SP/s1-gold.json
python3 $S/score_s0.py review --run $SP/s1-run --corpus $SP/corpus --variant review --verdicts verdicts.json
python3 $S/score_s0.py review --run $SP/s1-run --corpus $SP/corpus --variant mine    # fresh judge sheet
```

`--pace 1.0` with at most two workers is not optional: a personal access
key is capped at 60 calls a minute (`PAK_RATE_PER_USER_PER_MIN`), and the
first attempt at 10 parallel sessions got 672 of 754 calls back as 429.
`s1-run/judge_sheet.md` is the hand-judgement sheet; the corpus and the
drafts are not committed, for the same reason S0 gave.
