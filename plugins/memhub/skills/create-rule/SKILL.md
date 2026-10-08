---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake", "make it actually stop me"). Pins a when-X-then-Y sentence, drafts a deterministic check, and files it for review through the memhub `create_rule` tool — a rule that advises and a rule that stops the command are filed the same way, and a reviewer turns either one on.
argument-hint: '[--scope org|personal|workspace] [--workspace "<name>"] [the rule, in your own words]'
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_hook.py" book-path *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_paths.py"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" *), Bash(git remote get-url origin), Write(//tmp/*.json), Edit(//tmp/*.json), Edit(//tmp/*/pretest-base/**), Edit(//var/folders/**/pretest-base/**), Read, Task, Agent, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub_memhub__list_rulebooks, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rulebooks'
---

You are creating a **Rulebook rule**: a human-authored, team-owned rule stored
in MemHub, fetched once per session by the coding agent of everyone in the
rule's **scope** (who it applies to), and measured on every fire. Rules are data, not prose in a
doc — and a rule with a loose check fires on innocent commands more often than
not, so the check is where the care goes.

There is no local rule file. The write path is the memhub **`create_rule`**
MCP tool; the rule reaches the people in its scope once a reviewer activates
it (an org admin; for a rule that applies to just the user, the user
themselves).

Arguments: `$ARGUMENTS`
- `--scope org|personal|workspace` (+ `--workspace "<name>"`) → who the rule
  applies to; omit and step 0 decides.
- `--rulebook "<id>"` (deprecated) → passed through as `rulebook_id`. One that
  is not an id is ignored ("rulebooks are scopes now").
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## Handed a turn by the harness

If your prompt begins `MemHub harness fork, moment <session_id>#<turn>`, you
are on the **Harness-draft path**: Read `${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/harness.md` **before step 0** and follow it — it
changes step 0, step 4b and step 5, and nothing on that path reaches the person
except the fork's one final line. Everyone else: skip this section.

## 0. Who it applies to — decide it first

Every rule goes into one **scope**, and the scope is who it applies to:

| `scope` | applies to |
|---|---|
| `org` (**the default**) | everyone in the organisation |
| `personal` | just the user |
| `workspace` | the people in one workspace — **only when the user asks for it** |

Call `list_rulebooks` and read its `scopes` block (`org` / `personal` with
`label` and `applies_to`, `supports_workspaces`, `workspaces[]`); do not
enumerate books. Then, in order:

1. `--scope` given → use it (`--workspace` names the workspace).
2. The user's own words say "just me", "only me", "for myself" or
   "personal" → `personal`.
3. The user names a workspace or teamspace that matches a
   `scopes.workspaces[].name` (case-insensitive) and `supports_workspaces` is
   true → `workspace`, with that `workspace_id`.
4. **Otherwise use `org`.** Do not ask, and never offer the workspace scope
   unprompted.

A named workspace that is not listed, or no `supports_workspaces`, is said
once and the rule goes to `org`. No `scopes` block (an older server) → file
without `scope`. Show the chosen scope's `label` and `applies_to` in the
step-5 preview, so the user can change it.

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. A conditional shape is what
makes a rule actionable; a bare observation is not a rule. If the user gave a
war story, extract the conditional from it and confirm your reading.

The example carried through every step below is a real rule:

> Never force-push (-f, --force, --force-with-lease) to main, master or
> staging: it rewrites history teammates have pulled. Rebase onto a fresh
> origin branch and open a PR instead. Force-pushing your own feature branch
> is fine.

**Then write the sentence down as its parts.** The agent is shown the
`statement` (step 3); the server's **rule judge** reads four fields to decide,
when the pattern matches, whether the agent is really in the rule's situation.
Both come from the same sentence: **the two say the same thing,
nothing in one that the other lacks.**

| field | what it holds | at most |
|---|---|---|
| `when` | the X — the **situation** the rule is for, one sentence about what the agent is doing or about to do | 300 chars |
| `do` | the Y — what the rule asks | 400 chars |
| `why` | the incident or reason behind it | 400 chars |
| `when_not` | situations the rule does **not** apply to — a list | 8 entries, 200 chars each |

