# Plugin Spec: rulebook disclosure, session framing, and a live forward-test

**Repo:** `XTraceAI/agent-plugins`. **Subsystem:** the Rulebook — `plugins/memhub/scripts/rulebook_hook.py`
and `plugins/memhub/skills/create-rule/SKILL.md`. Independent of the PR-linking specs in this
directory; they share only a release train.

**Status:** implemented in v0.50.0. Sole source of truth for these four changes.

**Host scope: Claude Code only.** The rulebook hook is not wired into the Codex bridge
(`codex_hook_bridge.py` dispatches directive recall, artifact sync and flush — never
`rulebook_hook.py`) nor into Cursor. That matters for §3: `systemMessage` is a Claude Code hook
field, and it is the channel that makes disclosure deterministic. Extending the rulebook to the
other hosts is out of scope here; when it happens, §3.4 is the part that needs rethinking.

---

## 0. Overview

Four changes, in the order a user meets them:

| # | Change | Where |
|---|---|---|
| A | Session start says what rules **are**, and that they carry CLAUDE.md's standing | `rulebook_hook.session_digest` |
| B | Every fire is disclosed to the user in a fixed shape, on two channels | `rulebook_hook`'s fire rendering + `emit` |
| C | A new rule is proven **live** by a sub-agent before it is filed | `create-rule` step 4b |
| D | Rule patterns must survive chained commands (`cd .. && gh pr create`) | `create-rule` step 3/4 guidance |

**The problem A and B solve.** A fire today reaches the agent as a bullet in `additionalContext`
and the user as one terminal line (`XTrace ▸ [label] …`). Nothing requires the agent to *say* a
rule changed what it did, so a fire that silently steered the work is indistinguishable from no
fire at all — to the user watching, to the transcript, and to everything downstream that reads the
transcript. A rule the team can't see working is a rule the team can't trust or improve.

**The problem C and D solve.** `rulebook_verify.py` proves a pattern matches the strings the author
thought of. It cannot prove the rule fires in a real session, at a moment that helps — and the
most common way a pattern is wrong in practice is a false *negative* that no author writes a case
for: an anchored pattern that misses `cd sub && <trigger>`.

---

## 1. What exists today (read before editing)

- **`session_digest`** (`rulebook_hook.py` ~L2325) builds the SessionStart block: an
  `## {BRAND} Rulebook (team rules — advisory)` heading, one bullet per `on="session"` posture
  rule, then `"N rules armed for this repo — they fire inline as you work (proactive on tool
  calls, reactive on errors). Treat a fire as a teammate's note, not boilerplate."`, then the
  books roster. Posture rules are budgeted (`MAX_POSTURE`, `POSTURE_BUDGET_CHARS`) and logged as
  fires with `hook_phase="session"`.
- **Fire rendering** (~L2681) builds two parallel lists per emitting call: `lines` (markdown
  bullets → `additionalContext`, for the agent) and `user_lines` (→ `systemMessage`, the one field
  the terminal shows the user). Three shapes exist:
  `{BRAND} ▸ [label] text` (advisory), `{BRAND} ⚠ gate overridden — [label] why`,
  `{BRAND} ⛔ blocked by [label] text` (gate).
- **`emit(event_name, text, *, user_line=None, deny=None)`** (~L2132) is the single stdout writer:
  `additionalContext` for the agent, `systemMessage` for the user, `permissionDecision:"deny"` for
  a gate.
- **`_label`** is the rule's server-side `title`, cleaned (`~L1180`); rules also carry `text` (the
  statement) and `why`.
- **`log_fires`** (~L2223) appends one row per fire to `<base>/ledger/fires.jsonl` with `fire_id`,
  `rule_id`, `session_id`, `repo`, `branch`, `tool`, `hook_phase`, `mode`, `fired_at` and a
  160-char `excerpt`. This is what §4 evaluates against.
- **`MEMHUB_RULEBOOK_BASE`** relocates the whole rulebook state directory (book cache + ledger);
  **`MEMHUB_RULEBOOK_FETCH=0`** disables fetching. Both are read from the hook process's own
  environment, which is inherited from the Claude Code process — **a skill cannot set them for
  hooks that are already running.** §4.2 is built around that constraint.
