---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake", "make it actually stop me"). Pins a when-X-then-Y sentence, drafts a deterministic check, and files it for review through the memhub `create_rule` tool — a rule that advises and a rule that stops the command are filed the same way, and a reviewer turns either one on.
argument-hint: [--rulebook "<name or id>"] [the rule, in your own words]
allowed-tools: Bash, Read, Write, Task, Agent, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_rulebooks, mcp__plugin_memhub_memhub__create_rulebook, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rulebooks, mcp__plugin_memhub-staging_memhub__create_rulebook
---

You are creating a **Rulebook rule**: a human-authored, team-owned rule stored
in MemHub, fetched once per session by the coding agent of everyone the rule's
**rulebook** binds, and measured on every fire. Rules are data, not prose in a
doc — and a rule with a loose check fires on innocent commands more often than
not, so the check is where the care goes.

There is no local rule file. The write path is the memhub **`create_rule`**
MCP tool; the rule reaches the rulebook's members once a reviewer activates it
(on a book that binds only its creator, that reviewer is the user themselves).

Arguments: `$ARGUMENTS`
- `--rulebook "<name or id>"` (optional) → the rulebook to write into
  (`rulebook_id` on `create_rule`). Omit and step 0 resolves it.
  `--brain "<name>"` is still accepted and means the same thing — a rulebook
  used to be a brain and people still type it — but say "rulebook" back.
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## 0. Which rulebook — resolve it first