- **`when` is a situation, never trigger vocabulary.** It describes what is
  happening in the turn, in words that would still be true if the command
  were spelled differently.
  - Good: *"The agent is about to rewrite the history of a branch other
    people build on."*
  - Bad: *"The command contains git push --force."* That is the pattern
    restated; the pattern already matched, so it tells the judge nothing.
- **`when_not` only for exclusions the person or the evidence actually
  named** — "except on my own branch", a step-1b sample that was plainly
  innocent. Otherwise omit it: it is usually empty when a rule is filed, and
  it is where confirmed misfires get written later. Each entry is a situation
  too, never a pattern: the example's statement names one exemption, so it
  gets *"The agent is updating its own feature branch after a rebase."* —
  not *"not when the branch starts with feat/"*.
- **`do` and `why` are the sentence's own halves**, not a second draft of
  them. If the person gave no reason and no incident, leave `why` out rather
  than invent one. The example: `do` *"Push to a feature branch and open a PR;
  never force-push main, master or staging."*, `why` *"A force-push rewrites
  history teammates have already pulled."*

### 1b. Evidence: how often would it have applied?

A rule is worth the team's attention in proportion to how often the
situation actually occurs. Once step 3 has a matcher, write the candidate
`create_rule` body to `/tmp/cand.json` (step 4 shows it) and replay it over
the local transcripts (Claude Code, Codex, Cursor) — about a minute:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/start-rulebook/scripts/mine_sessions.py" \
  --rule-file /tmp/cand.json --out /tmp/mine