- **`maybe_refresh(repo, fetched_at)`** runs on **every PreToolUse call** and spawns a detached
  re-fetch once the cached book is older than `REFRESH_AFTER_S` (1h), overwriting the book file.
  §4.2 must defeat this for the length of the test.
- **`create-rule` step 4** runs `rulebook_verify.py` with `--fires` / `--silent` cases and refuses
  to file while it exits non-zero. Step 5 does the conflict check and files the rule; rules land
  `proposed` unless a rulebook admin passes `activate=True`, and **a proposed rule does not fire.**

---

## 2. Change A — the session-start introduction

`session_digest` gains a fixed preamble, emitted whenever the block is emitted at all (i.e.
whenever any in-scope active rule exists). Exact text:

```
## 📏 Rulebook (team rules — advisory)
These are your team's engineering rules — standing instructions from your teammates, carrying the
same weight as this repo's CLAUDE.md. Follow them as you would CLAUDE.md: they are how this team
works, not suggestions to weigh. When one fires, you MUST disclose it to the user on its own line,
exactly `📏 Rule fired: <the rule, in 20 words or fewer>`, before anything else in that reply.
```

Then the existing content, unchanged: posture bullets, the "N rules armed…" line, the roster.

Notes for the implementer:

- **It is a constant, not a template.** No rule counts, no repo name — nothing that changes
  between sessions, so a reader who has seen it once can skip it.
- **Budget.** ~65 words / ~420 chars, charged to every session that has any rule. It is
  deliberately *not* counted against `POSTURE_BUDGET_CHARS`, which exists to bound rule *content*;
  the framing is what makes that content usable. Do not let it grow past ~500 chars without
  deciding that trade again.
- The heading emoji changes from `{BRAND} Rulebook` to `📏 Rulebook`, matching the marker §3 gives
  advisory fires so the section and its fires read as one system. `BRAND` stays in use elsewhere.
- Posture rules are printed here and are **not** given `📏 Rule fired:` lines (§3.5).

---

## 3. Change B — disclosure of every fire

### 3.1 The two channels, and why both

- **`systemMessage`** — the hook's own line, rendered by the terminal. Deterministic: it appears
  whatever the model does. This is the source of truth for "invariably".
- **`additionalContext`** — an instruction to the agent to repeat the same line at the top of its
  reply. This is the copy that lands in the transcript, and therefore the only one that session
  capture, `/memhub:rules-from-sessions`, a handoff, or a PR comment can ever see.

Neither alone is enough: the first is invisible to everything downstream, the second is not
guaranteed. The user sees the line twice when the model complies; that is the accepted cost.

### 3.2 The line

```
📏 Rule fired: <desc>          ← advisory fire, and a gate that was overridden
⛔️ Rule fired: <desc>          ← a gate that BLOCKED the call
```

`⛔️` keeps its existing meaning — *this call was stopped* — and advisory fires take `📏`. Same
sentence either way, so the shape is one recognisable thing; the symbol is what says whether work
was actually halted.

**`<desc>`** is resolved in this order, then normalised:

1. `r["_label"]` — the rule's title, already constrained to 3–8 words at authoring time;
2. else `r["text"]` — the statement;
3. else `r["id"]`.

Normalisation: collapse all whitespace to single spaces, strip markdown emphasis, take the first
20 words, and if that exceeds 120 characters cut at 120 on a word boundary; append `…` when
anything was dropped. One line, never wrapped by us.

### 3.3 The rendered `systemMessage`

The mandated line first, and beneath it — indented by three spaces — **today's line, unchanged**:
the same `{BRAND} ▸ [label] …` / `{BRAND} ⚠ gate overridden — …` / `{BRAND} ⛔ blocked by …`
strings this hook already builds. The disclosure line is new; the detail line is not rewritten,
so nothing a user recognises today is lost and the existing detail assertions in
`rulebook_hook_test.py` keep asserting on the same substrings.

