---
description: Use when the user wants to create a team engineering rule for the Rulebook (e.g. "/memhub:create-rule", "add a rule that we never force-push", "make a rule for this mistake"). Pins a when-X-then-Y sentence, drafts a deterministic check, and files it through the memhub `create_rule` tool — always advisory. Where it lands is the server's call: an org admin who says so at the confirmation turn puts it in the book immediately; everyone else files it for review.
argument-hint: [--brain "<rulebook brain name>"] [the rule, in your own words]
allowed-tools: Bash, Read, AskUserQuestion, mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub-staging_memhub__create_rule
---

You are creating a **Rulebook rule**: a human-authored, team-owned rule stored
in MemHub, fetched by every teammate's coding agent once per session, and
measured on every fire. Rules are data, not prose in a doc — and a rule with a
loose check fires on innocent commands more often than not, so the check is
where the care goes.

There is no local rule file, and there is no second authoring tool. The write
path is the memhub **`create_rule`** tool for every kind of rule, nominations
included.

Arguments: `$ARGUMENTS`
- `--brain "<name>"` (optional) → the rulebook to write into (`agent_brain_id`
  on `create_rule`). Omit to use the repo's own rulebook.
- Remaining text = the rule in the user's words. If absent, ask for it — one
  sentence, ideally already conditional ("when X, do/never Y").

## The flow — every step is mandatory

### 1. Pin the rule sentence

Get to a **when-X-then-Y** sentence with a **why**. A conditional shape is what
makes a rule actionable; a bare observation is not a rule. If the user gave a
war story, extract the conditional from it and confirm your reading.

### 2. Duplicate check — by eye now, deterministically in step 4

Call the memhub `list_rules` tool for the target rulebook (every status) and
read the new rule against every title and statement. Same subject → plan to
replace the existing rule instead of adding a twin: note its `rule_id` for
`supersedes_rule_id` in step 4. The server does no title matching — you
decide what a rule replaces. Keep the `list_rules` reply: step 4 runs the
deterministic check over it.

### 3. Draft the rule — one delivery, one engine block

| the rule is… | `delivery` | engine block |
|---|---|---|
| a Bash command with a checkable form | `agent_hook` | `matcher: {event: "bash", command_rx, command_not_rx?, warn_once_per}` |
| an edit/write to certain paths or content | `agent_hook` | `matcher: {event: "edit", path_rx, path_not_rx?, content_rx?}` — `path_rx` is required; `content_rx` narrows it |
| a failing or noteworthy tool output | `agent_hook` | `matcher: {event: "output", content_rx, command_rx?, content_not_rx?}` |
| "run X after edits, before Y" | `agent_hook` | `ordering: {required_command_rx, gated_command_rx, armed_by_events?, min_edits?, display_name?}` |
| applies when a file / symbol / command is in play, but the form isn't checkable | `anchor_recall` | `anchors: [identifiers]` — the server decides relevance per call |
| worldview with no trigger at all | `session_context` | none — shown once at session start; prefer a checkable shape whenever one exists, because advice delivered in-flight is acted on far more often than advice shown at session start |

Plus on every rule: `title` (short, imperative; the server allows up to 200
chars but aim for under 60), `statement`, `scope_repos` (`["<repo>"]` or `[]`
for all), `scope_paths` / `scope_exclude_paths` (globs).

**The `statement` budget is 400 characters**, and it is a hard refusal — a
longer one is rejected outright, not trimmed. It carries the advisory line *and*
its reason ("… Why: …"), so the nuance a reviewer needs has to fit inside 400
characters, not after them. (400 is also all the hook would ever deliver.)

**The matcher vocabulary is closed.** The server rejects unknown events and
unknown keys outright, so a matcher is not a place to improvise:

- **Events:** `bash`, `edit`, `output`. There is no `result` event and no
  `result_rx` key — a rule about a *failing command* is
  `{event: "output", content_rx: ...}`. `write` is accepted as a legacy alias
  and stored as `edit`; author `edit` (a Write counts as an edit).
- **Keys:** `event`, `command_rx`, `command_not_rx`, `content_rx`,
  `content_not_rx`, `path_rx`, `path_not_rx`, `match_heredoc_body`, `body_rx`,
  `warn_once_per`. Anything else is refused.
- **Required by event:** `bash` needs `command_rx`; `output` needs
  `content_rx`; `edit` needs `path_rx`; `match_heredoc_body` needs `body_rx`.
  The server also accepts an `edit` rule carrying only `content_rx` or only
  `min_chars` — **do not write one.** The hook reads `path_rx` unconditionally
  on the edit lane, so such a rule loads, reports itself active, and never
  fires. Give every `edit` rule a `path_rx`, even a broad one (`.` matches
  every path), and narrow with `content_rx`.
- **Ordering** needs both `required_command_rx` and `gated_command_rx`;
  `armed_by_events` may only contain `edit`. `warn_once_per` is accepted here
  but the hook never reads it — an ordering rule dedups per worktree+branch.

**Matcher-authoring rules:**
- Bash rules match the **pre-heredoc segment only** by default — heredoc bodies
  are data (python source, commit messages) and are the main false-fire class.
  Set `match_heredoc_body: true` **together with** `body_rx` only if the rule
  targets what a heredoc says.
- Patterns must be **shape-specific**: match the violating *form*
  (`git\s+push\b[^|;&]*\s(?:-f|--force)\b`), never a keyword that also
  appears in innocent content.
