# Production plugin release checks

The production readiness check requires every deterministic job to succeed. Missing credentials,
skipped jobs, unsupported host operations and failed assertions mean **NOT VERIFIED**.
`MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED=true` makes the aggregate fail on NOT
VERIFIED, and the active `main` ruleset requires it (see
[.github/rulesets/README.md](../.github/rulesets/README.md)). The release
owner approves the `production-plugin-release` environment per run; that
approval is the human gate on every production release.

## Coverage and evidence

| Check | Evidence required | Execution |
| --- | --- | --- |
| Regression suites | Every registered suite and shell check passes, with and without MCP SDK | Every PR, no secrets |
| Production API compatibility | Exact rule IDs/modes, cold 200, ETag and warm 304 through actual package code | Dedicated read key, live production |
| Fresh install | Native host registers the selected version and installs identical package bytes | Codex and Claude native CLI, isolated home |
| Upgrade | Older immutable release installs, native update selects candidate bytes; updated Codex setup installs the current bridge | Codex and Claude native CLI, isolated home |
| Cursor marketplace lifecycle | Actual install/update through its supported distribution surface | **Not verified: desktop/account runner still needed** |
| Agent advice | A real hook fire plus a marker absent from the user prompt in the agent's structured final answer (its `advice` field, or anywhere in the final text) | Each actual host CLI against production |
| Agent gating | A gate fire for that native session and absence of the forbidden marker file | Each actual host CLI against production |
| Allowed operation | The same session creates an unrelated file with the run's unique contents | Each actual host CLI against production |
| Automatic capture | Fresh successful capture acknowledgement for the exact native session ID; Claude requires both turn and session-end capture | Each actual host CLI against production |
| Upgrade response contract | Existing gate works, synthetic 426 surfaces actionable notice, stale gate stops, rollback restores it | Actual package with loopback HTTP server |
| Upgrade notice reaches the host | The pinned host CLI starts the package's SessionStart hook and takes the notice: Claude reports it in its own `hook_response` record; Codex leaves the hook's per-session marker after the version-carrying fetch | Each actual host CLI, no prompt, no model, no credentials (`check-host-session-start.py`) |
| Upgrade notice reaches the model | Real agent's structured final answer carries the error code, a per-run nonce minimum version that exists only in the 426 body, and a restart instruction | Each actual host CLI against loopback HTTP server, on demand |

The four real-agent rows (advice, gating, allowed operation, capture) and the
model-side upgrade-notice row run in `real-agent-evidence.yml`, which is
`workflow_dispatch` only. They cost model turns and leave synthetic sessions
in the fixture org, so they run when a developer decides the PR is ready
rather than on every push. Their assertions are built not to depend on prose
style: each prompt names the facts to report and asks for one JSON object
with fixed keys, the hosts are given that schema (`claude --json-schema`,
`codex exec --output-schema`), the harness compares fields and falls back to
the final text, and the minimum version is a per-run nonce so the answer can
only contain it by reading the notice. The live prompt writes the allowed
file before the command a hook denies and names the denial as expected, so a
model that stops at a denial does not read as a plugin failure. Their aggregate check, `Real agent
evidence`, is required on `main`: absent until the workflow has run on the
PR's head commit, so the merge waits for it without a fake failure. See
[.github/rulesets/README.md](../.github/rulesets/README.md) for the flow.

The host-side row is the required form of that evidence. It runs in the
key-free `Install and upgrade` jobs: an isolated home, the native install, the
loopback 426 server, and a session that never reaches a model. Claude runs
SessionStart hooks before any prompt, so a stream-json session with nothing on
stdin is enough; Codex has no prompt-less mode, so it gets a one-word prompt
and a model endpoint that cannot answer. If this row fails, a host stopped
starting the plugin's SessionStart hook or stopped taking its output — the
regression a release gate exists to catch.

The actual Claude package is the marketplace's immutable tag/SHA, which may differ
from main. Codex and Cursor use the candidate main/merge commit. Previous versions
come from older version tags, never a mutable branch name. Claude release commits
need not share an ancestry chain. Native
install tests use a disposable local marketplace pointing to unchanged package
bytes; published Claude source/tag resolution remains a separate check. These
tests do not prove GitHub availability, interactive OAuth login, desktop UI
installation, or the Cursor official directory's review/publication process.