```

It prints the whole start-rulebook report; find your title under PROPOSED
RULES and read its `evidence:` line — `applies-in N/M sessions` (by host) and
one sample (more in `/tmp/mine/proposals.json`). For a "do X before Y" rule
add `"requires_prior_rx": "<X>"` to the body — the line then shows
`precision = fired-with-no-prior-X / fired`; below ~50 % the matcher would nag
people who already complied, so use the `ordering` shape (step 3) or make it
`session_context`. The row's `rule_evidence` is step 5's `evidence` (Read
`${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/labels.md`). If N is 0 across
all hosts, say so to the user before filing — it may still be right
(insurance for a new teammate) but it is not lift. The example scores
`applies-in 0/855`: insurance; the rule it replaces scores 66/855, nagging
force-pushes to people's own branches. For many rules from sessions, use
`/memhub:start-rulebook`.

### 2. Duplicate check — by eye now, deterministically in step 5

Call the memhub `list_rules` tool with **no `rulebook_id`** and
**`include_retired=True, limit=200`** so the reply spans every rulebook the
user can see in every state, and read the new rule against every title and
statement. Same subject → plan to replace the existing rule
instead of adding a twin: note its `rule_id` for `supersedes_rule_id` in step 5.
The server never picks what a rule replaces by title — you decide, with
`supersedes_rule_id`. It does refuse a rule whose title or trigger equals a
live rule's in the same book (step 5), and it answers an identical retry
`unchanged`. With a `source_ref`, the re-import key is (book, `source`,
`source_ref` minus its `@<sha>` and `#…`, normalised title): re-filing under
the same document and title supersedes that rule (or answers `unchanged`), so
a `source_ref` is not free text to vary between attempts. Keep the
`list_rules` reply: step 5 runs the deterministic check over it.

`include_retired=True` matters: a rule someone already dismissed is exactly the
twin you must not re-file, the default view hides retired rules, and step 5's
script skips them too — this read is the only check that sees them. `limit` is
200 at most — if the reply says `has_more`, ask again with `offset` and
concatenate `rules` before running the check, or the comparison silently misses
whatever fell off the first page.

### 3. Draft the rule — one delivery, one engine block

| the rule is… | `delivery` | engine block |
|---|---|---|
| a Bash command with a checkable form | `agent_hook` | `matcher: {event: "bash", command_rx, command_not_rx?, warn_once_per}` |
| an edit/write to certain paths or content — by the Edit/Write tools OR by a Bash command that wrote the file (heredoc, `write_text()`, `sed -i`; that one is seen only after the write, so it can advise but never gate) | `agent_hook` | `matcher: {event: "edit", path_rx, path_not_rx?, content_rx?, content_not_rx?}` — `content_not_rx` exempts the complied-with form |
| a failing or noteworthy tool output | `agent_hook` | `matcher: {event: "output", content_rx, command_rx?, content_not_rx?}` |
| a file about to be read into the agent's context — the Read tool, OR a `cat`/`head`/`tail`/`less`/`more`/`sed` on a path in a Bash command (not piped, not redirected; `cd`-relative paths resolve) | `agent_hook` | `matcher: {event: "read", path_rx?, path_not_rx?, command_not_rx?, given: {file: {lines_gt}, agent: {main}}}` — needs `path_rx` or `given.file`, or it fires on every file |
| "run X after edits, before Y" | `agent_hook` | `ordering: {required_command_rx, gated_command_rx, armed_by_events, path_rx?, min_edits, display_name}` — `path_rx` limits which edits arm it |
| "run X once a session, before Y" — X is not owed to an edit, it is owed to the session (`git fetch` before reading `origin/*`) | `agent_hook` | the same `ordering` block with `armed_by_events: ["session"]`: armed at session start, discharged by one green X, and re-armed for the next session |
| "when the person asks about Z, do X before answering" (probe staging before answering a staging question) | `agent_hook` | the same `ordering` block with `armed_by_events: ["prompt"]` **and** `armed_by_rx` — the pattern the prompt must match. Without `armed_by_rx` the rule arms on nothing; only what a person TYPED arms it, never a slash command's body or a loop wake-up |
| when a wake-up or background notification arrives — a `/loop` tick, a monitor event, a `<task-notification>` — and the rule is about what the agent says next | `agent_hook` | `matcher: {event: "prompt", prompt_rx, prompt_not_rx?, warn_once_per}` + `min_hook_version: "0.99.0"`. Matched against the RAW prompt, harness wrappers included (`<command-message>loop</command-message>`, `<task-notification>`, `<<autonomous-loop`). **Advise only** — the prompt is already sent, so there is nothing to block and the server refuses `mode: "gate"`. `given` takes `agent` and `repo` only |
| applies when a file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — matched whole, case-sensitive, in the command or path (so never `spec`, `pytest` or text with spaces); the judge decides fit |
| worldview with no trigger at all | `session_context` | none — at session start the hook shows at most 15 such notes in about 3,300 characters, shared across every book that binds the person (at 400-character statements that is about 8); prefer a checkable shape when one exists, because advice shown in-flight is acted on far more often than advice shown at session start |

Plus on every rule:
- `title` — short, imperative, under 60 characters (200 max).
- `statement` — the line shown when it fires, with sanctioned forms and
  exemptions; 400 characters max.
- `scope_repos` — `[]` for every repo, or `["<repo>"]` where `<repo>` is
  `basename $(git remote get-url origin)` without `.git`. **Never the
  directory name**: in a worktree that is the branch, and the rule binds nobody.
- `scope_paths` / `scope_exclude_paths` — globs for edit rules; a Bash call
  carries no path, so an include-scoped rule never fires on one.
- step 1's `when` / `do` / `why` / `when_not` — read by the judge, never shown
  to the agent, no effect on the pattern.
- `categories` — zero or more; see labels.md (step 1b).

**A rule that needs a newer hook.** The server sets `min_hook_version` for
you where the rule's shape implies one (0.54.0 for a session- or prompt-armed
ordering, 0.88.0 for a pattern over 400 characters, 0.99.0 for a prompt
matcher) and refuses an explicit value below that floor. Pass it yourself only
for another newer key. Where the installed hook is older it runs the rule as
ADVICE, never as a gate, and says so once per session — instead of ignoring the
condition and firing as if it held. A key the hook does not know degrades the
same way.