- Every known-legitimate exemption goes in `command_not_rx` now, not after it
  fires. Give bash rules a `command_not_rx` that exempts commands which merely
  mention the pattern (`python -c`, `grep`).
- Keep every pattern under **400 characters** — the hook drops a longer one and
  the rule silently stops firing. A quantified group that is itself quantified
  (`(a+)+`) is refused outright by the server.
- The hook's own pattern lint is **stricter than the server's**, and it drops
  what it rejects rather than refusing the write — so these file clean, report
  active, and never fire. Avoid all three: a quantified alternation group
  (`(-f|--force)*` — use `(?:-f|--force)`), a capturing `(.*)`, and a repeated
  `.*` (`.*.*`).
- `warn_once_per` is `session` (the default — a rule that nags every call gets
  ignored), `turn`, `call`, `branch`, or `counter:N`. Use `turn` where each
  occurrence matters (e.g. force-push). Do **not** offer `file`: the hook maps
  it to `session`, so it is not a distinct choice.

Sanity-check every regex against two or three real commands from this repo's
history (`git log`, your own shell history) — one that should fire and one
that should not — before filing.

### 4. Conflict check

Before showing the rule, check it against the book — the server files a
colliding title or matcher as a silent second rule unless you name what it
replaces, so this is the only place it gets caught. Call `list_rules` (every
status), save the reply, write the candidate `create_rule` body to a file, and
run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" \
  --candidates <candidate.json> --existing <list_rules.json> --repo "<repo>"
```

`same_title` / `same_matcher` (an **active** rule fires on the same call) /
`anchors_overlap` are deterministic; then read the `judge_by_statement` list
it prints and mark the candidate `duplicate`, `contradicts` or `distinct`
against each (the script prints the exact `supersedes_rule_id` value under
each hit). `duplicate` (or a `same_title` / `same_matcher` hit you judge to
be the same rule) → file with `supersedes_rule_id: <that rule's rule_id>`.
`same_matcher` against an **active** rule that is NOT the same rule → do not
file; tell the user. `contradicts` → file WITHOUT `supersedes_rule_id`, but
name the rule it fights in the report; a reviewer retires one side before
activating the other.

### 5. Confirm — the one checkpoint

Show the rule sentence, the delivery + engine block, the sample commands it
does and doesn't match, and the conflict verdict. Then stop and ask, every
time:

```
Filing this rule.

  • Rulebook: <rulebook name> (org <org>)
  • Scope: <repo | all repos>
  • Replaces: <title of the rule it supersedes> | nothing

How should it land?
  1. File for review — someone activates it later
  2. Put it in the book NOW — live for every teammate from their next
     session, no second reviewer. Org admins only; if you're not one it
     lands in review and the reply says so.
  3. Edit it first / cancel
```

**There is no default.** Never pick 2 on your own initiative — it is the only
path that puts a rule in front of the whole team with nobody else reading it.
Never file in the background, inside a loop, or as a side effect of another
skill: filing a rule always costs the user a turn, on purpose.

Don't try to work out whether the user is an org admin first. You can't, and
you don't need to — a non-admin who picks 2 is not an error: the rule lands for
review and the reply says why.

### 6. File it

Call the memhub **`create_rule`** tool with `title`, `statement`, `delivery`,
the engine block, `scope_repos`, `supersedes_rule_id` when it replaces a rule,
`agent_brain_id` when `--brain` was given, and `activate: true` **only** if the
user picked 2. Omit `source_ref` — it is a document anchor for imported rules,
and a per-session string here would never match itself on a re-file.

If the rule is better as a plain suggestion than a check — the user doesn't
want to write a detector — file it as `source: "nomination"` with
`delivery: "session_context"`. It still needs a `title` and a `statement` you
write from what they said; it needs only viewer access to the rulebook, and it
always lands for review whatever the user picked.

**Read the reply. Never assume the outcome:**

| reply | what happened |
|---|---|
| `status: "active"`, `activated: true` | live for every teammate from their next session |
| `status: "proposed"` | filed for review; it fires once someone activates it |
| `unchanged: true` | identical content is already in the book; nothing was written |
| `superseded_rule_ids: [...]` | the rules this one actually retired, now |
| `supersedes_rule_id` on a `proposed` row | what it *will* retire when someone activates it |

The reply also carries `rulebook` and `org` — the destination. With no `org_id`
that comes from the user's default org, so a misroute is only visible if you
report it.

**Two refusals, both meaning nothing was written — so a retry is safe.** They
arrive as a tool error carrying only the sentence; the reason code behind each
(`supersedes_unknown`, `target_already_replaced`) is not on the wire, so
recognise them by their text:

- *"The rule this one replaces isn't a live rule in this rulebook — check
  supersedes_rule_id."* → `supersedes_unknown`. The target is retired,
  dismissed, or in another rulebook. Re-read `list_rules`, pick the right
  target, call again.
- *"The rule this one replaces has already been replaced by someone else's."* →
  `target_already_replaced`. Re-read `list_rules`, re-target the rule that is
  in the book now, and call again; or file without `supersedes_rule_id` and say
  so in the report.

### 7. Report

Give the user the reply's `message` as it stands, and name the `rulebook` and
`org` it landed in. If it went live, say so plainly and name what it retired
(from `superseded_rule_ids`) — not what it aimed to retire. If it is waiting
for review, say who can activate it: the rule's owner, a rulebook admin, or an
org admin, in MemHub. Then: every teammate's coding agent picks an active rule
up on their next session, and its firing history accrues in MemHub as the
evidence that later decides whether to keep, narrow, or retire it.