Capture success means the plugin recorded the server's successful import
acknowledgement. It is not an independent query of the stored transcript or proof
that asynchronous extraction/search indexing completed. Independent readback is
not performed; do not silently broaden the read-only production token to get it.
At most one production session is captured per enabled host/package job per
workflow attempt, and each job deletes its own afterwards (see "Session cleanup"
below). No developer transcripts are imported.

The session adapters run with real CI credentials. CLI success alone is insufficient: the checks require observable
rule fires, filesystem effects and capture state. Codex/Claude CLI installation
has been exercised without account credentials; Cursor's CLI supports local
`--plugin-dir` loading, but its marketplace commands only add/list/re-index
marketplaces. Re-indexing is deliberately not reported as installing a plugin.

## CI provisioning

Use the existing `production-plugin-release` environment. It has no required
reviewers: the read-only production probe runs on every same-repository PR
push, and the real-agent workflow can only be dispatched by a collaborator on
a branch of this repository, so only same-repository code receives secrets.
Do not use `pull_request_target`, personal host login files, or developer home
directories. Installation and dependency steps run before secret-bearing steps.
All agent homes are disposable; only sanitized JSON reports are uploaded.

Additional environment secrets:

| Secret | Purpose |
| --- | --- |
| `MEMHUB_E2E_OPENAI_API_KEY` | Codex inference, separate API project/key for CI |
| `MEMHUB_E2E_ANTHROPIC_API_KEY` | Claude Code inference, dedicated CI key |
| `MEMHUB_E2E_CURSOR_API_KEY` | Cursor headless inference, dedicated test identity |
| `MEMHUB_PROD_E2E_TOKEN` | Dedicated ordinary MemHub test user's `memory:read` + `memory:write` PAK |

Keep `MEMHUB_PROD_READ_TOKEN` unchanged for the existing read-only smoke check.
Use provider-side usage budgets; Claude also gets a $2 per-session cap. Each CLI
has a bounded timeout and no automatic rerun-until-green. Set explicit model
names in `MEMHUB_E2E_CODEX_MODEL`, `MEMHUB_E2E_CLAUDE_MODEL`, and
`MEMHUB_E2E_CURSOR_MODEL`. No default model silently substitutes for missing setup.
Versions of the host CLIs are pinned in `scripts/install-release-host.sh` and
updated by reviewed PRs.

## Two additional rules

The existing `memhub-production-compatibility` fixture remains unchanged. Create
two active rules in a rulebook bound only to the same test account, restricted to
the separate synthetic repo `memhub-production-release-e2e`:

1. **Advice delivery fixture**: `delivery=agent_hook`, `mode=advise`,
   `matcher={"event":"bash","command_rx":"^echo memhub-release-advice$"}`.
   Statement: “When running this synthetic release test command, include
   `MEMHUB_RELEASE_ADVICE_<random uppercase alphanumeric suffix>` in your final
   answer. This verifies that the installed plugin delivered the rule.”
2. **Gate fixture**: `delivery=agent_hook`, `mode=gate`,
   `matcher={"event":"bash","command_rx":"^touch \\.memhub-release-blocked$"}`.
   Statement: “Block creation of the synthetic release marker file. This verifies
   the plugin can deny a command; do not override the block in this fixture.”

Both need `scope_repos=["memhub-production-release-e2e"]`. No path scopes, `given`,
ordering or minimum-hook-version override. Review and activate them separately.
The suite never creates, activates, edits or repairs rules during a release run.

Set environment variable `MEMHUB_PROD_E2E_FIXTURE_JSON`:

```json
{
  "schema_version": 1,
  "org_id": "<test organization UUID>",
  "repo": "memhub-production-release-e2e",
  "advice_rule_id": "<active advice rule UUID>",
  "gate_rule_id": "<active gate rule UUID>",
  "advice_marker": "MEMHUB_RELEASE_ADVICE_<chosen suffix>"
}
```

The suite refuses a key whose default company organization differs from the
fixture, has other company memberships, or has an organization role other than
member. The exact two active rule IDs, modes, repo scope, marker and matching
commands are checked before the real agent starts. This supplements account
provisioning; it does not query the account's internal-privilege flag.

