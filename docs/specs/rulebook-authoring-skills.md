# Rulebook authoring skills — the client half of the authoring contract

**Domain** `AUTHORING-SKILLS` (Rulebook Reconciliation, wave 2) ·
**Repo** `XTraceAI/agent-plugins` · **Paired with** `AUTHORING-API`
(MemHub-Backend) on contracts **C1** and **C2**.

**Files owned by this spec**

| Path | Why |
|---|---|
| `plugins/memhub/skills/create-rule/SKILL.md` | one rule, drafted from a sentence |
| `plugins/memhub/skills/import-claude-md/SKILL.md` | many rules, decomposed from a document |
| `plugins/memhub/scripts/rulebook_conflicts.py` | the pre-filing conflict check both skills run |
| `tests/rulebook_skills_test.py` | the drift gate (new) |

`plugins/memhub/scripts/rulebook_hook.py` is **not** owned here — it belongs to
the `HOOK` domain. Neither is anything in MemHub-Backend.

---

## 1. Why this exists

Both skills are broken against the server they call.

The Rulebook Reconciliation plan was written to catch exactly one failure
mode: *two halves of one contract, drifting*. Since the plan's baseline
(`agent-plugins` v0.28.0, `MemHub-Backend` `ca4b3b11`) the server half moved
three times — #1103 dropped the backtest, #1102 replaced title identity with
`supersedes_rule_id`, #1117 collapsed the two authoring tools into one and made
consent a checked argument — and the client half moved twice, on a different
schedule. They no longer meet.

The concrete breakages, verified against `MemHub-Backend@origin/staging`
(`623d6067`) and `agent-plugins@origin/main` (`019cc51` / v0.29.4):

| # | What the skills do | What the server does | Effect |
|---|---|---|---|
| 1 | `create-rule` step 4 calls **`nominate_rule`**; both skills list it in `allowed-tools` | The tool does not exist — #1117 removed it | The "no detector" path is a hard failure |
| 2 | Both report **`status: "draft"`** as the new-rule outcome | `draft` was deleted from `RuleStatus`; a write lands `active` or `proposed` | The skills describe a status that can never come back, and the report is wrong every time |
| 3 | Neither passes **`activate`** | `activate=True` + org admin + `source="authored"` ⇒ `active` | The one path that puts a rule in the book is unreachable from the terminal |
| 4 | Neither reads `brain_id` / `rulebook` / `org` off the reply | With no `org_id` the destination comes from the caller's **default org** | An admin can arm a rule in the wrong team's rulebook and read a reply that never says so |
| 5 | Neither handles the two supersede refusals | Both refuse with a sentence, and write nothing | A lost race reads as an opaque error; the rule is silently not filed |
| 6 | `import-claude-md` teaches `event: … \| write \| …` | `MATCHER_EVENTS = ("bash", "edit", "output")`; `write` is a legacy alias folded to `edit` | Teaches a vocabulary the server rewrites underneath the author |
| 7 | Neither states a `statement` budget | The hook truncates delivered text at `_TEXT_MAX = 400` | Everything past 400 chars is silently cut before a teammate sees it |
| 8 | Both cite "at most 15 [session rules] per repo scope" | `MAX_POSTURE = 15` is removed by the `HOOK` domain (decision 17) | A number about to stop being true |
| 9 | `create-rule` suggests `source_ref: "user correction, session <id>"` | A `source_ref` *drops owner-scoping* on the re-import key | A per-session string is worse than none: it never matches itself, and it widens the key across teammates |
| 10 | `rulebook_conflicts.py` treats only `deprecated`/`superseded` as retired | `dismissed` is a sixth, terminal state | A declined nomination surfaces as a live title conflict forever |

Note what is **not** on this list. The plan's headline bug — both skills
teaching `event: "result"` / `result_rx` — was fixed in #115 and is not present
on `main`. This spec must not "re-fix" it; it must **pin** it so it cannot come
back.

---

## 2. Decisions of record

Settled with the owner before implementation. Do not relitigate.

1. **Base is `main`.** `origin/staging` was deleted; every PR since #115 merged
   into `main`. Branch `feat/rulebook-authoring-skills`, PR base `main`.
