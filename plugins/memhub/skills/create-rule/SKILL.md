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

## Handed a turn by the harness

MemHub's harness can block a stop with `MemHub harness: before you stop: turn
N of this session was flagged as …`. Then you are here because a turn was
flagged, not because the user asked for a rule, and two things differ:

**This section, not the harness line, is the harness path.** The line is
rendered to the person as `Stop hook feedback:` — there is no Stop channel that
reaches only the model — so it stays a pointer and everything it used to carry
lives here.

**The invariant, before the details: nothing on this path reaches the person
except one line naming a rule that was filed.** Every step of this skill that
shows, asks, offers or reports — §4b.1's prompt preview, §4b's cleanup report,
the revision offer when a forward test finds nothing, the conflict choices,
the step-5 confirmation — is suppressed here, including ones added after this
was written. Where a step says to stop and ask, stop silently instead. The
enumerations below say what to DO at each such point; this says what the
person sees, which is nothing until there is a rule.

**One exception, and it is not about the rule: the rulebook cache.** The
invariant covers disclosures about the CANDIDATE; it does not cover machine
state this skill changed. Since §4b.3 arms the candidate in a private base and
never writes the shared book, there is now only one such notice, and it is
spoken even though nothing was filed:

- **The claim did not take** — `book-path` still answers with the shared book
  after you wrote the redirect. Say so, name the file, and **abort before
  arming anything**. Carrying on would mean doctoring the book every other
  session on the machine reads, which is what the claim exists to prevent.

(There used to be a second notice: a failed restore, or a `$BOOK.pretest-*`
left by an interrupted run, either of which meant an unreviewed candidate was
armed in the shared cache. Neither can happen now — nothing shared is written,
so there is nothing to restore and nothing to recover. If you are reading this
skill against an older plugin, that recovery still matters; against this one it
is gone rather than merely unlikely.)

Silence about the claim failing is a safety bug wearing the costume of quiet.

- **The test.** A lesson is one that would change what an agent DOES next time,
  is not already a RULE, is not project state, and will still be true next
  month. Already written in CLAUDE.md or the docs does NOT disqualify it: if
  this turn tripped over it anyway, the prose was not enough — file it and cite
  where it is written. Skip only when nothing went wrong and you would merely
  be restating the docs.
- **No lesson** → say nothing to the person about the turn, and stop. Nothing
  is recorded: how often a block produces a rule is already the `handed` rows
  in the moments file against `session_draft` rules on the server, and whether
  a rule HELPS is the fire-event fold's question, not this one's.
- **A lesson** → run this flow with it as the user's words, and **ask the
  person nothing at all**. A `session_draft` lands `proposed` and fires for
  nobody until a reviewer activates it, so every confirmation this skill asks
  for elsewhere is already held by whoever reviews the book. Concretely, on
  this path:
  - **Step 0 (which rulebook)** does not ask, and never guesses. In order:

    1. Exactly one `bound` book → file there.
    2. Exactly one `bound` book with `scope: all_org` among several → file
       there.
    3. **Otherwise — several `all_org` books bound, or no book bound at all —
       the repo's own book**, named `Rulebook: <repo>` exactly as `scope_repos`
       spells the repo. Use the bound one with that name if it exists; if none
       does, `create_rulebook` it with `scope: "explicit"`.

    Rule 3 replaced "anything else → file nothing". That clause read as
    fail-closed and was not: the arithmetic is prose, a model applies it, and
    on an org with TWO bound `all_org` books it refused once and picked a book
    every other time. Silently non-deterministic about which team's rulebook
    gets written to is worse than either answer — a per-repo book is one the
    configuration can always produce, so there is nothing left to resolve.

    **Read `member_count` off the create reply before filing into a new book.**
    `create_rulebook` seeds an `explicit` book with you — *unless you are an
    organisation admin*, who is seeded only by naming themselves in
    `member_user_ids`. An admin who takes the default therefore gets a book
    binding NOBODY, and "a book that binds nobody serves its rules to no
    session": the rule files, the reply says success, and it reaches no one.
    So pass your own id in `member_user_ids` when you know it, and either way
    treat `member_count: 0` as a FAILURE — report that the book was created but
    binds nobody and needs a member, rather than filing into a void.

    A book created here is `explicit` and small on purpose. `all_org` is an
    admin act that binds everyone in the organisation, now and in future, and
    nobody can leave it — not a thing to do on a path that asks nobody
    anything.
  - **Step 4b.6** (no git checkout, no Agent tool) does not ask. Say the
    precondition was missing, file with the pattern proven only against the
    verifier's synthetic cases, and note that in the report.
  - **Step 5** does not ask. File, then tell the person one line naming the
    rule.
  - **A conflict that the mandatory policy says not to file, is not filed** —
    and on this path it is not reported either: `cross_book` (a rule in a book
    `supersedes_rule_id` cannot reach), or `same_matcher` on an active rule
    that is not this one. File nothing, say nothing, stop.
  - **A live verification that runs and fails is terminal.** The forward test
    firing on zero candidate rows is not a conflict and not a missing
    precondition: the pattern is unproven, so file nothing, say nothing, and
    stop. Do not offer to revise it — that offer is the interruption this path
    exists to avoid, and an unproven matcher is worse than no rule.
  - **Never pass `activate`.** Never put a person's name, home directory or
    e-mail in a rule.
  - **`unchanged: true` is a filing, not a blocker.** That reply means the rule
    is already in the book — a retry after a lost response, or identical
    content re-filed. The moment ended WITH a rule, so tell the person as you
    would for any filing.

- **`scope_repos` is the harness line's, verbatim** — it is already narrowed by
  `proposal_scope`. When the line says the turn worked in several repositories,
  cut it further to the ones the lesson is about. Do NOT rebuild it from
  `state.touched_repos`: that re-broadens it to repos the lesson has nothing to
  do with, which then fire on unrelated work.
- **`source_ref` is passed EXACTLY as the harness line gives it.** The generic
  steps append `|applies N/M|precision P` to a `source_ref`; on this path they
  do not. That value is half of the server's `(rulebook, source_ref, title)`
  re-import identity, so a retry carrying different evidence counts files a
  second row instead of matching the first. Put those numbers in the report to
  the person instead.
- **The stamp** comes from the moments file
  (`$MEMHUB_HARNESS_DIR`, else `~/.config/memhub-plugin/harness`),
  `<session_id>.moments.jsonl`: the last JSON object whose `source_ref` matches
  the harness line's. Pass its `state` verbatim in step 5 with
  `source="session_draft"` and that `source_ref` — MemHub refuses a
  `session_draft` without its `state`.

`mode: "gate"` still needs the user's own words asking for a block (step 3).

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
File with no destination — omit `rulebook_id` and let the server put the rule
where it used to — and say so in step 6; the rest of this skill is unchanged.
Do NOT reach for `agent_brain_id`: `create_rule` has no such parameter any
more, so passing it turns a degraded-but-working file into a failed one.

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
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" \
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
at once from sessions, use `/memhub:start-rulebook` instead.

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
fire. Advice has the same channel, one call later: an agent that reads an
advisory and goes on without it says why on its next command as
`RULEBOOK_OVERRIDE='[<label>] <why>'`, naming the rule, and the reason lands on
that rule's fire. A rule with a `converted_rx` also records "not followed" on
its own: a fire whose conversion has not been seen two turns later is closed
`converted=false`, so a rule nobody acts on shows it instead of showing
nothing. The hook only reports what it saw — the command that converted, the
override that set a rule aside, each turn ending — and the server decides the
outcome from those facts (the earliest one after the fire wins).

**Also say what the rule PREVENTS.** `predicts_rx` is a pattern over tool
output naming the failure this rule exists to stop — the traceback, the
`rejected` line, the 409. It changes nothing about when the rule fires; it is
what lets a fire be scored as a catch rather than counted as a nag, and it is
far easier to write now, while the war story that produced the rule is in
front of you, than at review time. Write it wherever the failure has a
recognisable line; skip it for a rule whose violation produces no output.

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

**4b.3 Arm the candidate in a book that is YOURS.** The candidate is not filed
yet and a proposed rule never fires, so the test has to arm it in a book the
hook really reads. It does **not** arm it in the shared one. That file is a
single book per repo, read by every session on this machine working in that
repo, on every `PreToolUse` — so doctoring it hands an unfiled, unreviewed rule
to your colleagues' sessions and to your own other terminals, and two tests
that interleave leave one armed with no backup left to find it by.

Instead, claim a private base for the calls made inside your scratch worktree:

```bash
BASE="${MEMHUB_RULEBOOK_BASE:-$HOME/.config/memhub-plugin/rulebook}"
PRIV="$(mktemp -d)/pretest-base"
mkdir -p "$PRIV/book" "$PRIV/ledger" "$PRIV/state"
cp -R "$BASE/book/." "$PRIV/book/"          # start from the real rules