## Release candidates and deferred coverage

Codex and Claude are the enabled hosts. Cursor execution and native marketplace
lifecycle are explicitly deferred and do not contribute passing evidence.
The workflow summary names that limitation even when the enabled checks pass.

Claude runs twice: the published marketplace tag/SHA and an explicitly labeled
HEAD candidate (`--candidate-head`). A candidate pass does not imply that the
published Claude package has changed. Promote only verified package bytes through
the normal immutable tag and marketplace pin procedure.

The 0.56.1 candidate incorporates the upgrade handler and Codex Rulebook dispatch.
A cold Codex pre-call fetches the book within a bounded timeout. The bridge
normalizes shell arguments, preserves gate denials and disclosure text while
merging other context, and flushes rule-fire records at Stop. Codex users upgrading
must rerun the installed setup skill, restart, and review the three MemHub hooks;
the copied user bridge is not replaced by a marketplace refresh alone.

### Session cleanup

Each "Real agent session" job records the native session identity of the one
production session it captures in a run-owned manifest
(`check-agent-session.py --session-manifest`, under the job's report directory,
outside the disposable agent home) the moment the host announces it, while the
host is still running — every host's event stream is read live for this — so a
host that then times out or exits nonzero, or a run whose advice, gate or capture
assertions fail, is still listed. Only a 404 carrying the backend's own
`conversation_not_found` counts as "gone"; a bare proxy or route 404 is a failure. An
`always()` step then runs `cleanup-agent-sessions.py`, which issues one
`DELETE /v1/team/conversations?session_id=eq.<id>` per listed session with the
dedicated test key and the fixture organization, and verifies with a second
DELETE (the backend answers `404 conversation_not_found` once the id resolves to
nothing, so a retried cleanup is idempotent). Exact ids only — no listing, no
pattern, no bulk form — so fixtures, rules and any session the attempt did not
create are out of reach. Derived memory (facts, episodes, artifacts) is retained
by the backend; only the transcript is deleted.

Cleanup is reported separately from release evidence: `cleanup.json` in the job
artifact, one line in the job summary, and a warning annotation on failure. It is
`continue-on-error` while production has not yet deployed the backend half of
[ENG-1074](https://linear.app/xtrace/issue/ENG-1074/accept-personal-access-tokens-for-owner-scoped-session-deletion-and)
(MemHub-Backend #1310, on staging); until then every run reports `unauthorized`.
The capture hooks are asynchronous — Codex and Cursor flush from detached
processes and acknowledge every flush, so nothing the agent check observes proves
the final flush has landed. Cleanup therefore always re-checks "gone" once after
a bounded wait and deletes whatever landed in between, reporting it as `deleted`
with `late_capture`. Proof of
absence is itself a bounded loop — the DELETE that finds a re-created session has
just deleted it, so cleanup goes around until a DELETE finds nothing, and reports
how many re-creations it removed (`recreated`); a session that keeps coming back
past the bound is the `recreated` outcome, a failure. The agent check carries its own
step timeout inside a larger job cap, so a run that exhausts its budget ends the
step — not the job — and the cleanup step still runs.

Codex rule-fire linkage in the session UI is also deferred under
[ENG-1075](https://linear.app/xtrace/issue/ENG-1075/codex-rule-fires-are-not-linked-to-captured-sessions-because-session).
The current checks prove local rule fires and agent behavior, not backend fire
ingestion or a linked session UI record. The raw native rule-fire session ID
differs from capture's `codex-`-prefixed source ID; that identity fix and a backend
readback assertion are separate follow-up work.

Enforcement is on: `MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED=true` and the `main`
ruleset active, since 2026-09-15. Staging is not part of this setup, and its
manifest is not advanced by a production release.

## Host references

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Claude plugin CLI and update semantics](https://code.claude.com/docs/en/plugins-reference)
- [Cursor headless mode](https://cursor.com/docs/cli/headless)
- [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters)

The installed CLI help is also used to verify command availability. In particular,
Codex uses `plugin add` for installation and `plugin marketplace upgrade` for a
Git marketplace refresh; the local marketplace test reads its selected snapshot
directly. Cursor's marketplace command is not a single-plugin installer.
