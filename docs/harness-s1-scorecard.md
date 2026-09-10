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

**The pipeline got worse on the gate that matters, and it must not be
turned on as it stands.** It drafts almost four times as many rows as S0
(117 vs 31) and more of them are activatable in absolute terms (37 vs 21),
but the ratio a reviewer sees fell from **0.68 to 0.32**, under the 0.5
gate. The cause is not the judge — its recall on the gold set is 0.93, as
the server team measured — it is that the **author stopped refusing**:
S0's author refused 86% of the moments it saw and S1's refuses 33%, so the
refusal machinery that carried S0 is not carrying S1. Half the sessions hit
the 8-draft budget, which is why drafts-per-session still "passes".

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

| metric | S0 | S1 | target | verdict |
|---|---|---|---|---|
| judge recall on `clf_gold.json` | 0.85 completed · 0.77 end-to-end | **0.93** (40 / 43) among 138 of 140 completed · ≥ 0.89 end-to-end | ≥ 0.85 | **PASS** |
| rows a human would activate ÷ rows drafted | 0.68 (21 / 31) | **0.32** (37 / 117) | ≥ 0.5 | **MISS** |
| drafts per session | max 5 · mean 1.29 | max **8** · mean **4.88** — 12 of 24 sessions at the cap | ≤ 8 | PASS on the number; **the cap is doing the work** |
| duplicate rows within a run | 0 lexical · 2 semantic (by hand) | **0** lexical · **5** semantic (by hand) · 24 flagged by trigger/title, mostly false | report both | **MISS** — worse than S0 |
| router regex precision | 0.015 (1 row / 68 author calls) | **0.25** (12 rows / 48 hinted calls); activatable 5 / 12 | report | reported — a different quantity now, see Finding 6 |

**Two of five gates miss, the same two S0 missed, and the one that
matters most moved the wrong way.**

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

### The 37 rows judged activatable

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

**Not yet.** The sensor is built, flagged off, and tested; the pipeline it
would feed drafts too much. Before `MEMHUB_HARNESS_EXTRACT` goes on
anywhere but a dogfood machine, in this order:

1. **Give the author back its refusals** (server, MemHub #1249's prompt):
   the first and fifth rubric criteria as explicit refusal tests — "name
   the next command this changes" and "true next month" — and a
   concreteness test on anchors (Finding 2). Re-measure on the same corpus.
2. **Run the review over this run's drafts** and report the ratio *after*
   review, which is the ratio a human actually sees (Finding 3).
3. **Decide the budget's meaning** (Finding 4): a per-session cap of 8
   with a session sending its first 8 signals is not the same as a review
   choosing the best 8.
4. **Second judge.** One pass by the pipeline's author, again.

None of these is "the model is not good enough". Recall is 0.93 and the
activatable rows are real lessons; the machinery that is missing is the
refusal machinery S0 had for free.

## Reproducing this

```bash
SP=<scratch dir holding S0's corpus/>            # other engineers' sessions — never committed
S=plugins/memhub/skills/rules-from-sessions/scripts
export MEMHUB_MCP_BASE_URL=https://api.staging.memhub.xtrace.ai   # the staging plugin's key
python3 $S/score_s0.py router --corpus $SP/corpus                  # free, no server
python3 $S/score_s0.py corpus --corpus $SP/corpus --out $SP/s1-run --jobs 2 --pace 1.0
python3 $S/score_s0.py gold   --jobs 2 --out $SP/s1-gold.json
```

`--pace 1.0` with at most two workers is not optional: a personal access
key is capped at 60 calls a minute (`PAK_RATE_PER_USER_PER_MIN`), and the
first attempt at 10 parallel sessions got 672 of 754 calls back as 429.
`s1-run/judge_sheet.md` is the hand-judgement sheet; the corpus and the
drafts are not committed, for the same reason S0 gave.
