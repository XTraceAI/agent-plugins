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
  for elsewhere is already held by the org admin who reviews it. Concretely, on
  this path:
  - **Step 0 (who it applies to)** does not ask and does not choose: a
    harness draft always files with `scope: "org"` — everyone in the
    organisation, the default scope. It lands `proposed`, so it reaches
    nobody until an org admin reads it and activates it. Never pass a
    `rulebook_id` to `create_rule`. Call `list_rulebooks` once, only to read
    `scopes.org.rulebook_id` for step 5's conflict check (`--rulebook-id`
    with it; `--new-scope` when it is `null`; neither when the reply has no
    `scopes`) — without it every hit reads `WHICH RULEBOOK?` and the check
    cannot clear a supersede. The one exception is a replacement: with
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

  `scope_repos` says which repos the rule fires in; `scope` (always `org`
  here) says whose agents it reaches. They are independent.
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
  `scope: "org"`, `title`, `statement`, `when`, `do`, `why`, the one engine
  (`matcher`, `ordering` or `anchors`), `delivery`, `mode`, `scope_repos`,
  `source="session_draft"`, `source_ref`, `state` and `author="xtrace"` —
  plus `supersedes_rule_id` when step 2 found one (and then no `scope`), and `when_not` only when
  the turn itself named a situation the lesson does not cover. The tool also
  offers `categories` and `evidence`;
  both are for a person's or a backtest's judgement, and this path has
  neither, so a harness draft sends neither: a guessed category or a
  malformed evidence object is refused.
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
  shown as the person's own.

`mode: "gate"` still needs the user's own words asking for a block (step 3).