```
📏 Rule fired: Run the suite before pushing
   XTrace ▸ [run-tests] Run the tests before you push — a red main blocks everyone. (in `src/db.py`, written by that command)

⛔️ Rule fired: Never force-push to main
   XTrace ⛔ blocked by [no-force-push] Never force-push a shared branch.

📏 Rule fired: Never force-push to main
   XTrace ⚠ gate overridden — [no-force-push] rebasing my own topic branch
```

The brand lives on the detail line and nowhere else in the stanza: the disclosure line must be
**byte-identical** to what §3.4 tells the agent to echo, and an echoed `XTrace ▸` would put the
plugin's own branding into the user's transcript on every fire.

Several rules firing on one call produce several such stanzas in one `systemMessage`, in the
existing order.

### 3.4 The `additionalContext` instruction

Appended **once per emitting call**, after the rule bullets, listing the exact lines to echo:

> Disclose these to the user. Begin your next reply with the following line(s), verbatim and each
> on its own line, before anything else — including before any tool call narration:
> `📏 Rule fired: Run the suite before pushing`
> This is how the team sees its rules working. Do not paraphrase, do not merge them into a
> sentence, and do not omit one because it did not change what you were going to do — a rule that
> fired and changed nothing is exactly the rule the team needs to hear about.

The lines given here are byte-identical to the `systemMessage` first lines, built from the same
function (`disclosure_line(rule, blocked)`) — a divergence between what the terminal shows and
what the agent is told to say would be worse than either alone.

### 3.5 Lanes

| Lane | Disclosed? |
|---|---|
| `pre` (proactive, PreToolUse) | yes |
| `post` (reactive, on failure output) | yes |
| gate / blocked | yes, with `⛔️` |
| gate overridden | yes, with `📏` (the rule fired; the call was allowed) |
| `anchor_recall` | yes |
| `session` (posture rules at SessionStart) | **no** — they are the session block itself, already printed in full, and prefixing five of them with `📏 Rule fired:` at every session start would turn the disclosure into wallpaper. They stay ledger fires; they are not disclosures. |

---

## 4. Change C — `create-rule` step 4b: prove it fires in a live session

Inserted between today's step 4 (the deterministic verifier) and step 5 (conflict check + file).
**Mandatory: the rule is not filed until step 4b has produced a fire.**

### 4.1 The shape

1. **Write the fake feature prompt.** The skill composes a short, realistic task that should trip
   the rule with near-certainty and is doable in a couple of tool calls — a demo, not a project.
   Show it to the user before running it. It must never touch anything outside the scratch
   worktree, and it must not be phrased as "trigger the rule": a prompt that names the rule tests
   the sub-agent's obedience, not the rule's pattern.
2. **Scaffold a scratch worktree** (§4.3).
3. **Arm the candidate** (§4.2).
4. **Run the sub-agent** with the Agent tool, instructing it to work only inside the scratch
   worktree path.
5. **Restore the book**, always, immediately after the sub-agent returns — success or failure
   (§4.2).
6. **Evaluate** from the ledger, then the sub-agent's own output (§4.4).
7. **Report and clean up** (§4.5).

### 4.2 Arming the candidate — and the three things that can go wrong

The candidate is not filed yet, and a proposed rule never fires, so the test arms it by editing the
**local book cache** for the duration:

```
book = <MEMHUB_RULEBOOK_BASE or ~/.config/memhub-plugin/rulebook>/books/<repo>-<hash>.json
```

Resolve that path by running the hook's own helper rather than recomputing the hash:
`python3 rulebook_hook.py book-path <repo>` — **add that tiny subcommand** as part of this change;
a second implementation of `book_path` in a skill would drift from the one the hook reads.

1. Copy `book` to `book.pretest-<pid>` verbatim. This is the restore source.
2. Write `book` back with the candidate **appended** to `rules` — never replacing the list. The
   team's real rules stay armed throughout, so the test window cannot leave the user unprotected.
3. The appended copy is normalised for the test:
   - `id`: `candidate-<uuid4-hex8>` — unique, and identifiable if a row ever leaks to the ledger;
   - `_label`: the candidate's title, so disclosure renders as it will in production;
   - `status`: `active`;
   - **`mode`: `advise`, always — even for a rule that will ship as a gate.** A gate armed in the
     user's live session can block the *user's own* next command, not just the sub-agent's. Gate
     behaviour is what `rulebook_verify`'s deterministic table already proves; what this test adds
     is that the pattern fires in a real session, and advise proves that just as well. Say this in
     the report rather than letting the author believe blocking was exercised.