2. **The backtest is dead and is not resurrected.** Column, argument and gate
   were all removed (#1103). Waves §7 decision 3 is superseded by backend spec
   v2.6. Passing `backtest` is an unknown-argument error.
3. **One authoring tool.** The C2 gate is closed by #1117 having landed: there
   is no `nominate_rule`. A nomination is `create_rule(source="nomination")`,
   which needs only `viewer` on the rulebook.
4. **Never predict the landing status.** The skill does not resolve the
   caller's role, ever. It calls the tool and reports `status` / `message` from
   the reply. One source of truth.
5. **Consent is a three-way confirm, and `activate` is only ever set from an
   explicit pick in that turn.** Decision 5 of the waves doc (consent as prose)
   is reversed by #1117: consent is a checked bit now.
6. **Teach the matcher vocabulary the hook can actually deliver** — which is
   narrower than the server's allowlist. Say `write` folds into `edit`. Omit
   `converted_rx` (outcome instrumentation, not a trigger).
   **Amended 2026-08-28 by /bug-hunt.** This decision originally read "teach the
   full allowlist, including `min_chars`", which was read off `validation.py`
   alone. That is wrong in the one direction that matters: the server accepts an
   `edit` matcher carrying only `content_rx` or only `min_chars`, but
   `rulebook_hook.evaluate` reads `rule["path_rx"]` unconditionally on the edit
   lane (`rulebook_hook.py:143`, inside a `try/except → False`), and `min_chars`
   is consulted only by the `write_stdlib` lane that `to_hook_rule` can never
   produce from a server row. Such a rule files clean, reports itself `active`,
   and never fires — the exact silent-death this spec exists to prevent. So:
   **`path_rx` is required on every `edit` matcher**, and `min_chars` is not
   taught at all.
7. **`statement` ≤ 400 characters, with the reason why.** Corrected: this is a
   **hard server refusal**, not a silent trim — `validation.MAX_STATEMENT` is
   400 on `origin/staging` (decision 12 landed), checked on the stripped
   statement. The hook's `_TEXT_MAX` truncation at the same number is therefore
   unreachable for anything the server accepted.
8. **Drop the session-rule number, keep the pressure.** No "15"; keep the
   argument that in-flight advice is acted on far more than session-start
   advice.
9. **Omit `source_ref` on hand-authored rules.** `import-claude-md` keeps
   `path@sha#heading` — that one is a real document anchor and is required for
   `source="claude_md_import"`.

---

## 3. The frozen contract, as landed

This section is the client's copy of **C1** and **C2**. Every statement here is
read off `MemHub-Backend@623d6067`; `tests/rulebook_skills_test.py` pins the
skills to it.

### 3.1 The tool surface — `create_rule`

```
create_rule(
  title, statement, delivery,              # required, every source
  matcher=None, ordering=None, anchors=None,
  scope_repos=None, scope_paths=None, scope_exclude_paths=None,
  source_ref=None, source="authored",
  supersedes_rule_id=None,
  activate=False,
  agent_brain_id=None, org_id=None,
) -> dict
```

`title` and `delivery` are **required for every source, nominations included**.
The server no longer derives a title from the statement — turning the user's
words into a well-formed rule is the skill's job.

`source` ∈ `authored` | `claude_md_import` | `nomination`.
`claude_md_import` requires a `source_ref`.

### 3.2 Landing status — computed server-side

```
activate=True  AND org admin  AND source="authored"   →  "active"
anything else                                          →  "proposed"
```

`claude_md_import` and `nomination` **never** land `active`, whatever
`activate` says — a document is decomposed by a model, not written sentence by
sentence, and a nomination is for review by definition. A non-admin passing
`activate` is **not an error**: the rule lands `proposed` and `message`
explains. This is why the skill must never try to work out the caller's role
before calling.

### 3.3 The reply

```
{brain_id, rulebook, org, rule_id, title,
 status: "active" | "proposed", mode: "advise", activated,
 supersedes_rule_id, superseded_rule_ids, message}
```
…or the same keys plus `unchanged: true`. `message` is written for the user;
report it as it stands. `rulebook` and `org` name the destination — always
report them, because with no `org_id` the destination is the caller's default
org.

### 3.4 Two refusals to handle

**Corrected 2026-08-28 by the exit test.** The earlier draft of this section
had the skills matching on the `reason` code. They cannot: `mcp_server.py`
collapses `AppException` → `_ToolInputError(exc.msg)` → `ValueError(str(exc))`,
so an MCP caller receives the **message text only** and the `data.reason` never
crosses the wire. A skill keyed on the code would never recognise either
refusal. Recognise them by their sentence; the code is the underlying identity,
not the signal.

| Reason (server-side) | Sentence the caller actually sees | What the skill does |
|---|---|---|
| `supersedes_unknown` | "The rule this one replaces isn't a live rule in this rulebook — check supersedes_rule_id." | Re-read `list_rules`, re-target, call again |
| `target_already_replaced` | "The rule this one replaces has already been replaced by someone else's. …" | Re-read `list_rules`, re-target the rule that is in the book now, call again — or file without `supersedes_rule_id` |

Both leave the book untouched, so a retry is safe.

### 3.5 Matcher vocabulary (C1) — exact

The server rejects unknown events **and** unknown keys. This is exact, not
indicative.

- **Events:** `bash` · `edit` · `output`. **`result` is not an event.**
  `write` is accepted as a legacy alias and stored as `edit` — author `edit`.
- **Key allowlist:** `event`, `command_rx`, `command_not_rx`, `content_rx`,
  `content_not_rx`, `path_rx`, `path_not_rx`, `match_heredoc_body`, `body_rx`,
  `warn_once_per`, `converted_rx`, `min_chars`. **`result_rx` is not a key.**
- **Required by event:** `bash` needs `command_rx` · `output` needs
  `content_rx` · `edit` needs one of `path_rx` / `content_rx` / `min_chars` ·
  `match_heredoc_body` needs `body_rx`.
- **`warn_once_per`:** `session` · `turn` · `call` · `branch` · `counter:N`.
  `file` is accepted but the hook maps it to `session` (`_SCOPE_MAP`), so it is
  **not offered** as a distinct choice.
- **Ordering keys:** `required_command_rx`, `gated_command_rx`,
  `armed_by_events`, `min_edits`, `display_name`, `warn_once_per`.
  `required_command_rx` and `gated_command_rx` are both required;
  `armed_by_events` may only contain `edit`.
- **Regexes** are Python `re`. Server cap 2000 chars; the hook drops any
  pattern over **400**, so 400 is the real limit. A quantified group that is
  itself quantified (`(a+)+`) is refused at authoring time by the server.
- **The hook's pattern lint is strictly stricter than the server's**, and it
  DROPS what it rejects instead of refusing the write. `rulebook_hook._RX_NESTED`
  is `\([^()]*[+*|][^()]*\)\s*[+*{]|\(\.\*\)|(\.\*){2,}`, so a quantified
  alternation group (`(-f|--force)*`), a capturing `(.*)`, and a repeated `.*`
  all pass `validation._NESTED_QUANTIFIER` and are then silently discarded
  client-side. Measured, all three: server accepts, `rx_ok` returns False. The
  skills must warn about all three.
- **`warn_once_per` on an `ordering` block is inert.** `ORDERING_KEYS` accepts
  it and the server stores it, but `to_hook_rule` returns at the ordering branch
  before the `fire_scope` translation and the dispatch loop `continue`s before
  reading it — every ordering rule dedups per worktree+branch regardless. Not
  offered.

---

## 4. What changes, file by file

### 4.1 `plugins/memhub/skills/create-rule/SKILL.md`

**Frontmatter**
- Drop `mcp__plugin_memhub_memhub__nominate_rule` and
  `mcp__plugin_memhub-staging_memhub__nominate_rule` from `allowed-tools`.
- Rewrite `description`: it currently promises "files it as a draft… a reviewer
  activates it". Both halves are wrong. The description is what the model reads
  to decide whether to invoke the skill, so it states the real outcome: files
  through `create_rule`, always advisory, landing status decided by the server
  and reported back.

**Step 3 — the delivery table**
- Add `min_chars` to the `edit` row; note `write` folds into `edit`.
- `session_context` row: drop "at most 15 such rules per repo scope"; keep the
  in-flight-vs-session-start argument.
- Add the `statement` budget (≤400, and why) to the per-rule field list.
- Extend the `warn_once_per` guidance to the full vocabulary minus `file`.

**Step 4 — conflict check, confirm, file**
- Keep the conflict check as-is (it is the pre-filing rigor that replaced the
  backtest).
- Replace the free-form "on approval" with the **three-way confirm** (§5).
- The `create_rule` call: drop the `source_ref` suggestion; add `activate` set
  only from choice 2 of the confirm turn.
- Replace the reply table: `active` / `proposed` / `unchanged`, plus the two
  refusals.
- Delete the `nominate_rule` paragraph; replace with
  `create_rule(source="nomination")` for the no-detector case — noting it needs
  only viewer access and never lands active.

**Step 5 — report**
- Report `message` verbatim, name the `rulebook` and `org` it landed in, and
  name what it retired (`superseded_rule_ids`) rather than what it *aimed* to
  retire.

### 4.2 `plugins/memhub/skills/import-claude-md/SKILL.md`

Same vocabulary corrections, plus:
- **No `activate`, ever** — say so explicitly and say why (§3.2), so nobody
  adds it later as a convenience.
- Frontmatter: drop `nominate_rule`; fix the `draft` language in `description`.
- Step 2 table: `event: bash | edit | output` (no `write`); add `min_chars`.
- Step 4: reply table becomes `proposed` / `unchanged` (an import has no third
  outcome) plus the two refusals.
- Step 5: the follow-up is `list_rules` status **`proposed`** — not
  `draft / proposed`.
- Keep `source_ref = path@sha#heading`, and keep the "stable across runs"
  reasoning — with a `source_ref` the key is a real document anchor and two
  teammates importing the same file converge on one rule instead of filing
  twins.

### 4.3 `plugins/memhub/scripts/rulebook_conflicts.py`

- `RETIRED = ("deprecated", "superseded", "dismissed")` — a dismissed
  nomination is terminal and was never a rule the team had, so it must not
  surface as a live title conflict or land in `judge_by_statement`.
- Refresh the module docstring: it describes the pre-#1102 identity key
  ("(rulebook, source_ref path, title)") and calls the collision outcome "a
  second draft". Both are stale.
- `tests/rulebook_conflicts_test.py` gets the `dismissed` coverage, because its
  fixtures already exist there — a second copy of them in the new file would be
  the duplication this spec is otherwise trying to remove. Its `EXISTING`
  fixture also still carries a `status: "draft"` row and asserts on
  `"no-force-push [draft]"` in the CLI summary; both move to `proposed`.

### 4.4 `tests/rulebook_skills_test.py` (new)

The drift gate — the thing that would have caught `result`/`output` on day one,
and would have caught `nominate_rule` the day #1117 merged. House style:
stdlib, no pytest, `check(label, condition)`, non-zero exit on failure, run by
`tests/run_all.py` via the `*_test.py` glob.

It pins the two SKILL.md files against §3, as constants with the contract cited
in comments:

- **Forbidden strings** — in either skill, at all: `result_rx`, `event: "result"`,
  `nominate_rule`, `"draft"` as a status, `backtest`.
- **Required vocabulary** — every event in `MATCHER_EVENTS`; every key in
  `MATCHER_KEYS` except `converted_rx`; the four required-by-event rules; the
  `warn_once_per` values minus `file`; all six ordering keys.
- **`warn_once_per: "file"` is never offered.**
- **The reply keys** the skills promise to read: `brain_id`, `rulebook`, `org`,
  `status`, `activated`, `superseded_rule_ids`, `unchanged`, `message`.
- **Both refusal reason codes** appear in both skills.
- **`activate`** appears in `create-rule` and is explicitly forbidden in
  `import-claude-md`.
- **The 400-character budget** is stated in both.
- **`allowed-tools`** lists `create_rule` + `list_rules` for both the prod and
  staging MCP server names, and nothing that no longer exists.
- **No `MAX_POSTURE` number** ("15") in either skill.

Two of these need care, because a skill has to be able to say "never write
`result_rx`" without the test reading that as teaching it. `nominate_rule`,
`` `draft` ``, `backtest` and the `MAX_POSTURE` number are banned outright.
`result_rx`, `event: "result"` and `warn_once_per: "file"` are banned *unless*
the text disowns them — naming the reflex shape as wrong is worth more than
never writing it down.

**The waiver is scoped to the SENTENCE, not the paragraph** (corrected
2026-08-28 by /bug-hunt). A paragraph-wide waiver was demonstrably worse than
none: one "there is no …" sentence licensed every banned token beside it, so
appending `use event: "result" with result_rx` to the very paragraph that
disowns it still passed. The check now requires a negation within 60 characters
before the token, in the same sentence.

**Two more assertions were vacuous** and are fixed the same way: the budget
check tested `"400" in text`, which is satisfied by `4000` (the cap this one
replaced), so it now pins the phrase `"400 characters"` and rejects the stale
numbers; and reply keys were matched as bare substrings, so `org` passed on
"org admin" and `activate` on "activates it" — they are now matched as
backticked code tokens (`` `key` `` or `` `key: value` ``).

The gate is mutation-tested: eleven separate regressions — each one a real
prior failure mode of these files — were injected one at a time, and all eleven
fail the suite.

The `find_conflicts` unit check for `dismissed` lives in
`tests/rulebook_conflicts_test.py` (§4.3), not here: this file is purely the
prose drift gate.

---

## 5. The confirmation turn — exact shape

The only human checkpoint on the path that writes org-wide rules. Both skills
carry it; only `create-rule` offers option 2.

```
Filing this rule.

  • Rulebook: <rulebook> (org <org>)
  • Scope: <repo | all repos>
  • Replaces: <title> | nothing

How should it land?
  1. File for review — someone activates it later
  2. Put it in the book NOW — live for every teammate from their next
     session, no second reviewer. Org admins only; if you're not one it
     lands in review and the reply says so.
  3. Edit it first / cancel
```

Binding rules stated in the skill text:
- **No default.** The skill never picks 2 on its own initiative.
- **Never file in the background, inside a loop, or as a side effect of another
  skill.**
- `activate: true` is set **only** when the user picked 2 in this turn.
- For `import-claude-md` the turn is per-run over the table, and option 2 does
  not exist.

---

## 6. Verification

1. `python3 tests/run_all.py` green, and
   `uv run --with 'mcp<2' python tests/run_all.py` green.
2. **Version parity** — bump `plugins/memhub/.claude-plugin/plugin.json` and
   `plugins/memhub-staging/.claude-plugin/plugin.json` to the same new version
   in the same commit (`tests/version_parity_test.py`). Target **0.29.5**;
   0.30.0 is claimed by open PR #120.
3. **Wave-2 exit test (C1/C2)** — against staging only, with
   `MEMHUB_MCP_BASE_URL=https://api.staging.memhub.xtrace.ai` set explicitly
   (the staging plugin's symlinked `scripts/` otherwise resolves to prod).
   Into a scratch rulebook brain, file one rule of each shape and confirm each
   is **accepted** and lands at the expected status:

   | Shape | delivery | engine block | expected |
   |---|---|---|---|
   | bash | `agent_hook` | `matcher {event: bash, command_rx}` | `proposed` |
   | edit | `agent_hook` | `matcher {event: edit, path_rx}` | `proposed` |
   | **output** | `agent_hook` | `matcher {event: output, content_rx}` | `proposed` |
   | ordering | `agent_hook` | `ordering {required_command_rx, gated_command_rx}` | `proposed` |
   | anchor | `anchor_recall` | `anchors[]` | `proposed` |
   | nomination | `session_context` | none, `source="nomination"` | `proposed` |
   | arm | `agent_hook` | any, `activate=true` | `active` + `activated: true` |

   **Run 2026-08-28 against staging — all seven accepted**, into the scratch
   rulebook `0ba60433-0f09-423a-b23c-df726feefffc`. Two further round trips
   confirmed the rest of §3.3: a re-file naming `supersedes_rule_id` with
   `activate` came back `active` with `superseded_rule_ids` populated and
   "Live now, replacing the earlier version…", and an identical re-file came
   back `unchanged: true`. Pointing at the now-retired predecessor produced the
   `supersedes_unknown` refusal — which is what exposed §3.4's error.

   `scripts/purge_today.py` does **not** cover rules (it deletes facts,
   episodes and artifacts; there is no `delete_rule` tool). Clean up through
   the lifecycle instead: `PATCH /v1/team/rulebook/rules/{id}` to `dismissed`
   from `proposed`, `deprecated` from `active`. All eight rows were left
   terminal. **Never against `api.memhub.xtrace.ai`.**
4. **README** — the `/memhub:create-rule` and `/memhub:import-claude-md`
   bullets, plus the `plugin.json` / `marketplace.json` descriptions, wherever
   they describe the old draft/reviewer outcome.
5. `/bug-hunt` on the diff. **Run 2026-08-28**: nine findings confirmed against
   the real code and fixed (the `edit`/`path_rx` silent death, the paragraph-wide
   negation waiver, the `"400" in "4000"` budget check, the hook's stricter regex
   lint, an example pattern `[-f|--force]` that is not valid Python `re` at all,
   inert `warn_once_per` on ordering, the 400 mechanism, four vacuous gate
   assertions, and `import-claude-md` never relaying the server's `message`).

**Known gap, not this domain's to fix.** `scope_paths` / `scope_exclude_paths`
are stored by the server and serialized into the hook view, but `to_hook_rule`
copies only `scope_repos` and `scope_ok` has no path notion — so a rule scoped
to `src/**` currently fires on every edit in the repo. The skills advertise a
narrowing the hook does not perform. The fix belongs to `HOOK`
(`rulebook_hook.py`, §7); raised rather than absorbed, per the plan's rule on
frozen contracts.

## 7. Out of scope

`rulebook_hook.py` and `MAX_POSTURE` (`HOOK`) · every backend change
(`AUTHORING-API`) · the Rulebook screen (`RULEBOOK-UI`, `FIRE-UI`) · the `gate`
tier (decision 1) · `machine_check` delivery (Phase 2) · resurrecting the
backtest (decision 2 above).