**Advise, or stop the command?** A rule advises by default. `mode: "gate"`
denies the matching call — only when the user's own words ask for a block
("stop me", "don't let me"), and it **stops everyone the rule applies to**.
Before drafting a gate, or a `given` block (branch, diff, what the user said,
file size, main agent vs subagent), Read
`${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/gate-and-given.md`.

**Also say what the rule PREVENTS.** `predicts_rx` (a `matcher` key) is a
pattern over tool output naming the failure the rule exists to stop — the
example's `\(forced update\)`, a traceback, a 409. It lets a fire be scored as
a catch instead of a nag; write it now, while the war story is in front of you.
Skip it when the violation prints nothing.

**Matcher-authoring rules:**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and are the main false-fire class.
  For a rule about what a heredoc SAYS, add `body_rx`: `command_rx` then names
  the shell form (`python3 - <<`) and `body_rx` must match the body.
- Patterns must be **shape-specific**: match the violating *form*, never a
  keyword that also appears in innocent content. The example matches `git
  push`, then (by lookahead) `-f`/`--force` in the same command, then a
  protected branch as the target — so a force-push to a feature branch and a
  plain push to staging both stay silent.
- Every known-legitimate exemption goes in `command_not_rx` now, not after it
  fires. Exempt the trigger **inside a quoted span** (`grep "git push …"`,
  `-m "…"`, `--allowedTools 'Bash(git push…)'`), not by the command's first
  word: an exemption keyed on `grep`/`echo` also silences `echo start; <the
  real violation>`. Keep `;&|` out of the quoted span, or the exemption
  swallows a real command chained after a quoted one.
- Default `warn_once_per: "session"` — a rule that nags every call gets ignored.
  `turn` means **every matching call** (no dedup at all) — only for rules
  where each occurrence matters (force-push). `file` behaves as `session`.
- **Never anchor a `command_rx` with `^`.** Real commands arrive chained and
  prefixed: `cd .. && gh pr create`, `cd sub; npm test`, `(cd pkg && git push)`,
  `env CI=1 pytest`. A pattern anchored at the start of the string matches none
  of them, and the rule then fires for some people and not others with nothing
  to show why — the worst failure a rule has, because it looks like the rule
  working. Don't try to anchor at command position either — a prefix like
  `(?:^|[;&|(]\s*)` still misses `env CI=1 git push` and fires inside
  `Bash(git push:*)`. Use a word boundary (`\bgit\s+push\b`) and let the
  quoted-span `command_not_rx` handle mentions. This is the mirror image of the
  SILENT guidance in step 4: that guards the false positive, this guards the
  false negative.
- **The pattern stays broad; the judge decides fit.** Everything above is
  about a pattern that matches the wrong **shape** — a `grep` that mentions
  the command, a heredoc body, a quoted argument — and narrowing is still the
  right fix for that. It is not the tool for a pattern that matches the right
  command in the wrong **situation** (`ls -d agent-plugins*` while only
  locating a repo, under a rule about reporting one as not cloned). Do not
  narrow a pattern, drop an anchor or add a `command_not_rx` to keep it out of
  a situation: that loses fires the rule should have had. Say the situation
  in `when`, and the one it is not for in `when_not`.

### 4. Prove it fires — and prove it stops

A rule that matches the command you had in mind can still be wrong in three
ways that only show up once the whole team has it. Run the candidate through
the engine that will actually run it:

Write the candidate with the Write tool to `/tmp/cand.json` — this is the
whole running example, exactly as step 5 will file it:

```json
{
 "title": "Don't force-push main, master or staging",
 "statement": "Never force-push (-f, --force, --force-with-lease) to main, master or staging: it rewrites history teammates have pulled. Rebase onto a fresh origin branch and open a PR instead. Force-pushing your own feature branch is fine.",
 "when": "The agent is about to rewrite the history of a branch other people build on.",
 "do": "Push to a feature branch and open a PR; never force-push main, master or staging.",
 "why": "A force-push rewrites history teammates have already pulled; the team's standing rule is that humans drive merges and shared history.",
 "when_not": [
  "The agent is updating its own feature branch after a rebase."
 ],
 "delivery": "agent_hook",
 "matcher": {
  "event": "bash",
  "command_rx": "\\bgit\\s+push\\b(?=[^;&|\\n]*\\s(?:-f|--force)\\b)[^;&|\\n]*\\s\\+?(?:\\S+:)?(?:refs/heads/)?(?:main|master|staging)(?=\\s|$|[;&|)])",
  "command_not_rx": "'[^';&|]*\\bgit\\s+push\\b[^']*'|\"[^\";&|]*\\bgit\\s+push\\b[^\"]*\"",
  "warn_once_per": "turn",
  "predicts_rx": "\\(forced update\\)"
 },
 "scope_repos": []
}
```

