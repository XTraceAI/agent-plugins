<!-- create-rule step 4b, the live forward test. Read from SKILL.md; step numbers refer to it. -->

### 4b. Prove it fires in a LIVE session — mandatory

`rulebook_verify.py` proves the pattern matches the strings you thought of. It
cannot prove the rule fires in a real session, at a moment that helps — and the
most common way a pattern is wrong in practice is a false negative nobody
writes a case for. So before filing, run the rule against a real agent doing
real work.

**The rule is not filed until this step has produced a fire.** There is no
"skip the live test": if the user asks to skip it, tell them what it would have
proven and run it. The one exception is §4b.6 — an environment that cannot run
it at all.

**4b.1 Write the fake feature prompt.** Compose a short, realistic task that
should trip the rule with near-certainty and is doable in a couple of tool
calls — a demo, not a project. Show it to the user before running it. It must
never touch anything outside the scratch worktree, and it must **not** be
phrased as "trigger the rule": a prompt that names the rule tests the
sub-agent's obedience, not the rule's pattern.

**A destructive trigger gets a scratch target.** An advise candidate does not
stop the call, so a fake feature that "force-pushes staging" really would.
Point the action at something the worktree owns: for the force-push example,
the sub-agent runs `git init -q --bare .scratch.git && git remote add scratch
.scratch.git`, pushes once, then is asked to "squash the last two commits and
update `staging` on the `scratch` remote" — which fires on `git push --force
scratch HEAD:staging` with nothing shared at risk. If no scratch target can
stand in, treat the run as §4b.6.

**4b.2 The scratch worktree is the sub-agent's own — `isolation: "worktree"`.**

Do not `git worktree add` a scratch checkout yourself and tell the sub-agent
to "work inside" it. The claim in §4b.3 is keyed on the **cwd the hook
payload carries** (`set_active_base(cwd)` in `rulebook_hook.py`), and an
Agent-tool sub-agent inherits the SESSION's cwd — the directory your terminal
is in — no matter which paths its commands name. A sub-agent editing files in
a worktree you made by hand runs every call from your cwd, reads the shared
book, and the private ledger ends the test with **zero rows for every rule**,
which reads exactly like "the candidate did not fire".

Spawn the sub-agent with the Agent tool's `isolation: "worktree"` instead. It
gets its own checkout at

```
<repo>/.claude/worktrees/agent-<agent id>     on branch  worktree-agent-<agent id>
```

and that path IS its cwd, so a claim over the parent directory
`<repo>/.claude/worktrees` reaches it. Real layout, real remote, real repo
identity — repo- and path-scoped rules match without any faking, and the path
is inside the sub-agent's sandbox write allowlist (a `mktemp -d` worktree
created outside the sandbox is not: the write fails `EPERM` and the sub-agent
cannot legitimately get past that). **Never run the sub-agent in the user's own
working tree.**

**The isolated worktree is cut from the main checkout's HEAD, which may be
stale or carry local commits.** The fake feature's anchors (the constant, the
file, the path the rule names) may not exist there. Make the sub-agent's first
call bring its own scratch branch to the shipping tree — `git fetch -q origin
&& git reset -q --hard origin/<base>` — and print the anchor. That reset is
confined to the sub-agent's branch; it never touches the user's checkout.

**It is on a branch, never detached.** A detached worktree makes the hook report
the branch as `detached`, so **every `given.repo.branch_rx` / `branch_not_rx`
predicate silently fails** — including the documented "never push to main"
rule. Step 4b is mandatory, so that reads as "the rule never fired" and blocks
filing a perfectly good rule. Verified: detached → `given_ok` returns False for
`branch_rx: "^(main|master)$"`; on a named branch it evaluates normally.

**When the rule asks about the branch, name the branch to match.** Read the
candidate's `given.repo.branch_rx`; the isolated branch is `worktree-agent-…`,
so have the sub-agent's first call also `git checkout -b <matching-name>`
inside its own worktree. If the pattern demands a name that is already checked
out — `^(main|master)$` is the common case, and git refuses a second worktree
on it — you cannot exercise that predicate here: say so, report the branch
predicate as **unexercised**, and treat the run as §4b.6 (ask before filing)
rather than reporting a failed rule. Never delete the `given` block to make
the test pass: a fire the rule would not produce in production is a worse
answer than no fire.

**4b.3 Arm the candidate in a book that is YOURS.** The candidate is not filed
yet and a proposed rule never fires, so the test has to arm it in a book the
hook really reads. It does **not** arm it in the shared one. That file is a
single book per repo, read by every session on this machine working in that
repo, on every `PreToolUse` — so doctoring it hands an unfiled, unreviewed rule
to your colleagues' sessions and to your own other terminals, and two tests
that interleave leave one armed with no backup left to find it by.

Instead, claim a private base for the calls made from the sub-agent's isolated
worktree. Its exact path is not known until the Agent tool creates it, so the
claim covers the parent directory every isolated worktree of this repo lands
in — write it **before** spawning the sub-agent:

```bash
BASE="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_paths.py")"   # this install's, keyed by backend
PRIV="$(mktemp -d)/pretest-base"
mkdir -p "$PRIV/book" "$PRIV/ledger" "$PRIV/state"
cp -R "$BASE/book/." "$PRIV/book/"          # start from the real rules
PREFIX="<repo root>/.claude/worktrees"       # where isolation: "worktree" puts the sub-agent
ls "$PREFIX" 2>/dev/null                     # any sibling isolated worktree live right now?