4. **Defeat the background re-fetch.** `maybe_refresh` runs on every PreToolUse and will overwrite
   the book once the cache is an hour old — silently deleting the candidate mid-test, which reads
   exactly like "the rule never fired". The skill cannot set `MEMHUB_RULEBOOK_FETCH=0` for hooks
   already running, so it defeats the check on its own terms:
   - write `fetched_at` in the doctored book as **now**, which makes `_age_s(fetched_at) <
     REFRESH_AFTER_S` true and returns early;
   - also write `{"at": "<now>"}` to `book + ".refresh"` (backing up any existing stamp), which
     blocks a retry for `REFRESH_RETRY_S` even if the age check is ever changed.
5. **Restore**: copy `book.pretest-<pid>` back over `book` (restoring its original `fetched_at`
   and `etag`), restore or delete the `.refresh` stamp, and delete the backup. Verify by hashing:
   if the restored file does not match the backup byte-for-byte, say so loudly and tell the user
   the path — a doctored book is a rule set they did not choose.
6. **If the skill is interrupted between 2 and 5**, the next SessionStart re-fetches the book and
   overwrites the candidate. The damage window is one session, and the stale-stamp trick above
   does not survive a restart. Note this in the report so an interrupted run is not a mystery.
7. **No cached book for this repo** (nothing has been fetched yet, or no rulebook binds the user
   here) → there is no file to copy, so **write one** containing exactly the candidate plus a
   `fetched_at` of now, and record that there was no backup. Restore then means *deleting* the
   file, not copying one back; the next SessionStart fetches the real book as it always would.
   Nothing is displaced, because there was nothing there. Do not treat this as the escape in §4.6
   — the test still proves what it exists to prove.

### 4.3 The scratch worktree

```bash
git -C <repo> worktree add -b rulebook-fwd-<8 hex> "$(mktemp -d)/rulebook-forward-test" HEAD
```

Real layout, real remote, real repo identity — so repo- and path-scoped rules match without any
faking, which a synthetic fixture repo would have to reproduce by hand. The hook resolves a rule's
repo from the acted-on file's worktree, so a rule scoped to this repo fires there exactly as it
would in the user's own checkout.

**On a branch, never `--detach`.** A detached worktree makes `_branch()` report `detached`, so
every `given.repo.branch_rx` / `branch_not_rx` predicate silently fails — including the
`{"repo": {"branch_rx": "^(main|master)$"}}` example this repo's own `create-rule` documents.
Because §4 is mandatory, that reads as "the rule never fired" and blocks filing a good rule.
Verified: detached → `given_ok` returns False for that predicate; `-b <name>` → it evaluates
normally.

Where the candidate carries a branch predicate, name the worktree's branch to satisfy it. Where
the pattern demands a name that is already checked out (`^(main|master)$` — git refuses a second
worktree on it), the predicate cannot be exercised here: report it as **unexercised** and route to
§4.6 rather than reporting a failed rule. Never strip the `given` block to make the test pass — a
fire the rule would not produce in production is a worse answer than no fire.

Remove it in step 7 with `git worktree remove --force`, delete the branch `-b` created
(`git branch -D rulebook-fwd-<hex>`), and `rm -rf` the temp dir. Never run the sub-agent in the
user's own working tree.

### 4.4 Evaluation — the ledger first, the transcript second

**The ledger answers "did it fire".** `<base>/ledger/fires.jsonl` gets one row per fire.

1. Before arming, record the byte offset (or last `fire_id`) of `fires.jsonl`.
2. After the sub-agent returns, read the rows appended since, and keep those with
   `rule_id == "candidate-<hex8>"`.
3. Report per row: `hook_phase` (pre/post), `tool`, `mode`, `fired_at`, `excerpt`.

Outcomes:

| Ledger result | Meaning | What the skill does |
|---|---|---|
| ≥1 candidate row | fired | continue to the transcript check |
| 0 candidate rows, and the candidate is still in the book at restore time | did not fire | **do not file.** Report it as a real failure: the pattern passes the verifier's synthetic cases but not a real session. Offer to revise the pattern and re-run. |
| 0 rows, and the candidate is **gone** from the book | inconclusive — the book was re-fetched mid-test | re-run once; if it recurs, report the environment problem, do not file |

**The transcript answers "as expected".** Read the sub-agent's returned output (and, if needed,
its turns in this session's transcript — an Agent-tool sub-agent's turns are sidechain records of
the *parent* session, not a separate session file, so `capture.py` cannot be used here). Judge:

- did the fire land at a moment where it could actually change what the agent did, or after the
  fact;
- did the sub-agent disclose it in the `📏 Rule fired:` shape §3 requires — this is also the
  end-to-end test of Change B;
- did the fire actually change the behaviour, or did the agent acknowledge and proceed anyway.

State these as observations with the evidence quoted, and be explicit that they are a judgment
where the ledger result is a fact.

### 4.5 Report and cleanup

The step's report, before step 5's confirm-and-file, names: the fake feature used, the worktree
path (now removed), the ledger rows, the transcript judgment, and — always — the two caveats:
*the candidate was armed in advise mode, so blocking was not exercised*, and *this proves the rule
fires, not that it is worth firing*.

Cleanup is mandatory and happens even on failure: restore the book (§4.2.5), remove the worktree,
delete the temp dir, delete the backup files.

### 4.6 The one thing that is not a failure

Step 4b is **mandatory and has no user opt-out**: "the rule did not fire" blocks filing, and a
user asking to skip the test is answered with what the test would have proven, not with a
shortcut. `source_ref` never records a rule as filed-unproven, because there is no such state.

There is exactly one exception, and it is about the *environment*, never the rule: the test cannot
be **run at all**. That means, concretely — the repo is not a git checkout, `git worktree add`
fails (a bare repo, no `HEAD`, a filesystem that refuses it), or the Agent tool is unavailable in
this host. A missing book cache is NOT one of these (§4.2.7 covers it), and neither is a
sub-agent that ran and tripped nothing — that is a failing test, and it blocks.

When the step genuinely cannot run, the skill:

1. says which precondition was missing, in one sentence, and what the run would have proven;
2. states plainly that the pattern is proven only against `rulebook_verify`'s synthetic cases —
   the thing §0 says is not enough;
3. **asks** whether to file anyway. Nothing is filed without a yes, and the yes is the user's, not
   an inference from their earlier answers.

Cleanup still runs: an aborted step 4b must leave no worktree, no temp dir, and no doctored book.

---

## 5. Change D — chained commands in `command_rx`

Guidance only; no verifier change. Added to `create-rule` step 3 (drafting the matcher) and
repeated in step 4's checklist:

> **Never anchor a `command_rx` with `^`.** Real commands arrive chained and prefixed:
> `cd .. && gh pr create`, `cd sub; npm test`, `(cd pkg && git push)`, `env CI=1 pytest`. A
> pattern anchored at the start of the string matches none of them, and the rule then fires for
> some people and not others with nothing to show why — the worst failure a rule has, because it
> looks like the rule working.
> Match at **command position** instead: `(?:^|[;&|(]\s*)` before your trigger, or simply no
> anchor at all plus a `command_not_rx` for the mention-in-argument cases you are protecting
> against.
> Then prove it: add `--fires 'cd .. && <your trigger>'` alongside your plain case. A rule whose
> chained form does not fire is not ready to file.

The existing SILENT guidance (grep / `python -c` / quoted-argument false fires) is unchanged —
this is its mirror image: SILENT guards false positives, this guards the false negative.

---

## 6. Tests

**`tests/rulebook_disclosure_test.py`** (new)
- `disclosure_line`: title present → `📏 Rule fired: <title>`; no title → first 20 words of the
  statement with `…`; neither → the id; a 300-char title clipped at 120 on a word boundary;
  newlines and `**bold**` flattened.
- an advisory fire's `systemMessage` starts with `📏 Rule fired: ` and its second line carries the
  full statement.
