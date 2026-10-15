<!-- create-rule, Harness-draft path. Read from SKILL.md; step numbers refer to it. -->

## Handed a turn by the harness

MemHub's harness judges each of the person's turns at the Stop after it, and on
a flag the agent launches a background fork of itself whose prompt begins
`MemHub harness fork, moment <session_id>#<turn>`. If that is you, you are here
because turn N was flagged, not because the user asked for a rule, and two
things differ:

**This file, not the fork prompt, is the harness path.** The prompt stays a
pointer; everything it would otherwise carry lives here.

**You hold turn N+1 too — read it first.** The person's next message is what
happened after the flagged turn. If it reversed, abandoned or replaced the
correction, there is no lesson in turn N: end with the `none` line.

**The invariant, before the details: nothing on this path reaches the person
except the fork's one final line.** Wherever a step says to show, ask, offer or
report, skip it; where it says to stop and ask, stop silently. The bullets
below say what to DO at each such point.

- **The test.** A lesson is one that would change what an agent DOES next time,
  is not already a RULE, is not project state, and will still be true next
  month. Already written in CLAUDE.md or the docs does NOT disqualify it: if
  this turn tripped over it anyway, the prose was not enough — file it and cite
  where it is written. Skip only when nothing went wrong and you would merely
  be restating the docs.
- **No lesson** → file nothing and end with the fork prompt's `none` line.
- **A lesson** → run this flow with it as the user's words, and **ask the
  person nothing at all**. A `session_draft` lands `proposed` and fires for
  nobody until a reviewer activates it, so every confirmation this skill asks
  for elsewhere is already held by whoever reviews it. Concretely, on this
  path:
  - **Step 0 (who it applies to)** does not ask: a harness draft files with
    `scope: "personal"` — the person whose turn it was, who reviews and
    activates it themselves. It goes to `org` only when step 0's rule 4
    holds: the lesson is right for anyone working in that repo or org, and
    you can say why in one sentence. That sentence then ends `why`
    (`Org-wide because …`), the only place the org admin who reviews it will
    read it — and `why` stays within its 400 characters, so shorten the
    incident, never drop the sentence. A lesson about this person's own setup, habits or taste is never
    `org`. Never pass a `rulebook_id` to `create_rule`. Call `list_rulebooks`
    once, only to read `scopes.<scope>.rulebook_id` for step 5's conflict
    check (`--rulebook-id` with it; `--new-scope` when it is `null`; neither
    when the reply has no `scopes`; for a replacement, the replaced rule's own
    `rulebook_id` from its `list_rules` row) — without it every hit reads
    `WHICH RULEBOOK?` and the check cannot clear a supersede. The one exception is a replacement: with
    `supersedes_rule_id`, leave `scope` out — a replacement stays in the
    scope of the rule it replaces.
  - **Step 4b never runs here** — the fork prompt treats its precondition as
    missing (4b.6). Do not ask and do not read `live-test.md`: file with the
    pattern proven by step 4's verifier alone.
  - **Step 5** does not ask. File, then end with the fork prompt's `filed`
    line naming the rule.
  - **A conflict that the mandatory policy says not to file, is not filed** —
    and on this path it is not reported either: `cross_book` (a rule in
    another scope, which `supersedes_rule_id` cannot reach), or `same_matcher` on an active rule
    that is not this one. File nothing, say nothing, stop.
  - **Never pass `activate`.** Never put a person's name, home directory or
    e-mail in a rule.
  - **`unchanged: true` is a filing, not a blocker.** That reply means the rule
    is already in the book — a retry after a lost response, or identical
    content re-filed. The moment ended WITH a rule, so tell the person as you
    would for any filing.
  - **A twin refusal is also already filed.** A `session_draft` whose
    statement is near-identical to a rule already `proposed` in the book is
    refused with `A proposed rule in this rulebook already says this: "<title>"
    (twin_rule_id <id>)…`. The lesson is in the review queue under that id —
    someone's earlier turn filed it. Do not re-file, do not supersede it, say
    nothing to the person, and end with the `none` line.
  - **A dismissed-twin refusal is a reviewer's "no".** The same draft is
    refused with `A reviewer already declined this lesson in this rulebook:
    "<title>" (twin_rule_id <id>)…` when its statement is near-identical to a
    rule someone dismissed. Nothing was written and nothing is waiting in
    review. Do not re-file, do not reword the statement to get past the
    check, do not name the dismissed rule as `supersedes_rule_id` (it cannot
    be superseded), say nothing to the person, and end with the `none` line.
    This is not a cannot-file: the server worked, so never the `failed` line.
  - **A draft refused as `stamped by plugin hook <v>; drafts need <min> or
    newer`** cannot be filed from this stamp — the hook that wrote it predates
    the draft contract. Treat it as cannot-file: file nothing and end with the
    `failed` line.