# the claim: calls whose cwd is under $PREFIX read $PRIV, nothing else does
python3 - "$BASE" "$PRIV" "$PREFIX" <<'EOF'
import json, os, sys
base, priv, wt = sys.argv[1:4]
p = os.path.join(base, "pretest-redirect.json")
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"base": priv, "cwd_prefix": wt, "pid": os.getpid()}, f)
EOF

# where the sub-agent's calls will actually read from — check before arming
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path "<repo>" "$PREFIX/probe"
# and where YOUR calls still read from — must stay the shared book
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path "<repo>" "<repo root>"
```

The first `book-path` must print a path under `$PRIV`; the second must print
the shared book. If the first prints the shared book, the claim did not take
(a stale or unreadable redirect reads as *no redirect*, by design) — **stop,
and do not arm anything**. Ask the hook rather than recomputing the hash, here
as before.

**What the parent-directory claim reaches.** For the minutes it is armed,
another session's isolated sub-agent in the same repo would read the private
book too. That book is the real rules plus one advise-or-gate candidate, so
nothing is taken from anyone, and its fires would land in your private ledger
where the id filter below excludes them — but if `ls "$PREFIX"` showed a live
sibling, say so in the report. `<repo root>/.claude/worktrees` is the Agent
tool's location; if a spawned sub-agent reports a different path, re-claim
with that parent and re-run rather than trusting a test the claim never saw.

Then write the candidate into the book **under `$PRIV`**, appended to `rules` —
never replacing the list, so the real rules stay armed and the sub-agent runs
under the same posture a real session has. Normalise the appended copy:

- `id`: `candidate-<8 hex>` — unique, and identifiable if a row ever leaks to
  the ledger;
- `_label`: the candidate's title, so disclosure renders as it will in
  production;
- `status`: `active`;
- **`mode`: the mode the rule will ship with.** A gate candidate stays a gate:
  what the rule does to a real call, made by a real agent, is the one thing a
  live test shows that `rulebook_verify`'s table cannot, and the claim above
  keeps the gate away from the person's own commands.

  The hook confines this rather than weakening it: a `candidate-` row read
  from the shared base is **dropped**, so the gate reaches the sub-agent under
  your claim and reaches nobody at all outside it. Report what the sub-agent
  actually hit — a refusal is a result, not a failure.

**The background re-fetch cannot touch the private book.** `maybe_refresh`
runs on every PreToolUse and spawns a refresh once the book is a minute old
(`REFRESH_AFTER_S`). Under your claim it reads the private book's
`fetched_at` and stamps `<private book>.refresh`, but the detached child it
spawns fetches with no cwd and so refreshes the **shared** book — never the
private one. The candidate cannot be deleted mid-test by a re-fetch. Still
write `fetched_at` as **now**: it keeps the sub-agent's first minute from
spawning a pointless fetch of the shared book.

**No cached book for this repo** (nothing fetched yet, or no rulebook binds the
user here) → `cp -R` copies nothing and you simply write the private book
yourself, containing exactly the candidate plus a `fetched_at` of now. Nothing
is displaced, because nothing was there and nothing shared is touched either
way — this is not the §4b.6 escape.

**4b.4 Run the sub-agent.** Run it with the Agent tool, `isolation:
"worktree"`, on the fake feature prompt. The prompt tells it: its current
working directory is its own isolated worktree and it stays there (relative
paths, no `cd` out, no commit, no push, sandbox on); its first call is the
`git fetch -q origin && git reset -q --hard origin/<base>` from §4b.2 plus a
print of the anchor; then the feature; then a `git diff --stat`; and it
reports the exact commands it ran and any hook text it saw, verbatim. The
Agent tool's completion notice names the worktree path and branch it created —
keep both for cleanup, and check the path is under `$PREFIX`.

The sub-agent's fires land in `$PRIV/ledger/fires.jsonl`. Only calls from a
worktree under `$PREFIX` write there — never your own setup commands — so the
id check below is all it takes to keep a sibling sub-agent's rows out.

**4b.5 Release the claim — always, immediately after the sub-agent returns**,
success or failure. Keep the evidence first — the evaluation below reads it:

```bash
EVID=$(mktemp -d)                        # the evidence outlives the private base
cp "$PRIV/ledger/fires.jsonl" "$EVID/fires.jsonl" 2>/dev/null || : > "$EVID/fires.jsonl"
cp "$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path "<repo>" "$PREFIX/probe")" \
   "$EVID/book.json"                    # is the candidate still there?