A **rulebook** is a container with its own membership: whoever is a member has
their agent bound by its rules. One person can be in several (an org-wide book
plus their team's), and a rule is filed into exactly one. So the destination is
a decision, not a default — settle it before drafting anything.

Call `list_rulebooks`. Each row has `rulebook_id`, `name`, `scope`
(`all_org` | `explicit`), `member_count`, `rule_count`, `bound` (does it govern
me?) and `is_admin`. Then:

- `--rulebook` given → match it against `rulebook_id` first, then `name`
  (case-insensitive). No match → show the list and ask; never file into a
  book the user did not name.
- Omitted, exactly one book → use it, and say which one in step 6.
- Omitted, several books → **ask** with AskUserQuestion, one option per book
  labelled with its name and who it binds ("org-wide" / "N members"). Do not
  guess: filing into the wrong book binds the wrong people. (This mirrors the
  server's own `TOOL_MANY_RULEBOOKS` — it refuses to guess too.)
- **No books at all** (an empty list, or `TOOL_NO_RULEBOOK` from a write) →
  nothing is auto-provisioned. Offer to create one: propose
  `create_rulebook(name: "<repo> rules", scope: "explicit")` — a book that
  binds only the user, which is the only shape a non-admin may create — and
  ask. On yes, create it and file into it. On no, stop and report; there is
  nowhere to put the rule.

Never call `create_rulebook` with `scope: "all_org"`, and never name another
user in `member_user_ids`. Both are org-admin acts (`rulebook_scope_needs_admin`
/ `rulebook_members_need_admin`), and widening who a rulebook binds is a
governance decision this skill does not make. Membership changes are not MCP
tools at all — they are done in MemHub.

**When the create is refused.** `create_rulebook` validates the creator as an
active org member, so it can answer `rulebook_member_not_in_org` naming *the
user themselves* — even though you named nobody. That is not a bug to retry:
their org membership is inactive, and no rulebook can be created until someone
fixes it in MemHub. Say that plainly and stop. (`rulebook_name_too_long` means
the name exceeded 200 characters — shorten it and retry once.)

**Older backend:** if `list_rulebooks` / `create_rulebook` are not present, or
`create_rule` rejects `rulebook_id`, the server predates rulebook containers.
Fall back to today's behaviour — `agent_brain_id` from `--brain`, omitted
otherwise — and carry on; the rest of this skill is unchanged.

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. A conditional shape is what
makes a rule actionable; a bare observation is not a rule. If the user gave a
war story, extract the conditional from it and confirm your reading.

### 1b. Evidence: how often would it have applied?

A rule is worth the team's attention in proportion to how often the
situation actually occurs. Write the candidate `create_rule` body to a file
and replay it over the local transcripts (Claude Code, Codex, Cursor):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/rules-from-sessions/scripts/mine_sessions.py" \
  --rule-file /tmp/cand.json --out /tmp/mine
```

Read the candidate's line: `applies-in N/M sessions` (by host) and 3 sample
commands. For a "do X before Y" rule add `"requires_prior_rx": "<X>"` to the
body — the line then shows `precision = fired-with-no-prior-X / fired`;
below ~50 % the matcher would nag people who already complied, so use the
`ordering` shape (step 3) or make it `session_context`. Carry the numbers
into `source_ref` in step 5 (`…|applies N/M|precision P`). If N is 0 across
all hosts, say so to the user before filing — it may still be right
(insurance for a new teammate) but it is not lift. For deriving many rules
at once from sessions, use `/memhub:rules-from-sessions` instead.

### 2. Duplicate check — by eye now, deterministically in step 5

Call the memhub `list_rules` tool with **no `rulebook_id`** and
**`include_retired=True, limit=200`** so the reply spans every rulebook the
user can see in every state, and read the new rule against every title and
statement. Same subject → plan to replace the existing rule
instead of adding a twin: note its `rule_id` for `supersedes_rule_id` in step 5.
The server does no title matching — you decide what a rule replaces. Keep the
`list_rules` reply: step 5 runs the deterministic check over it.

`include_retired=True` matters: a rule someone already dismissed is exactly the
twin you must not re-file, and the default view hides retired rules. `limit` is
200 at most — if the reply says `has_more`, ask again with `offset` and
concatenate `rules` before running the check, or the comparison silently misses
whatever fell off the first page.

A twin in **another** rulebook is a different problem: `supersedes_rule_id`
only retires a rule inside one book, so nothing you file can absorb it, and
both rules reach the same call if both books bind the user. Say so and let
them decide — step 5 flags these as `cross_book`.

### 3. Draft the rule — one delivery, one engine block

| the rule is… | `delivery` | engine block |
|---|---|---|
| a Bash command with a checkable form | `agent_hook` | `matcher: {event: "bash", command_rx, command_not_rx?, warn_once_per}` |
| an edit/write to certain paths or content — by the Edit/Write tools OR by a Bash command that wrote the file (heredoc, `write_text()`, `sed -i`) | `agent_hook` | `matcher: {event: "edit", path_rx, path_not_rx?, content_rx?}` |
| a failing or noteworthy tool output | `agent_hook` | `matcher: {event: "output", content_rx, command_rx?, content_not_rx?}` |
| a file about to be read into the agent's context — the Read tool, OR a `cat`/`head`/`tail`/`less`/`more`/`sed` on a path in a Bash command (not piped, not redirected; `cd`-relative paths resolve) | `agent_hook` | `matcher: {event: "read", path_rx?, path_not_rx?, command_not_rx?, given: {file: {lines_gt}, agent: {main}}}` — needs `path_rx` or `given.file`, or it fires on every file |
| "run X after edits, before Y" | `agent_hook` | `ordering: {required_command_rx, gated_command_rx, armed_by_events, min_edits, display_name}` |
| "run X once a session, before Y" — X is not owed to an edit, it is owed to the session (`git fetch` before reading `origin/*`) | `agent_hook` | the same `ordering` block with `armed_by_events: ["session"]`: armed at session start, discharged by one green X, and re-armed for the next session |
| "when the person asks about Z, do X before answering" (probe staging before answering a staging question) | `agent_hook` | the same `ordering` block with `armed_by_events: ["prompt"]` **and** `armed_by_rx` — the pattern the prompt must match. Without `armed_by_rx` the rule arms on nothing; only what a person TYPED arms it, never a slash command's body or a loop wake-up |
| applies when a file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server decides relevance per call |
| worldview with no trigger at all | `session_context` | none — at most 15 such rules per repo scope are shown at session start; prefer a checkable shape when one exists, because advice shown in-flight is acted on far more often than advice shown at session start |

Plus on every rule: `title` (short, imperative; the server allows up to 200
chars but aim for under 60), `statement` (the advisory line and the nuance a
reviewer needs: sanctioned forms, exemptions), `scope_repos` (`["<repo>"]` or
`[]` for all — `<repo>` is the repo's name, `basename $(git remote get-url
origin)` without `.git`, NEVER the directory you are in: in a worktree that
is the branch name, and the hook matches `scope_repos` by exact string, so
the rule would bind nobody), `scope_paths` / `scope_exclude_paths` (globs — they constrain
edit rules by file path; a Bash call carries no path, so an include-scoped
rule never fires on one).

**A rule that needs a newer hook.** If the rule uses a key an older installed
hook would not understand, pass `min_hook_version: "<major.minor.patch>"`.
Where the installed hook is older it runs the rule as ADVICE, never as a gate,
and says so once per session naming the version it wanted — instead of
ignoring the condition and firing as if it held. A key the hook does not know
degrades the same way even without the field, so `min_hook_version` is how you
make the message say what is actually missing.

**Advise, or stop the command?** A rule advises by default: its sentence is
shown and the call goes through. Pass `mode: "gate"` and the rule DENIES a
matching command before it runs — the person can still run that exact command
by prefixing `RULEBOOK_OVERRIDE='<why>'`, and their reason is recorded with the
fire.

Ask for it when the user's own words ask for it — "block", "stop me", "don't
let me", "never let it happen again" — and never on your own initiative. Two
things bound it:

- **Only a call the hook sees BEFORE it runs can be stopped.** A `bash`
  matcher, an `edit` matcher (matched against the content about to be
  written), a `read` matcher (the Read tool's call, or the Bash command that
  would print the file) or an `ordering` can block. `output` fires after the
  command already ran, and notes and anchors are advice by construction —
  the server refuses `gate` on those. A blocked Read has no prefix to carry
  a reason: the deny tells the agent to read narrower (`offset`/`limit`),
  delegate to a subagent, or run `RULEBOOK_OVERRIDE='<why>' cat <path>` in
  Bash, which records the override like any other.
- **It stops every teammate the book binds, not just the author.** Say that
  before filing, in those words. A blocking rule with a loose `command_rx` is
  the worst failure this skill can ship: it stops work, and the person it stops
  did not write it.

Filing it is not arming it — see step 5. `mode` is the rule's own field, so a
blocking rule needs the same `command_not_rx` exemptions and the same
`--fires` / `--silent` proof as any other; if anything, prove it harder.

**`given` — facts the call must also satisfy.** A matcher rule may carry a
`given` block inside its `matcher`; the regex is checked first, then these,
and the hook answers them from read-only git and the local transcript, once
per call. A fact it cannot establish never satisfies a predicate, so the rule
stays silent rather than firing on a guess.

| the rule says… | `given` |
|---|---|
| never push to main | `{"repo": {"branch_rx": "^(main|master)$"}}` on a `git push` matcher |
| a PR that changes source needs a test | `{"repo": {"diff_paths_rx": "^src/", "diff_paths_none_rx": "(^|/)tests?/"}}` on a `gh pr create` matcher |
| keep PRs under 500 lines | `{"repo": {"diff_lines_gt": 500}}` |
| don't commit unless asked | `{"user": {"not_said_rx": "\\b(commit|push|ship)\\b"}}` on a `git commit` matcher |
| don't pull a whole big file into the main context — delegate it | `{"file": {"lines_gt": 350}, "agent": {"main": true}}` on a `read` matcher (a subagent's reads pass: delegation is the way past the rule) |
| subagents may not push | `{"agent": {"main": false}}` on a `git push` matcher |

`repo` keys: `branch_rx`, `branch_not_rx`, `diff_lines_gt`, `diff_files_gt`,
`diff_paths_rx`, `diff_paths_none_rx` (the branch's changes against its base,
working tree and untracked files included), `dirty`. `user` keys: `said_rx`,
`not_said_rx` (what the person typed this session — never a tool result or
injected context). `file` keys (read rules only): `lines_gt`, `bytes_gt` —
what the call would pull into the context, so a Read with `offset`/`limit`
or a `head -50` counts only those lines. `agent` keys (any event): `main`
(`true` = the main agent, `false` = a subagent). An unknown key drops the rule at load, exactly as a bad
pattern does. A backend that predates `given` refuses it at `create_rule`;
verify locally (step 4) and file once the backend accepts it.

**Matcher-authoring rules:**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and are the main false-fire class.
  Set `match_heredoc_body: true` **together with** `body_rx` only if the rule
  targets what a heredoc says.
- Patterns must be **shape-specific**: match the violating *form* (`git push
  [-f|--force]`), never a keyword that also appears in innocent content.
- Every known-legitimate exemption goes in `command_not_rx` now, not after it
  fires. Give bash rules a `command_not_rx` that exempts commands which merely
  mention the pattern (`python -c`, `grep`).
- Default `warn_once_per: "session"` — a rule that nags every call gets ignored.
  `turn` is for rules where each occurrence matters (e.g. force-push).
- **Never anchor a `command_rx` with `^`.** Real commands arrive chained and
  prefixed: `cd .. && gh pr create`, `cd sub; npm test`, `(cd pkg && git push)`,
  `env CI=1 pytest`. A pattern anchored at the start of the string matches none
  of them, and the rule then fires for some people and not others with nothing
  to show why — the worst failure a rule has, because it looks like the rule
  working. Match at **command position** instead: `(?:^|[;&|(]\s*)` before your
  trigger, or simply no anchor at all plus a `command_not_rx` for the
  mention-in-argument cases you are protecting against. This is the mirror
  image of the SILENT guidance in step 4: that guards the false positive, this
  guards the false negative.

### 4. Prove it fires — and prove it stops

A rule that matches the command you had in mind can still be wrong in three
ways that only show up once the whole team has it. Run the candidate through
the engine that will actually run it:

```bash
cat > /tmp/cand.json <<'JSON'
{"title": "...", "statement": "...", "delivery": "agent_hook",
 "matcher": {"event": "bash", "command_rx": "...", "command_not_rx": "..."}}
JSON
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires 'the real command that should trigger it' \
  --silent 'the same situation once someone has complied'
```

For a `read` rule a case is the Read tool (`read:<path>`, narrowed with
`@<offset>,<limit>`) or a shell command run through the hook's own parser
(`bash:<command>`, relative paths against `--cwd`); `--file-lines N` stands
in for every named file's length so no real file is needed, and
`--agent-main false` runs a case as a subagent:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --file-lines 900 --cwd /repo \
  --fires  'read:/repo/src/service.py' \
  --fires  'bash:cd /repo && cat src/service.py' \
  --silent 'read:/repo/src/service.py@1,200' \
  --silent 'bash:cat src/service.py | head -50' \
  --silent 'bash:grep -n foo src/service.py'
```

For an `edit` / `write` rule a case is `path::content`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires '/repo/src/db.py::conn = connect(url, verify=False)' \
  --silent '/repo/src/db.py::conn = connect(url)'
```

A rule with a `given` block needs the facts it asks about — the fixture IS the
repo; no git runs and no transcript is read. Give them for every case
(`--branch`, `--diff-path` (repeatable), `--diff-lines`, `--dirty`,
`--user-said` (repeatable)) or per case as objects in a `--cases` file:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires 'git push origin HEAD' --branch main
cat > /tmp/cases.json <<'JSON'
{"fires":  [{"case": "gh pr create --fill", "diff_paths": ["src/a.py"]}],
 "silent": [{"case": "gh pr create --fill", "diff_paths": ["src/a.py", "tests/test_a.py"]}]}
JSON
```

An `ordering` rule is verified as a sequence of steps joined by ` >> `
(`edit:<path>`, `ok:<cmd>` a green receipt, `red:<cmd>` a red one, `session`
the SessionStart arming, `prompt:<what the person typed>` the
UserPromptSubmit arming, and last `gate:<cmd>`); the case fires when that
final call is gated. Use the arming step your rule's `armed_by_events` names
— a case that never arms the rule can never fire:

```bash
# armed_by_events: ["edit", "write"]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'edit:src/a.py >> gate:git push' \
  --fires  'edit:src/a.py >> red:pytest tests >> gate:git push' \
  --silent 'edit:src/a.py >> ok:pytest tests >> gate:git push'

# armed_by_events: ["session"]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'session >> gate:git log origin/main' \
  --silent 'session >> ok:git fetch -q >> gate:git log origin/main'

# armed_by_events: ["prompt"] + armed_by_rx
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'prompt:is the staging brain 404ing? >> gate:gh pr comment 7 --body ok' \
  --silent 'prompt:how is production? >> gate:gh pr comment 7 --body ok' \
  --silent 'prompt:check staging >> ok:curl -s https://staging/health >> gate:gh pr comment 7 --body ok'
```

A `prompt:` whose text does not match `armed_by_rx` arms nothing, so it is
the natural `--silent` case: it proves the rule stays quiet when nobody
raised the subject.

It exits non-zero until every case behaves. **Do not file a rule while it
exits non-zero, and show the table to the user.** What each line means:

- **LOAD** — whether the hook would load the rule at all. A pattern over 400
  characters, one that does not compile, or one that backtracks is dropped
  *silently* on every teammate's machine: the rule exists, is active, and
  never fires. This line is the only warning you get.
- **FIRES** — your `--fires` cases. At least one is required; without it
  nothing has shown the rule can trigger.
- **SILENT** — your `--silent` cases, plus two generated for you: `grep` and
  `python -c` quoting the rule's own trigger. Add one more yourself: the
  trigger inside a quoted argument (`--allowedTools 'Bash(git push:*)'`,
  `echo "…"`, a commit message) — measured live, this mention-in-args form
  is the largest false-fire class after `grep`. Searching for a rule's trigger
  is how people investigate it, and firing there is the largest false-fire
  class we have measured. If those two fail, add a `command_not_rx`.

**Always give at least one `--fires` case in CHAINED form** — `--fires 'cd .. &&
<your trigger>'` alongside your plain case. A rule whose chained form does not
fire is not ready to file, and the anchor that broke it (step 3) is invisible
in a table that only ever tested the bare command.

**Always give at least one `--silent` case for the complied-with form** — the
code *after* someone does what the rule asks. This is the check authors skip
and the one that matters most: a rule that keeps firing once you have fixed
the problem cannot tell a violation from a fix, so people learn to ignore it.
If you cannot write a `--silent` case that the rule passes, the rule is not
expressible as a pattern — make it `anchor_recall` or a `session_context`
note instead of shipping a nag.

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

**4b.2 Scaffold a scratch worktree — on a BRANCH, not detached.**

```bash
SCRATCH=$(mktemp -d)
git -C <repo> worktree add -b rulebook-fwd-<8 hex> "$SCRATCH/rulebook-forward-test" HEAD
```

Real layout, real remote, real repo identity — so repo- and path-scoped rules
match without any faking. The hook resolves a rule's repo from the acted-on
file's worktree, so a rule scoped to this repo fires there exactly as it would
in the user's own checkout. **Never run the sub-agent in the user's own working
tree.**

**Use `-b`, not `--detach`.** A detached worktree makes the hook report the
branch as `detached`, so **every `given.repo.branch_rx` / `branch_not_rx`
predicate silently fails** — including the documented "never push to main"
rule. Step 4b is mandatory, so that reads as "the rule never fired" and blocks
filing a perfectly good rule. Verified: in a detached scratch worktree
`given_ok` returns False for `branch_rx: "^(main|master)$"`; on a named branch
it evaluates normally.

**When the rule asks about the branch, name the branch to match.** Read the
candidate's `given.repo.branch_rx` and choose a worktree branch that satisfies
it (`git worktree add -b <matching-name> …`). If the pattern demands a name
that is already checked out — `^(main|master)$` is the common case, and git
refuses a second worktree on it — you cannot exercise that predicate here:
say so, report the branch predicate as **unexercised**, and treat the run as
§4b.6 (ask before filing) rather than reporting a failed rule. Never delete the
`given` block to make the test pass: a fire the rule would not produce in
production is a worse answer than no fire.

**4b.3 Arm the candidate in the local book cache.** The candidate is not filed
yet and a proposed rule never fires, so the test arms it by editing the book the
hook actually reads. Ask the hook where that is — never recompute the hash:

```bash
BOOK=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path "<repo>")
cp "$BOOK" "$BOOK.pretest-$$"        # the restore source
```

Then write `$BOOK` back with the candidate **appended** to `rules` — never
replacing the list, so the team's real rules stay armed and the test window
cannot leave the user unprotected. Normalise the appended copy:

- `id`: `candidate-<8 hex>` — unique, and identifiable if a row ever leaks to
  the ledger;
- `_label`: the candidate's title, so disclosure renders as it will in
  production;
- `status`: `active`;
- **`mode`: `advise`, ALWAYS — even for a rule that will ship as a gate.** A
  gate armed in the user's live session can block the *user's own* next
  command, not just the sub-agent's. Gate behaviour is what
  `rulebook_verify`'s table already proves; what this test adds is that the
  pattern fires in a real session, and advise proves that just as well. Say so
  in the report rather than letting the author believe blocking was exercised.

**Defeat the background re-fetch.** `maybe_refresh` runs on every PreToolUse
and will overwrite the book once the cache is an hour old — silently deleting
the candidate mid-test, which reads exactly like "the rule never fired". You
cannot set `MEMHUB_RULEBOOK_FETCH=0` for hooks that are already running, so
defeat it on its own terms:

- write `fetched_at` in the doctored book as **now**, which makes the age check
  return early;
- also write `{"at": "<now>"}` to `$BOOK.refresh` (backing up any existing
  stamp), which blocks a retry even if the age check is ever changed.

**No cached book for this repo** (nothing fetched yet, or no rulebook binds the
user here) → there is no file to copy: **write one** containing exactly the
candidate plus a `fetched_at` of now. Restore then means *deleting* the file.
Nothing is displaced, because nothing was there — this is not the §4b.6 escape.

**Still leave a marker**: `touch "$BOOK.pretest-absent"` before writing the
book. §4b.5's recovery looks for `$BOOK.pretest-*`, and with no original to
back up there would otherwise be nothing on disk saying a test was running — so
an interruption here would leave a freshly created book holding one unfiled,
armed candidate that the next run cannot discover. The marker means "delete
`$BOOK`", where a `pretest-<pid>` backup means "copy it back". Delete the
marker as part of restoring.

**4b.4 Bracket the ledger around the sub-agent, then run it.** Note the byte
offset of `<base>/ledger/fires.jsonl` (`<base>` is `$MEMHUB_RULEBOOK_BASE` or
`~/.config/memhub-plugin/rulebook`) **immediately before the Agent call, after
the book is already armed**, and note it again **immediately after the Agent
returns, before restoring**. Only rows between those two offsets are evidence.

**Why not "before arming, after restoring".** Your own setup and restore are
shell commands, and the candidate is armed while you run them — so a rule whose
matcher covers `cp`, `git`, `python3` or `rm` fires on §4b.3's
`cp "$BOOK" "$BOOK.pretest-$$"` or §4b.5's restore. The wider window then
reports a fire the sub-agent never caused, and the rule passes a test nothing
exercised. That is worse than a failed test: it is a green light nobody earned.

Then run the sub-agent with the Agent tool on the fake feature prompt,
instructing it to work **only** inside the scratch worktree path.

**4b.5 Restore the book — always, immediately after the sub-agent returns**,
success or failure. Copy `$BOOK.pretest-$$` back over `$BOOK` (restoring its
original `fetched_at` and `etag`), restore or delete the `.refresh` stamp, and
delete the backup. **Verify by hashing**: if the restored file does not match
the backup byte-for-byte, say so loudly and tell the user the path — a doctored
book is a rule set they did not choose.

**An interrupted run does NOT heal itself, so check for one first.** §4b.3
writes `fetched_at` as *now* precisely so the background re-fetch leaves the
book alone — which means that after a Ctrl-C the next SessionStart considers
the doctored book FRESH, renders the candidate, and only spawns a best-effort
background fetch. If that fetch fails, an unfiled rule stays armed
indefinitely. (An earlier version of this step claimed the damage window was
one session. It is not, and the mechanism that makes the test reliable is the
same one that makes the interruption durable.)

So **before arming anything**, look for a leftover from a previous run:

```bash
ls "$BOOK".pretest-* 2>/dev/null
```

Two shapes can come back and they mean OPPOSITE things:

- `$BOOK.pretest-<pid>` — a backup of a real book. **Copy it over `$BOOK`.**
- `$BOOK.pretest-absent` — there was no book before the interrupted run.
  **Delete `$BOOK`.** Restoring a file here would leave the candidate armed.

Either way, restore or delete `$BOOK.refresh`, remove the marker, and tell the
user you found and undid a doctored book from an interrupted run — naming the
file, because a rule set they did not choose was live until you did.

**Evaluate: the ledger first (fact), the transcript second (judgment).**

Read the rows between the two offsets from §4b.4 — not merely "since the
start" — and keep those with `rule_id == "candidate-<hex>"` **that also belong
to the sub-agent**: the row's `session_id` must be this session's, and its
`agent_id` must be the sub-agent's rather than the parent's.

**Why the id check, not just the window.** The book you doctored is the repo's
book, shared by every session on this machine. A teammate's terminal — or your
own second window — working in the same repo loads the candidate too, and a
fire it causes lands in the same ledger inside your window. Filtering on
`rule_id` alone would then report a pass that your fake feature never earned,
which is the one outcome this step exists to prevent. A candidate row outside
the window, or carrying another session's ids, was not caused by the sub-agent
and proves nothing. Report per row: `hook_phase` (pre/post),
`tool`, `mode`, `fired_at`, `excerpt`.

| Ledger result | Meaning | What you do |
|---|---|---|
| ≥1 candidate row | fired | continue to the transcript check |
| 0 rows, candidate still in the book at restore time | did not fire | **do not file.** Report it as a real failure: the pattern passes the verifier's synthetic cases but not a real session. Offer to revise the pattern and re-run. First rule out §4b.2's branch trap — a `given.repo.branch_rx` that the scratch worktree's branch cannot satisfy fails the same way a bad pattern does |
| 0 rows, candidate **gone** from the book | inconclusive — the book was re-fetched mid-test | re-run once; if it recurs, report the environment problem and do not file |

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
removed), the ledger rows, the transcript judgment, and — always — the two
caveats: *the candidate was armed in advise mode, so blocking was not
exercised*, and *this proves the rule fires, not that it is worth firing*. Add a
third when it applies: *the branch predicate was not exercised* (§4b.2).

Cleanup is mandatory and happens even on failure: restore the book, `git
worktree remove --force` the scratch worktree, delete the branch `-b` created
(`git branch -D rulebook-fwd-<hex>`), `rm -rf` the temp dir, delete the backup
files.

**4b.6 The one thing that is not a failure.** If the step cannot be **run at
all** — the repo is not a git checkout, `git worktree add` fails (a bare repo,
no `HEAD`, a filesystem that refuses it), or the Agent tool is unavailable in
this host — then say which precondition was missing in one sentence, say that
the pattern is therefore proven only against the verifier's synthetic cases,
and **ask** whether to file anyway. Nothing is filed without a yes. A
sub-agent that ran and tripped nothing is NOT this case: that is a failing
test, and it blocks. Cleanup still runs.

### 5. Conflict check, confirm, then file

Before showing the rule, check it against the book — the server files a
colliding title or matcher as a silent second draft unless you name what it
replaces, so this is the only place it gets caught. Call `list_rules` (every
status, no `rulebook_id`), save the reply, write the candidate `create_rule`
body to a file, and run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
  --candidates <candidate.json> --existing <list_rules.json> --repo "<repo>" \
  --rulebook-id <the step-0 rulebook_id>
```

Omit `--rulebook-id` entirely on an older backend, where step 0 resolved no
id — passing the flag with nothing after it is an argparse error and you get
no report at all.

`same_title` / `same_matcher` (an **active** rule fires on the same call) /
`anchors_overlap` are deterministic; then read the `judge_by_statement` list
it prints and mark the candidate `duplicate`, `contradicts` or `distinct`
against each (the script prints the exact `supersedes_rule_id` value under
each hit). `duplicate` (or a `same_title` / `same_matcher` hit you judge to
be the same rule) → file with `supersedes_rule_id: <that rule's rule_id>`;
the server files it as `proposed` and activation replaces exactly that rule.
`same_matcher` against an **active** rule that is NOT the same rule → do not
file; tell the user. `contradicts` → file as a draft WITHOUT
`supersedes_rule_id`, but name the rule it fights in the report; a reviewer
retires one side before activating the other.

A hit marked **`cross_book`** is in a rulebook you are not filing into.
`supersedes_rule_id` cannot reach it — whatever you file, both rules stay live
and both fire on the same call. Do not file over it silently: name the book and
the rule to the user and let them choose (retire one side in MemHub, narrow one
rule's `scope_repos` / `scope_paths`, or file anyway and accept the double
fire).

Show the user: the rule sentence, the delivery + engine block, the sample
commands it does and doesn't match, and the conflict verdict. On approval call the memhub
**`create_rule`** tool with `title`, `statement`, `delivery`, the engine
block, `scope_repos`, `source_ref` (e.g. `<path/to/CLAUDE.md>@<sha>#<heading>` or
`user correction, session <id>`, with the step-1b numbers appended:
`|applies N/M|precision P`), `supersedes_rule_id` when it replaces a
rule, `mode: "gate"` if the user asked for a rule that stops the command, and
`rulebook_id` from step 0. Read the reply:

- `unchanged: true` → identical content is already in the book (a retried
  call with the same `source_ref` path and title); nothing written.
- `status: "proposed"` + `supersedes_rule_id` → filed as a replacement for
  the rule you named; it retires that rule when a reviewer activates it.
- `status: "draft"` → new.

**New rules always land for review — never pass `activate`.** That holds even
on a book that binds only the user, where the server would let them arm their
own rule: the point of the review step is that somebody reads the rule after
the excitement of writing it.

**A blocking rule is filed the same way, and blocks nothing until it is
turned on.** `mode: "gate"` is stored on the rule and travels with it through
review; it is the reviewer turning the rule on that puts the block in front of
anyone. So filing one is safe, and the reply says as much — read the `mode`
back and report what it says rather than promising the user their command is
blocked from now on. If the server refuses the mode, the rule is not on a
command (see step 3): file it advising and tell the user which shape would
block.

If the rule is better as a plain suggestion than a check — the user doesn't
want to write a detector — file it the same way with
`source="nomination"` and no engine block; it lands as `proposed` for a
reviewer.

### 6. Report

Tell the user: which **rulebook** it went into and who that book binds
("org-wide" or "N members" — that is the set of people this rule will reach);
that it is filed as a draft — or as `proposed`, naming the rule it replaces by
title; and what happens next: the rule's owner or an admin activates it in
MemHub (a `proposed` rule retires the one it replaces), every **member of that
rulebook** picks it up on their next session, and its firing history accrues in
MemHub as the evidence that later decides whether to keep, narrow, or retire
it. Name any `cross_book` collision here too, under **Conflicts to resolve**.

If the rule was filed to stop the command, say so in the same breath as who it
reaches: once it is turned on, that command stops for everyone the book binds,
and each of them can still run it by putting `RULEBOOK_OVERRIDE='<why>'` in
front. Do not report a blocking rule as already blocking — it is waiting for
the same review as any other.
