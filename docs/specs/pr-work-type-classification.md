---
title: "Spec: PR work-type classification from the coding agent"
type: spec
---

# Plugin Spec: the coding agent labels the pull request it worked on

**Repo:** `XTraceAI/agent-plugins`. **Ticket:** [ENG-1031](https://linear.app/xtrace/issue/ENG-1031/pr-tag-from-client-side-plugin).
**Backend:** [MemHub-Backend#1262](https://github.com/XTraceAI/MemHub-Backend/pull/1262), squash-merged
into `staging` as `cd0732ed` on 2026-09-11. **Companion:** this repo's
`docs/specs/pr-linking-plugin-spec.md` — the linking half, which this extends and does not replace.

**Status:** specified. **Must not merge before staging is promoted to production** (§7).

---

## 0. What changes

MemHub used to infer a pull request's kind from its title prefix, then its branch prefix, then
`other`. That inference is **deleted** — `session_pr_type.py` went from ~350 lines on `main` to 79
on `staging`, and its docstring now says "No title, branch, GitHub-label or Linear inference."
Nothing server-side fills the gap. If the coding agent does not say what kind of work a pull
request is, it has no kind at all.

So the agent says. It already decides whether to link itself to a pull request; this adds one
more decision to that same call — **one primary work type**, chosen from the session's own
context and the changes actually in the diff, never from the title.

**The whole feature is four extra arguments on calls the plugin already makes.** No new script, no
new hook, no new skill, no new manifest entry, and no new network path.

---

## 1. Verified ground truth

Everything below was read from source or executed, not taken from prose. Claims from the ENG-1031
handoff artifact that are NOT restated here were not independently confirmed and must not be
relied on.

### 1.1 The wire contract

`link_pr` (MCP) / `POST /v1/team/pr-links` (REST) take two new optional arguments:

```json
{"pr_url": "https://github.com/o/r/pull/7",
 "session_ids": ["<conversation-id>"],
 "link_source": "session_self",
 "pr_type": "fix",
 "classification_session_id": "<the same conversation-id>"}
```

Accepted `pr_type` values are **exactly** these seven lowercase tokens
(`app/services/session_pr_type.py:15-21`, plus a DB `CHECK` constraint):

`feat` `fix` `chore` `docs` `perf` `refactor` `other`

No normalization of any kind. `"Feat"` and `"feature"` are rejected. `mixed` and `none` are
read-side grouping keys and are never valid inputs.

### 1.2 Validation, in the order it fires

`validate_link_source` and `validate_session_ids` run first, so a malformed `link_source` or
`session_ids` beats every classification error. Then `validate_classification`
(`session_pr_link_write.py:182-204`), in this order:

1. both `pr_type` and `classification_session_id` absent → no classification, no error;
2. `pr_type` missing or not one of the seven → `pr_type_invalid` (400);
3. `classification_session_id` not present in `session_ids` → `classification_session_invalid` (400);
4. `link_source != "session_self"` → `classification_source_invalid` (400).

The two fields are a pair. Sending `pr_type` alone is **not** silently ignored — it fails at (3).

Then, inside `link()`: if the classification session does not resolve to one of the caller's own
sessions → `classification_session_not_found` (404), raised before any insert and before any
commit, so **nothing at all is written** (`:971-975`).

### 1.3 Classification survives `already_linked` — the property the skills depend on

In the per-session loop, `classification_ref` is assigned **before** the three-way branch that
decides insert / upgrade / already-linked (`session_pr_link_write.py:1059-1060`), and
`record_classification` fires on `classification_ref is not None` (`:1119-1123`). So a call naming
a session that is **already linked** reports `skipped: already_linked` *and still records the
type*. Observed response:

```json
{"pr": {"pr_number": 7, "pr_type": "perf", "pr_type_source": "agent_session"},
 "linked": [],
 "skipped": [{"session_id": "…", "reason": "already_linked"}]}
```

HTTP 200. The existing link row's `link_source` is **unchanged** — a row whose source is already in
`CONFIRMED_LINK_SOURCES` (`{"url", "session_self", "session_found", "manual"}`) is never rewritten
(`:1085`, and the upgrade `UPDATE` carries `link_source.notin_(...)` at `:1140-1142`). Backend
coverage: `tests/test_pr_classifications.py:116-124`.

`record_classification` persists only `(org_id, repo_id, pr_number, pr_type, workspace_id, conv_id,
classified_by, classified_at)` — **no `link_source` column exists on that table**.

### 1.4 A classification is permanent

There is **no UPDATE and no DELETE against `pr_classifications` anywhere in the backend**. Unlinking
does not clear it; a GitHub webhook refresh does not disturb it; no backfill or repair script
exists. The only way a classification disappears is `ON DELETE CASCADE` when the parent pull
request row is deleted. A second, different type is `pr_classification_conflict` (409) and the
first decision wins.

`record_classification` raises at `session_pr_link_write.py:1120` — **before** `_insert_links`
(`:1129`) and before `db.commit()` (`:1169`). So a 409 does not merely lose the type: it refuses
the link too. §3.4 is what recovers from that.

**This is the single most important constraint in this spec.** Every design decision below that
looks over-cautious is paying for it.

### 1.5 MCP loses the error code

`app/mcp_server.py:7005-7008` converts `PrLinkError` into a tool error carrying only
`exc.message`. **The `reason` code and the HTTP status do not survive.** Every call site in this
plugin goes through the MCP tool, so instructions must match on message text:

| Condition | Message the agent actually sees |
|---|---|
| bad type | `pr_type must be feat, fix, chore, docs, perf, refactor, or other.` |
| id not in `session_ids` | `classification_session_id must name one of session_ids.` |
| wrong `link_source` | `Agent classification requires link_source=session_self.` |
| capture not arrived | `The classification session was not found among your sessions.` |
| already typed | `This PR already has a different classification; linking cannot overwrite it.` |
| probe with type | `Classification requires session_ids; probe mode is read-only.` |

### 1.6 The whitespace trap

`validate_session_ids` **strips** each id (`:334`); `classification_session_id` is compared **raw**
(`:195`, `:1059`). `session_ids=["abc"]` with `classification_session_id=" abc "` fails with
`classification_session_invalid`. Both fields must carry the byte-identical string.

### 1.7 `check` already reports the type

`GET /v1/team/pr-links/check` returns `pr.pr_type` and `pr.pr_type_source` — verified live against
staging. `null`/`null` means unclassified. `"other"`/`"agent_session"` is an explicit agent
decision and is **not** the same thing. This is what lets the hook avoid provoking a 409 (§3.2).

---

## 2. Where the decision is made, and where it is not

The four existing call sites (`docs/specs/pr-linking-plugin-spec.md` §4.5, §8, §9). All four gain
classification; two need a second call because the backend refuses classification from their
`link_source`.

| Site | Today's `link_source` | Shape | Why |
|---|---|---|---|
| **B1** `pr_link.CREATED` — this call opened the PR | `session_self` | **one call**: link + type | The session has the diff, the branch and the PR body it just wrote in context. The best-informed moment there is. |
| **B2** `pr_link.IN_PLAY` — some other GitHub call named one PR | `session_self` | **one call**, only on the branch where the agent decides it wrote the code | Already conditional on authorship; classification rides that same judgment. |
| **`/memhub:link-pr`** | `manual` | **two calls** | Backend refuses classification from `manual`. Cursor's only path (§2.2). |
| **`/memhub:find-contributing-sessions`** | `session_found` | **two calls** | Backend refuses classification from `session_found`. |

**The hook decides nothing semantic.** `pr_link_trigger.py` detects the event and injects text; the
model picks the type. There is deliberately no title parser, no branch heuristic and no default —
the backend deleted exactly that inference, and reintroducing it client-side would re-create the
problem this ticket exists to solve.

### 2.1 The two-call shape

For the two skills:

```
1. link_pr(pr_url=…, session_ids=[…], link_source="manual" | "session_found")
2. link_pr(pr_url=…, session_ids=["<one id>"], link_source="session_self",
           pr_type="…", classification_session_id="<the same one id>")
```

Call 2 names exactly ONE session — the one whose work the type describes — and its expected,
**successful** outcome is `linked: []` with `skipped: [{reason: "already_linked"}]` plus
`pr.pr_type` set. A skill that reads that as a failure will mislead the user.

**Decision of record:** passing `session_self` from a flow that is not literally the session itself
is deliberate and was approved. It is safe because nothing false is persisted — the link row keeps
its original `manual` / `session_found` source (§1.3) and the classification table has no
`link_source` column (§1.3). The gate is satisfied, not subverted: the caller *is* naming its own
session, which is what the check is protecting. If the backend later accepts classification from
`manual` and `session_found`, call 2 collapses into call 1 and this section is deleted.

### 2.2 Per-host reach

Unchanged from the linking spec, and worth restating because it decides who can classify at all:

| Host | B1 / B2 | Classification path |
|---|---|---|
| Claude Code | hook fires | hook, one call |
| Codex (plugin hooks) | hook fires | hook, one call |
| Codex (compatibility bridge) | shell only, no GitHub MCP | hook for `gh`/`curl`; else `/memhub:link-pr` |
| Cursor | **hook never runs** | `/memhub:link-pr` only |

`pr_link_trigger.py` is never invoked on Cursor — `cursor_capture.py` spawns only the flusher.
`pr_link.HOSTS` still lists `"cursor"` and `--host cursor` is still documented; that is dead
capability and this change does not revive it. **Cursor's only route to a work type is the
two-call path in `/memhub:link-pr`**, which is most of why that site classifies at all.

---

## 3. `pr_link.py`

Stdlib only, no network at import, every public function pure except `check()`. Unchanged.

### 3.1 New module constants

```python
PR_TYPES = ("feat", "fix", "chore", "docs", "perf", "refactor", "other")
```

Exactly the backend's accepted set, lowercase, in the backend's own order. A test asserts the
tuple literally, so a future edit that adds a synonym fails here rather than at a user's 400.

`TYPE_MENU` — the one-line-per-type gloss the injected text carries:

```
feat — a new capability or behavior
fix — corrects existing faulty behavior
chore — maintenance, tooling, dependencies, tests, build/CI, style
docs — documentation changes
perf — performance improvements
refactor — structural change that preserves intended behavior
other — a deliberate choice when none of the above fit
```

These glosses are *plugin guidance*. The backend validates only the tokens.

### 3.2 `classification_wanted(reply) -> bool`

```python
def classification_wanted(reply: object) -> bool:
    """True when this pull request has no work type yet."""
    pr = reply.get("pr") if isinstance(reply, dict) else None
    return isinstance(pr, dict) and pr.get("pr_type") is None
```

**Ask only when the server said the PR is unclassified.** With no correction path anywhere
(§1.4), a predictable 409 is a wasted call and a confusing error, and the type it would try to
write can never be applied. A genuine race — two sessions typing the same PR in the same moment —
still reaches a 409, which §3.4 tells the agent to report and never retry.

> **Release lever.** This reads `pr.get("pr_type") is None`, so on a backend that does not know the
> field at all the key is absent and the answer is still "ask" — which is wrong for production
> until it is promoted (§7). Changing the condition to `"pr_type" in pr and pr["pr_type"] is None`
> makes the plugin safe against an old backend and lets this merge before promotion. That is a
> one-line change and a deliberate non-goal today, per §7.

### 3.3 The classification block

Appended to `CREATED` and to `IN_PLAY`'s "link it" branch when `classification_wanted(reply)`;
omitted entirely otherwise, leaving today's text byte-identical. As a module constant so tests can
assert on it:

```
In the SAME call, also record what kind of work this is: add
pr_type="<one of feat|fix|chore|docs|perf|refactor|other>" and
classification_session_id="{session_id}" — byte for byte the same string as the
entry in session_ids.
Choose ONE primary type from what this session actually changed — the diff, not
the title:
{TYPE_MENU}
A pull request that does several things gets its PRIMARY purpose; never send
more than one. This CANNOT be corrected afterwards — there is no edit path — so
when you genuinely cannot tell, send `other` rather than guessing.
```

### 3.4 Error handling, in the injected text

```
If it fails saying the classification session "was not found among your
sessions", this session's capture has not reached MemHub yet. Wait 10 seconds
and retry the identical call; if it fails again, wait 20 more and retry once.
Then stop and say the type was not recorded. Do NOT drop the type to force the
link through: an unarrived session links nothing either way.
If it fails saying the pull request "already has a different classification",
another session typed it first. Report that and stop — never retry, and never
try a different type.
```

**The two failures get opposite advice, and that asymmetry is the point.**

*404 `classification_session_not_found`* — dropping the type rescues nothing. The same unresolved
id is refused with the fields (nothing written) and merely `skipped: session_not_found` without
them (200, still nothing written). Only waiting for capture changes the answer, so: one 10s retry,
then give up on the type. One retry, not two — this fires mid-turn on a fresh session's first
`gh pr create`, and 30s of dead time on a path that used to be silent is its own regression.

*409 `pr_classification_conflict`* — dropping the type rescues **the link**, which the server
refused along with it. Here the session is perfectly valid and the link would have succeeded; only
the type is contested. So the agent retries once with both fields removed and then reports the
type that is already there. Telling it to "report and stop" — as an earlier draft of this spec did
— silently costs a link that the pre-classification plugin would have made, on a race that two
sessions on one repo, or a `/memhub:pr-babysit` loop, reach normally.

### 3.5 `context_for` threading

`context_for(reply, pr_url, session_id, *, created)` gains no parameter — it already holds `reply`
and so can call `classification_wanted` itself. The advisory paths (`CONNECT_ADVISORY`,
`REPO_ADVISORY`) and the `enabled is False` silence are untouched: an org that cannot link cannot
classify either, and asking for a type it cannot record would be noise.

---

## 4. The two skills

### 4.1 `/memhub:link-pr`

New step between "Link" and "Report", and the report step learns two outcomes.

- Skip classification entirely when the step-3 response already carries a non-null `pr.pr_type`.
  The **write** response carries it whether or not the call classified anything:
  `session_pr_link_write.py:1124-1126` loads the classification and sets `pr_type` /
  `pr_type_source` unconditionally after the link loop, and `:993-995` does the same on the
  all-skipped early return. So **no extra probe call is needed**. (§1.7 is the *check* endpoint
  and does not establish this; an earlier draft cited it here and was wrong.)
- Otherwise ask the user which type, offering the `TYPE_MENU` glosses. This skill is a human
  saying so; the type is the human's, not a guess the agent makes on their behalf. When the user
  has no opinion, `other` is the honest answer, not a coin flip.
- Send call 2 (§2.1) naming one session.
- Report `skipped: already_linked` on call 2 as **success** — "recorded as `fix`" — not as a
  skipped link.
- On the "already has a different classification" message, report the existing type and stop.

- Strip `--session` values before use. The skill took them verbatim, and per §1.6 a padded id
  links fine and then fails to classify — the one place this repo can actually produce the
  whitespace mismatch, since `conversation_id_for` strips everything the hook emits.
- When several sessions were linked, the classification's author is recorded permanently, so name
  the running session and **ask** when it is not among them rather than taking the first.

`--unlink` never classifies; unlinking does not clear a type (§1.4) and saying otherwise would be
a lie.

### 4.2 `/memhub:find-contributing-sessions`

Same second call, with one constraint: `classification_session_id` must name one of the sessions
the user actually approved. The skill has just shown the user ranked candidates with their
evidence; the type describes that work, so the type comes from the highest-scoring approved
session and the user confirms it alongside the links. If the PR already has a type, say so and
skip — a later contributing session does not get to relabel a pull request.

### 4.3 Frontmatter

No change. Both skills already list `link_pr` in both the prod and staging spellings, and
`pr_type` is an argument, not a tool.

---

## 5. Tests

`tests/pr_work_type_test.py`, stdlib only, discovered by `run_all.py`'s `*_test.py` glob, ending
with the `globals()` runner idiom so `registration_test.py`'s tail check passes. It must run under
both `python3` and `uv run --with 'mcp<2' python`, and under `check-plugin.sh`'s `env -i` with a
throwaway `$HOME` — so no ambient credentials and no network.

- `PR_TYPES` is exactly the seven lowercase tokens, in order.
- `classification_wanted`: `pr.pr_type` null → True; `"fix"` → False; `pr` missing/not a dict →
  False; reply not a dict → False.
- `context_for(..., created=True)` on an unclassified reply → contains `pr_type=`,
  `classification_session_id=`, all seven tokens, and "cannot be corrected"; the
  `classification_session_id` value is the **same string** as the one in `session_ids`.
- the same reply with `pr.pr_type="fix"` → the classification block is **absent** and none of its
  phrases leak in. Byte-identity with v0.59.0 is pinned by a **digest of the `CREATED` and
  `IN_PLAY` templates**, not by rebuilding the expectation from those same constants — a
  reconstruction would pass just as happily if the wording had been rewritten.
- both of the above again for `created=False` (B2).
- `github_connected:false` and `repo_in_install:false` → advisory text, no classification block.
- `enabled:false` → `None`.
- the retry instruction names 10s and 20s and does **not** instruct dropping the fields.

Extensions to existing suites, not rewrites:

- `tests/pr_link_trigger_test.py` — a `gh pr create` payload whose stubbed `check` reports
  `pr_type: null` → stdout carries the classification block; the same payload with
  `pr_type: "fix"` → B1 without it. Both through the subprocess, as that suite already does.
- `tests/pr_link_test.py` — unchanged assertions must still pass, proving the no-classification
  path is byte-identical.

**Never exercised against production.** Manual runs set
`MEMHUB_MCP_BASE_URL=https://api.staging.memhub.xtrace.ai` explicitly, because the staging
plugin's symlinked `scripts/` otherwise resolves to prod.

---

## 6. Documentation

- `README.md`, the Session ↔ PR linking section: a paragraph saying the agent also records one
  work type when it links, that the seven types are what MemHub accepts, that **the first
  decision is permanent**, and that a PR already carrying a type is never re-asked.
- `README.md` skill list: `/memhub:link-pr` and `/memhub:find-contributing-sessions` entries gain
  a clause about recording the type.
- `plugin.json` / `marketplace.json` descriptions if the user-visible summary changes.
- `docs/specs/pr-linking-plugin-spec.md` — §4.5 quotes the exact `CREATED` / `IN_PLAY` strings and
  §8/§9 list the skills' steps; all three become wrong and get revised, with a pointer here.
  **Do not touch** the matcher and `case`-guard strings it quotes — `tests/documentation_test.py:174-181`
  pins them.

---

## 7. Release

Five version-bearing manifests move together to the next minor (new user-visible behavior):

```
plugins/memhub/plugin.json
plugins/memhub/.claude-plugin/plugin.json
plugins/memhub/.codex-plugin/plugin.json
plugins/memhub/.cursor-plugin/plugin.json
plugins/memhub-staging/.claude-plugin/plugin.json
```

`.github/workflows/bump-guard.yml` fails any PR into `main` touching `plugins/memhub/**` without
bumping `.claude-plugin/plugin.json`. Note that `version_parity_test.py` enforces agreement among
the **four production** manifests only — staging is checked separately and needs merely a truthy
version. Prod↔staging parity is convention plus a real cache-key hazard (the plugin cache is keyed
by version, so a stale manifest is never re-fetched), not something a test will catch.

**Merge ordering — the one hard constraint.** Production deploys from `main`
(`.github/workflows/deploy-production.yml`), which is currently 74 commits behind `staging` and
carries neither `app/models/pr_classification.py` nor the `pr_classifications` migration. A prod
client sending `pr_type` to that backend gets its `link_pr` call rejected, and because
classification refuses the whole write, the result is **no link at all** — a regression on a
feature that works today.

So: **open the PR, keep it unmerged until staging is promoted to production.** There is
deliberately no runtime gate; §3.2 records the one-line change that would add one if this needs to
merge sooner. `docs/ops/next-production-release.md` in the backend recorded an open ENG-1027
prerequisite blocking normal releases as of 2026-09-12, so expect this to wait and to need a
rebase.

---

## 8. Verification before this is called done

1. `python3 tests/run_all.py` green, and `uv run --with 'mcp<2' python tests/run_all.py` green.
2. Every changed hook script smoke-tested with a realistic event JSON on stdin: exit 0, nothing on
   stdout for the unhappy path, comfortably inside its `hooks.json` timeout.
3. **The two-step recorded end to end against staging — DONE, 2026-09-17**, on this feature's own
   pull request ([agent-plugins#247](https://github.com/XTraceAI/agent-plugins/pull/247)):
   - `link_pr(session_self)` with no classification → `linked[0].created: true`, and the reply
     carried `pr.pr_type: null` / `pr_type_source: null`. **This is what closes §4.1's assumption**:
     a call that classifies nothing still reports the field, so the skills can read it off the write
     response instead of spending a probe.
   - a second `link_pr` with `pr_type="feat"` + `classification_session_id` → HTTP 200,
     `linked: []`, `skipped: [{"reason": "already_linked"}]`, `pr.pr_type: "feat"`,
     `pr_type_source: "agent_session"`. The two-step works against the deployed service, not just
     the backend's SQLite fixture.
   - `GET /v1/team/pr-links/check` then returned `feat` / `agent_session`, and the link row kept
     `link_source: "session_self"`. Feeding that exact reply to `classification_wanted()` returns
     `False` — the hook will not ask again.
   - a third call with `pr_type="chore"` → `This PR already has a different classification; linking
     cannot overwrite it.` First write wins, and that message string is the one §3.4's instruction
     matches on. MCP carried **only** the message: no `reason`, no status (§1.5 confirmed live).
4. Staging writes left in place deliberately: the link and `feat` classification on #247 are this
   feature's own honest record, not test pollution, so `scripts/purge_today.py` is not run for them.

---

## 9. Non-goals

Correcting or clearing a classification (no endpoint exists). Backfilling historical pull
requests. Multiple labels on one PR. Inferring a type from a title, a branch, a GitHub label or a
Linear ticket. Reviving PR-link hooks on Cursor. Any MemHub-Backend change — if the backend later
accepts classification from `manual` and `session_found`, §2.1's second call collapses into the
first.

## 10. Open questions

None blocking. One deferred: whether the backend should relax
`classification_source_invalid` so `/memhub:link-pr` and `/memhub:find-contributing-sessions` can
classify in a single call. That is the honest fix and it is a backend change; the two-call shape
ships now and is forward-compatible with it.