rm -f "$BASE/pretest-redirect.json"     # release first: the claim is what steers
rm -rf "$PRIV"                          # then the private base — a literal path, never a $(…)
```

Release the claim **before** deleting the base, in that order: while the
redirect still points at a base that no longer exists it reads as *no
redirect*, which is the safe direction — a call falls back to the real book
rather than to a missing one.

There is nothing to restore: the shared book was never written.

**An interrupted run now heals itself**, which it did not before. A Ctrl-C
leaves a redirect file and a temp base behind; the redirect stops steering
anything after an hour (`REDIRECT_MAX_AGE_S`), and until then it only ever
affects calls made from an isolated worktree under the claimed prefix — the
abandoned sub-agent's, or a sibling's, which then reads the real rules plus
one candidate for at most that hour. No unfiled rule is left armed for the
user's own terminal or for any session outside that prefix, at any point — so
there is no pre-run recovery scan to do and nothing to report.

If you *do* find a `pretest-redirect.json` from an earlier run while setting
up, just overwrite it: one claim at a time, and the newest one owns the file.

**Evaluate: the ledger first (fact), the transcript second (judgment).**

Read `$EVID/fires.jsonl` (the private ledger, kept in §4b.5) — all of it — and keep the rows with
`rule_id == "candidate-<hex>"` **that also belong to the sub-agent**: the
row's `session_id` must be this session's, and its `agent_id`
must be the sub-agent's rather than the parent's.

**Why the id check, even now that the ledger is private.** The claim keeps
your own terminal and every session outside the prefix out, so the old hazard
— a teammate's terminal, or your own second window, loading the candidate from
the shared book and firing it inside your measurement window — cannot happen
any more. What the id check still catches: a sibling session's isolated
sub-agent under the same prefix during your window, and a row the parent
somehow caused. Filtering on `rule_id` alone would report a pass the fake
feature never earned. A candidate row carrying another session's ids, or the
parent's rather than the sub-agent's, was not caused by the sub-agent
and proves nothing. Report per row: `hook_phase` (pre/post/prompt),
`tool`, `mode`, `fired_at`, `excerpt`.

| Ledger result | Meaning | What you do |
|---|---|---|
| ≥1 candidate row | fired | continue to the transcript check |
| 0 rows for EVERY rule, the real ones included | the claim never reached the sub-agent — its calls read the shared book | not a verdict on the pattern. Check the worktree path in the Agent tool's completion notice against `$PREFIX`; re-claim with the right parent and re-run once |
| 0 candidate rows, candidate still in `$EVID/book.json` | did not fire | **do not file.** Report it as a real failure: the pattern passes the verifier's synthetic cases but not a real session. Offer to revise the pattern and re-run. First rule out §4b.2's branch trap — a `given.repo.branch_rx` that the scratch worktree's branch cannot satisfy fails the same way a bad pattern does |
| 0 rows, candidate **gone** from `$EVID/book.json` | inconclusive — something rewrote the private book mid-test | re-run once; if it recurs, report the environment problem and do not file |

Then read the sub-agent's returned output (an Agent-tool sub-agent's turns are
sidechain records of THIS session, not a separate session file, so `capture.py`
cannot be used here) and judge:

- did the fire land where it could actually change what the agent did, or after
  the fact;
- did the sub-agent disclose it as `📏 Rule fired: …` — this is also the
  end-to-end check on fire disclosure;
- did the fire change the behaviour, or did the agent acknowledge and proceed.

State these as **observations with the evidence quoted**, and be explicit that
they are a judgment where the ledger result is a fact.

**Report and clean up.** Name: the fake feature used, the worktree path (now
removed), the ledger rows, the transcript judgment, and — always — the caveat
*this proves the rule fires, not that it is worth firing*. Add *the branch
predicate was not exercised* when it applies (§4b.2).

Cleanup is mandatory and happens even on failure: release the claim (§4b.5, if
not already), `git worktree remove --force <repo root>/.claude/worktrees/agent-<id>`
and `git branch -D worktree-agent-<id>` — the Agent tool auto-cleans an
isolated worktree only when it is unchanged, and a test that fired always
changed it — then `git worktree prune` and `rm -rf` the evidence dir. There is
no book to restore — the shared one was never written.

**One artifact of the test to expect, not to chase:** a candidate that
supersedes an active rule carries that rule's title as its `_label`, so if the
sub-agent answers the fire with `RULEBOOK_OVERRIDE='[<label>] …'`, the hook
reports the label as fitting two rules and records nothing. In production only
one of the two is armed, and the override resolves.

**4b.6 The one thing that is not a failure.** If the step cannot be **run at
all** — the repo is not a git checkout, the Agent tool cannot create an
isolated worktree (a bare repo, no `HEAD`, a filesystem that refuses it), or
the Agent tool or its `isolation: "worktree"` option is unavailable in this
host — then say which precondition was missing in one sentence, say that
the pattern is therefore proven only against the verifier's synthetic cases,
and **ask** whether to file anyway. Nothing is filed without a yes. A
sub-agent that ran and tripped nothing is NOT this case: that is a failing
test, and it blocks. Cleanup still runs.