- a blocked gate's `systemMessage` starts with `⛔️ Rule fired: ` and the `deny` reason is
  unchanged (the existing blocked-reason test must still pass byte-for-byte).
- an overridden gate renders `📏`, not `⛔️`.
- two rules firing on one call → two stanzas, existing order preserved.
- `additionalContext` ends with the echo instruction, and every line it quotes equals the
  corresponding `systemMessage` first line **exactly** (build both from the same helper; assert
  string equality, not a regex).
- SessionStart output contains the §2 preamble and contains **no** `Rule fired:` line even when
  posture rules are present.

**`tests/rulebook_hook_test.py`** (extend, do not rewrite)
- every existing assertion on `XTrace ▸` / `⛔ blocked by` is **kept**, because §3.3 keeps the
  detail line verbatim; what changes is the handful that assert `systemMessage.startswith("XTrace")`,
  which now assert the disclosure line comes first and `XTrace …` appears on the line beneath.
  If one is deleted rather than updated, the coverage silently shrinks (see
  `registration_test.py`'s own docstring on exactly that failure mode).
- `book-path <repo>` subcommand prints the same path `book_path()` computes, and exits non-zero
  for an empty repo argument.

**`tests/documentation_test.py`** (extend)
- `create-rule/SKILL.md` contains a step 4b, the words "scratch worktree", "advise mode", and the
  never-anchor-with-`^` guidance. The skill is prose, and prose silently loses steps.

Skill behaviour itself (§4) is not unit-testable here; §7's manual verification is its check.

---

## 7. Rollout and manual verification

No backend change, no flag, no manifest beyond the version bump. `plugins/memhub/**` changes, so
all five version manifests move together (`version_parity_test.py`); shipped as **0.50.0**
with the PR-linking work (a patch bump if it ever lands alone).

Verify by hand on the staging build:

1. A session in a repo with rules → the §2 preamble appears once, and the posture bullets are
   unchanged.
2. Trip an advisory rule → `📏 Rule fired: …` in the terminal **and** as the first line of the
   agent's reply.
3. Trip a gate → `⛔️ Rule fired: …`, and the call is still denied with the existing reason text.
4. Override a gate → `📏`, not `⛔️`.
5. Run `/memhub:create-rule` end to end on a throwaway rule: confirm step 4b arms the candidate,
   the sub-agent trips it, the ledger shows exactly one `candidate-…` row, and — the check that
   matters most — **the book file is byte-identical to its backup afterwards**.
6. Kill the skill mid-test (Ctrl-C during the sub-agent run) and confirm the next session start
   restores the real book.

---

## 8. Decisions

- **D1.** Both channels disclose (§3.1). The hook's line is the guarantee; the agent's echo is
  what reaches the transcript.
- **D2.** `⛔️` stays reserved for a call that was actually blocked; advisory fires get `📏`. The
  sentence is identical either way.
- **D3.** SessionStart posture rules are not disclosures (§3.5).
- **D4.** The live forward-test is mandatory before filing (§4), and arms the candidate by
  swapping the local book cache — accepted with the mitigations in §4.2, of which defeating
  `maybe_refresh` by writing a fresh `fetched_at` is the load-bearing one.
- **D5.** The candidate is armed in **advise** mode even when the rule will be a gate, so a test
  cannot block the user's own work. Reported as a stated limitation, not hidden.
- **D6.** Evaluation is ledger-first (fact) then transcript (judgment). Never transcript-only: a
  fire the model failed to disclose is indistinguishable from no fire.
- **D7.** Chained-command coverage is author guidance, not an auto-generated verifier case.
- **D8.** Claude Code only, because the rulebook itself is (see the header).
- **D9.** The disclosure line carries no brand and the detail line beneath it keeps today's
  `{BRAND} ▸ …` shape verbatim (§3.3). The line the agent echoes is the line the terminal shows,
  and it should read as the team's rule rather than as the plugin's advertisement.
- **D10.** Step 4b has no user-facing skip (§4.6). The only way past it is an environment that
  cannot run it, which is reported as unproven and filed only on an explicit yes.