# the claim: calls made under <scratch worktree> read $PRIV, nothing else does
python3 - "$BASE" "$PRIV" "<scratch worktree>" <<'EOF'
import json, os, sys
base, priv, wt = sys.argv[1:4]
p = os.path.join(base, "pretest-redirect.json")
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"base": priv, "cwd_prefix": wt, "pid": os.getpid()}, f)
EOF

# where the sub-agent's calls will actually read from — check before arming
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path "<repo>" "<scratch worktree>"
```

That last line must print a path under `$PRIV`. If it prints the shared book,
the claim did not take (a stale or unreadable redirect reads as *no redirect*,
by design) — **stop, and do not arm anything**. Ask the hook rather than
recomputing the hash, here as before.

Then write the candidate into the book **under `$PRIV`**, appended to `rules` —
never replacing the list, so the real rules stay armed and the sub-agent runs
under the same posture a real session has. Normalise the appended copy:

- `id`: `candidate-<8 hex>` — unique, and identifiable if a row ever leaks to
  the ledger;
- `_label`: the candidate's title, so disclosure renders as it will in
  production;
- `status`: `active`;
- **`mode`: the mode the rule will ship with.** A gate candidate stays a gate.
  Earlier versions of this step forced `advise` here, because a gate armed in
  the SHARED book could refuse the person's own next command — but the claim
  above is what removes that hazard, and forcing advise would throw away the
  one thing a live test can show that `rulebook_verify`'s table cannot: what
  the rule does to a real call, made by a real agent, in a real session. A
  gate that fires is the evidence the author needs before asking anyone to
  activate it for the team.

  The hook confines this rather than weakening it: a `candidate-` row read
  from the shared base is **dropped**, so the gate reaches the sub-agent under
  your claim and reaches nobody at all outside it. Report what the sub-agent
  actually hit — a refusal is a result, not a failure.

**Defeat the background re-fetch.** `maybe_refresh` runs on every PreToolUse
and will overwrite the book once the cache is an hour old — silently deleting
the candidate mid-test, which reads exactly like "the rule never fired". The
private base does not change this: the fetch lane writes into whichever base
the call resolved to. You cannot set `MEMHUB_RULEBOOK_FETCH=0` for hooks that
are already running, so defeat it on its own terms, in the private book:

- write `fetched_at` as **now**, which makes the age check return early;
- also write `{"at": "<now>"}` to `<private book>.refresh`, which blocks a
  retry even if the age check is ever changed.

**No cached book for this repo** (nothing fetched yet, or no rulebook binds the
user here) → `cp -R` copies nothing and you simply write the private book
yourself, containing exactly the candidate plus a `fetched_at` of now. Nothing
is displaced, because nothing was there and nothing shared is touched either
way — this is not the §4b.6 escape.

**4b.4 Run the sub-agent.** Run it with the Agent tool on the fake feature
prompt, instructing it to work **only** inside the scratch worktree path —
which is also what makes the claim apply to it, since the redirect is keyed on
that path.

No ledger bracketing is needed any more. The sub-agent's fires land in
`$PRIV/ledger/fires.jsonl` and nothing else writes there, so **every row in
that file is evidence**. The old byte-offset window existed because the shared
ledger also carried your own setup commands and, worse, concurrent sessions'
fires — a rule whose matcher covered `cp`, `git`, `python3` or `rm` could pass
a test nothing exercised. A private ledger removes the problem rather than
narrowing the window around it.

**4b.5 Release the claim — always, immediately after the sub-agent returns**,
success or failure:

```bash
rm -f "$BASE/pretest-redirect.json"     # release first: the claim is what steers
rm -rf "$PRIV"                          # then the private base
```

Release the claim **before** deleting the base, in that order: while the
redirect still points at a base that no longer exists it reads as *no
redirect*, which is the safe direction — a call falls back to the real book
rather than to a missing one.

There is nothing to restore and nothing to verify by hashing. The shared book
was never written, so it cannot have been left doctored; that is the whole
point of the claim, and it is what replaces the old backup-and-copy-back dance
(`$BOOK.pretest-<pid>` / `$BOOK.pretest-absent`) along with its two markers
that meant opposite things.

**An interrupted run now heals itself**, which it did not before. A Ctrl-C
leaves a redirect file and a temp base behind; the redirect stops steering
anything after an hour (`REDIRECT_MAX_AGE_S`), and until then it only ever
affects calls made inside that scratch worktree, which the interruption also
abandoned. No unfiled rule is left armed for the user or for anyone else on
the machine, in any session, at any point — so there is no pre-run recovery
scan to do and nothing to report.

If you *do* find a `pretest-redirect.json` from an earlier run while setting
up, just overwrite it: one claim at a time, and the newest one owns the file.

**Evaluate: the ledger first (fact), the transcript second (judgment).**

Read `$PRIV/ledger/fires.jsonl` — all of it — and keep the rows with
`rule_id == "candidate-<hex>"` **that also belong to the sub-agent**: the
row's `session_id` must be this session's, and its `agent_id`
must be the sub-agent's rather than the parent's.

**Why the id check, even now that the ledger is private.** The claim keeps
other sessions out, so the old hazard — a teammate's terminal, or your own
second window, loading the candidate from the shared book and firing it inside
your measurement window — cannot happen any more. What the id check still
catches is nearer to home: your own setup and teardown commands run inside the
scratch worktree too, under the same claim, so a candidate whose matcher covers
`cp`, `git`, `python3` or `rm` can fire on *your* shell rather than on the
sub-agent's work. Filtering on `rule_id` alone would report a pass the fake
feature never earned. A candidate row carrying another session's ids, or the
parent's rather than the sub-agent's, was not caused by the sub-agent
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
commands it does and doesn't match, and the conflict verdict.

**A `session_draft` handed over by the harness skips that approval** — it files
straight away and tells the person afterwards in one line. The draft lands
`proposed` and fires for nobody until a reviewer activates it, so the approval
asked for here is already held by whoever reviews the book; asking again
mid-turn is the interruption the Stop block exists to avoid. A `cross_book`
conflict does NOT reach the person on this path either: it files nothing and
says nothing. The person hears about the turn only when a rule was filed.

On approval — or immediately, for a harness draft — call the memhub
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