Then give it every case that matters — the plain violation, a **chained**
one, the **complied-with** form, and the mentions:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'git push --force origin staging' \
  --fires  'cd ../MemHub-Backend && git push -f origin HEAD:main' \
  --fires  'echo "start"; git push -f origin main; echo "done"' \
  --silent 'git push --force-with-lease origin feat/retry-budget' \
  --silent 'git push origin staging' \
  --silent 'grep -rn "git push --force origin staging" docs/' \
  --silent "python3 -c 'print(\"git push -f origin main\")'" \
  --silent 'git commit -m "docs: never git push --force origin main"' \
  --silent "claude --allowedTools 'Bash(git push -f origin main:*)'"
```

Every line reads `ok` and it exits 0.

The `echo "start"; …` case is there because a first draft of the quoted-span
exemption let `;` inside the span and swallowed that real push. Say what the
rule still misses: a bare `git push -f` while *on* staging names no branch —
that needs `given.repo.branch_rx` (gate-and-given.md).

(`when` / `do` / `why` / `when_not` ride along in the body so the same file
is what step 5 files; the verifier and the replay test the pattern and do not
read them.)

Other shapes take other case syntax — a `read` rule (`read:<path>`,
`bash:<command>`, `--file-lines`), an `edit` rule (`path::content`), a `given`
block (`--branch`, `--diff-path`, `--user-said`, `--cases`), an `ordering`
rule (steps joined by ` >> `, armed by `session` / `prompt:` / `edit:`), an
`event: "output"` rule (`command::output`), an `event: "prompt"` matcher. For any of those, Read `${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/verify-recipes.md` for the exact form.

It exits non-zero until every case behaves. **Do not file a rule while it
exits non-zero, and show the table to the user.** What each line means:

- **LOAD** — whether the hook would load the rule at all. A pattern over 2000
  characters, one that does not compile, or one that backtracks is dropped
  *silently* on every teammate's machine: the rule exists, is active, and
  never fires. This line is the only warning you get.
- **FIRES** — your `--fires` cases. At least one is required; without it
  nothing has shown the rule can trigger.
- **SILENT** — your `--silent` cases, plus two generated for you: `grep` and
  `python -c` quoting the rule's own trigger. They are generated only when
  `command_rx` reduces to a plain literal of at least 4 characters; anything
  else gets none, and the verifier says so — then write those two yourself.
  Add one more yourself: the trigger inside a quoted argument
  (`--allowedTools 'Bash(git push:*)'`, `echo "…"`, a commit message) — after
  `grep`, the largest false-fire class we have measured. If any of these fire,
  add a `command_not_rx`.

**Always give at least one `--fires` case in CHAINED form** — `--fires 'cd .. &&
<your trigger>'` alongside your plain case. A rule whose chained form does not
fire is not ready to file, and the anchor that broke it (step 3) is invisible
in a table that only ever tested the bare command.

**Always give at least one `--silent` case for the complied-with form** — in
the example, the force-push to a feature branch. A rule that still fires after
someone complied cannot tell a violation from a fix, and people learn to
ignore it. If no such case passes, the rule is not a pattern — make it
`anchor_recall` or a `session_context` note.

### 4b. Prove it fires in a LIVE session — mandatory

`rulebook_verify.py` proves the pattern matches the strings you thought of; it
cannot prove the rule fires in a real session. **The rule is not filed until
this step has produced a fire** — if the user asks to skip it, tell them what
it would have proven and run it. Before starting, Read `${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/live-test.md` and follow it in
full: a sub-agent in its own `isolation: "worktree"` checkout runs a fake
feature against the candidate, armed in a private book claimed for that
worktree — never in the shared book every other session reads.

If the step cannot be run at all (no git checkout, no isolated worktree, no
Agent tool), live-test.md §4b.6 says what to do — ask before filing.

### 5. Conflict check, confirm, then file

Before showing the rule, check it against the book. The server refuses a
rule whose title or trigger equals a live rule's in the same book (`This
rulebook already has a rule with the same title|trigger: …`) unless you name
that rule in `supersedes_rule_id`; it does not catch a near-identical
statement, a twin in another book, or a dismissed one. Catch all of them here,
before the person approves. Save step 2's `list_rules` reply (every page) to
a file and run, with the step-4 candidate:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
  --candidates <candidate.json> --existing <list_rules.json> --repo "<repo>" \
  --rulebook-id <scopes.<scope>.rulebook_id from step 0>
```

For a workspace, the id is that workspace's `rulebook_id`. When it is `null`
(nothing has been filed into that scope yet), pass `--new-scope` instead of
`--rulebook-id`: every hit is then in another scope, which is right — there
is nothing in this one yet.

The human summary is the **last** lines of the output, after the JSON.
`same_title` / `same_matcher` (an **active** rule fires on the same call) /
`anchors_overlap` are deterministic — and "no deterministic hit" is not "no
conflict": the example's real twin (the rule it replaces, with a different
pattern) shows up only in the `judge_by_statement` list. Read that list
it prints and mark the candidate `duplicate`, `contradicts` or `distinct`
against each (the script prints the exact `supersedes_rule_id` value under
each hit). `duplicate` (or a `same_title` / `same_matcher` hit you judge to
be the same rule) → file with `supersedes_rule_id: <that rule's rule_id>`;
the server files it as `proposed` and activation replaces exactly that rule.
`same_matcher` against an **active** rule that is NOT the same rule → do not
file; tell the user. `contradicts` → file it (it lands `proposed`) WITHOUT
`supersedes_rule_id`, but name the rule it fights in the report; a reviewer
retires one side before activating the other.