- **`scope_repos` is yours to choose, from what the lesson is about.** The
  fork prompt says where the turn WORKED; that is evidence, not the scope.
  Scoping to the session's repo is what bound a `$?`-after-a-pipe lesson to
  MemHub-Backend only, and scoped rules anchored on `harness_stop.py` constants
  to a repo where those names do not exist, so they could never fire. Choose:
  - **`[]` (every repo)** when the rule names nothing that lives in one repo —
    shell, git, the sandbox, a CLI's flags, how to read a tool's error.
  - **`["<repo>"]`** when the rule names a file, symbol, config key, table or
    command that lives in that repo. It may be a repo the turn never touched —
    a lesson learned in MemHub-Backend about the plugin belongs to the plugin's
    repo.
  - **Several** only when each one holds what the rule names. Never widen to
    `state.touched_repos` just because the turn visited them: those then fire
    on unrelated work.

  **Prove a repo scope before filing.** For each repo you name, find its
  checkout (`git remote get-url origin` basename, the name the hook matches)
  and grep it for the rule's anchors or the paths its trigger names. If none of
  them is there, the rule can never fire in that repo — re-scope it, and if no
  repo holds them, file nothing. No checkout of that repo on this machine →
  file it anyway.

  `scope_repos` says which repos the rule fires in; `scope` (`personal`
  unless step 0's rule 4 holds) says whose agents it reaches. They are
  independent: a lesson scoped to one repo is still `personal` unless anyone
  working there needs it.
- **`source_ref` is passed EXACTLY as the fork prompt gives it.** A draft's identity is its exact `source_ref`: a re-file under a
  different one is refused as a twin of the first filing (end with `none`).
- **The stamp** is what the fork prompt's read-only
  `harness_stop.py stamp --transcript … --turn N --cwd …` command prints. Run it
  once and pass its JSON verbatim as `state` in step 5 with
  `source="session_draft"`, that `source_ref` and `author="xtrace"` — MemHub
  refuses a `session_draft` without its `state`. The author is XTrace because
  the harness wrote this rule and no person asked for it; it is a label only,
  and the session's person still owns the rule.
- **The harness draft's `create_rule` call carries these and nothing else:**
  `scope` (step 0 above), `title`, `statement`, `when`, `do`, `why`, the one engine
  (`matcher`, `ordering` or `anchors`), `delivery`, `mode`, `scope_repos`,
  `source="session_draft"`, `source_ref`, `state`, `author="xtrace"` and
  `categories` — plus `supersedes_rule_id` when step 2 found one (and then no `scope`), and `when_not` only when
  the turn itself named a situation the lesson does not cover. **A harness
  draft is labelled when it is filed:** `categories` is the set step 1b chose
  from labels.md's six, by its definitions and the `agent_conduct` sorting
  test — never a seventh, never the single `category`. There is no preview to
  show it in; the reviewer changes it in MemHub. Leave it out only when none
  of the six fits (a replacement then sends `[]`, labels.md). `evidence` is
  never sent: it is a backtest's measurement and this path runs none.
- **`when`, `do` and `why` are written here without asking**, by step 1's
  rules, from turn N and the person's reaction to it: `when` is what the agent
  was doing when it went wrong, `do` is what the person's correction asked
  for, `why` is what went wrong in this turn. Step 5's preview of them is
  suppressed like every other; the reviewer reads them in MemHub. A draft
  filed without them is judged on its statement alone.
- **A refusal is fixed in place, never rebuilt.** When `create_rule` refuses a
  field (a category, a matcher key, a repo scope), retry the SAME call with only
  that field changed. `source`, `source_ref`, `state` and `author` stay as they
  were: a retry that drops them files a rule nobody can trace to this turn,
  shown as the person's own. A `category_invalid` refusal is re-filed once
  with `categories` dropped and nothing else changed — never a guessed set.

`mode: "gate"` still needs the user's own words asking for a block (step 3).


## Handed a cost verdict (repair)

A fork prompt that says `to REPAIR one team rule` is the live-repair path
(harness-self-improvement-spec §4): the effect judge read what the agent did
after rule R fired on turn N of this session and found that showing it COST
the work (`p_cost` at or above 0.8). Everything in "Handed a turn by the
harness" holds — nothing reaches the person but the final line, ask nothing,
file `proposed`, never activate — with these differences:

- **The subject is an existing rule, and the deliverable is a supersede.**
  Call `list_rules` with `include_retired=True, limit=200` (more pages on
  `has_more`) and find the rule by its `rule_id` from the prompt: read its
  `statement`, `when`, `when_not`, `do`, `why` and `trigger`. Read turn N of
  the transcript — the call the rule fired on, the hook text the agent saw,
  and what it did after. Then decide which ONE of these it is:
  - **A situation the rule is not for** (it fired on the right shape in the
    wrong moment): keep the pattern, add that situation to `when_not` in
    situation words, never trigger vocabulary. This is the common case.
  - **The wrong shape** (a mention in a grep, a heredoc body, a quoted
    argument): add a `command_not_rx` for that shape; the pattern stays broad
    otherwise (step 3's "the pattern stays broad; the judge decides fit").
  - **A `do` this environment refuses** (the rule sent the agent into an
    action the auto-mode classifier denies, a tool it lacks, a server it
    cannot reach): rewrite `do` with what to do instead, and say so in the
    statement.
  - **Nothing wrong** (the judge misread the window, the cost was the
    agent's own detour): file nothing and end with the `none` line, naming
    the reason in a few words.
- **Prove the repair the way a new rule is proved.** A pattern change goes
  through step 4's verifier with the fired-on call as a `--silent` case when
  the shape was wrong, and the rule's own `--fires` cases kept. A `when_not`
  or `do` change needs no verifier run (the pattern is untouched) but the
  statement must still say the same thing as the fields.
- **File with `supersedes_rule_id` set to R and no `scope`** (a replacement
  stays in the scope of the rule it replaces), `source="session_draft"`,
  the prompt's `source_ref`, the stamp as `state`, `author="xtrace"`, and
  every field of R you did not change copied as it was — a supersede that
  drops a field drops it from the live rule. `scope_repos` stays R's.
- **One open repair per rule.** The hook hands a rule over once per machine
  per fortnight; a second cost verdict on the same rule in that window
  attaches to the proposal in review and spawns nothing. If `create_rule`
  answers that a proposed supersede of R already exists (a twin), end with
  the `none` line: the repair is in the review queue.
- **End with exactly one line:** `MemHub harness fork: repaired <title>` on a
  filing, else the `none` or `failed` line as above.