A hit marked **`cross_book`** is in another scope than the one you are
filing into. `supersedes_rule_id` cannot reach it — whatever you file, both
rules stay live and anyone both scopes reach gets both on the same call. Do
not file over it silently: name the scope (its label) and the rule to the user
and let them choose (retire one side in MemHub, narrow one rule's
`scope_repos` / `scope_paths`, or file anyway and accept the double fire).

Show the user: who it applies to (step 0), the rule sentence, the delivery + engine block, `Categories: <list>`,
the sample commands it does and doesn't match, and the conflict verdict. Under the
sentence, show what the judge will read, as the person will approve it:

```
When:      <when>
Do:        <do>
Why:       <why, or "none given">
Not when:  <each when_not entry, or "nothing named">
```

Read it against the statement before you show it: a situation, an exemption
or a reason that appears in one and not the other is a drafting error — fix
it, do not file both versions.

On approval — or immediately, for a harness draft — call the memhub
**`create_rule`** tool with `title`, `statement`, `when`, `do`, `why`
(left out only when step 1 found none), `when_not` when an exclusion was
named, `delivery`, the engine
block, `scope_repos`, `source_ref` (e.g. `<path/to/CLAUDE.md>@<sha>#<heading>` or
`user correction, session <id>`), `categories` and `evidence` as labels.md
says (a harness draft sends neither),
`supersedes_rule_id` when it replaces a
rule, `mode: "gate"` if the user asked for a rule that stops the command, and
`scope` from step 0 (plus `workspace_id` for a workspace) — never a guessed
`rulebook_id`; only a `--rulebook <id>` the user passed goes through as one.
A replacement (`supersedes_rule_id`) stays in the scope of the rule it
replaces, so send it without `scope`. No `author` when a person asked for the rule
(`nomination` included): they wrote it, and an unset author reads as the
owner — only a harness draft passes `author="xtrace"`. Read the reply:

- `unchanged: true` → the rule is already in the book (a retried call);
  nothing new is filed, though differing `when` / `do` / `why` / `when_not`
  and `evidence` are written onto the existing row. On a retry, reuse the
  first filing's `source_ref` byte for byte (only an `@<sha>` and a `#…` tail
  are ignored), or name its `rule_id` in `supersedes_rule_id`.
- **Refused as a duplicate** (`This rulebook already has a rule with the same
  title|trigger: "<title>" (rule_id <id>, <status>)`) → nothing was written.
  If it is the same rule, file again with `supersedes_rule_id: <id>`; if it is
  a different rule, retitle it (same title) or tell the user (same trigger).
- `status: "proposed"` + `supersedes_rule_id` → filed as a replacement for
  the rule you named; it retires that rule when a reviewer activates it.
- `status: "proposed"` with no `supersedes_rule_id` → new, awaiting review.
- `category_invalid` / `evidence_invalid` → labels.md, *Refused*.
- A scope refusal (`This organisation's plan doesn't include workspaces…`,
  `That workspace is locked…`, `No workspace with that id…`, `That's the
  organisation's main workspace…`) → nothing was written. Tell the user in
  one line and file again with `scope: "org"`. `A replacement stays in the
  same scope…` → file it again without `scope`. `A personal rulebook only
  takes its owner's own rules…` → the rule you meant to replace is someone
  else's personal rule: tell the user, and file it only as a new rule (no
  `supersedes_rule_id`) if they want one.

**A rule that replaces another inherits what it does not name.** With
`supersedes_rule_id`, any of `when` / `when_not` / `do` / `why` you leave out
is copied from the rule being replaced — which is what you want for a
pattern-only fix, and wrong the moment the situation itself changed. If the
new rule is for a wider, narrower or different situation, re-state `when` —
and `when_not`, as the whole list it should now be — rather than inheriting
the old one. A blank or omitted field never clears a stored one, so a
replacement cannot remove a `when_not` entry by leaving it out — re-state the
list without it.

The server never answers `draft` — a call without `activate` lands
`proposed`. Relay the reply's `message` to the user: it is the server's own
sentence for what just happened.

**New rules always land for review — never pass `activate`.** That holds even
for a `personal` rule, which the user may activate themselves and the server
would let them arm: the point of the review step is that somebody reads the rule after
the excitement of writing it.

**A gate blocks nothing until it is activated.** Read `mode` back off the
reply and report it; never tell the user their command is blocked from now
on. If the server refuses the mode, file it advising and say which shape
would block.

If the rule is better as a plain suggestion than a check — the user doesn't
want to write a detector — file it the same way with
`source="nomination"`, `delivery: "session_context"` and no engine block (or
`anchor_recall` with its `anchors`); it lands as `proposed` for a reviewer.
Not `agent_hook`: with no `matcher` or `ordering` the server refuses it as
`This rule says nothing about how it is checked`.

### 6. Report

Name the audience from the reply — `label`, `applies_to` and `scope` — never a
rulebook's stored name. For the example: *"Filed `proposed` for Everyone in
Acme (everyone in Acme (12 people)), replacing 'No force-push to staging or
main'. It takes effect when an org admin activates it."* That is: who the rule
applies to (the set of people it will reach); that it is filed `proposed` and
awaiting review — naming the rule it replaces by title when it supersedes one;
and what happens next: **an org admin** activates it (**you**, when `scope` is
`personal`: "It takes effect when you activate it."), everyone it applies to
picks it up on their next session, and its firing history accrues in MemHub
as the evidence that later decides whether to keep, narrow, or retire it.
Relay `message` verbatim as well — it already ends with who the rule applies
to. Name any `cross_book` collision here too, under **Conflicts to resolve**.

For a gate, add: once activated it stops that command for everyone it applies
to, and each of them can still run it with `RULEBOOK_OVERRIDE='<why>'` in
front (an edit gate takes a `rulebook-override[<rule>]: <why>` marker in the
content instead).
