# Production plugin release checks

The production readiness check requires every deterministic job to succeed. Missing credentials,
skipped jobs, unsupported host operations and failed assertions mean **NOT VERIFIED**.
While `MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED` is unset, the aggregate reports that
result without blocking merges. Do not enable enforcement until the gaps below
are closed and all enabled hosts have a real successful run and a deliberate failure run.

## Coverage and evidence

| Check | Evidence required | Execution |
| --- | --- | --- |
| Regression suites | Every registered suite and shell check passes, with and without MCP SDK | Every PR, no secrets |
| Production API compatibility | Exact rule IDs/modes, cold 200, ETag and warm 304 through actual package code | Dedicated read key, live production |
| Fresh install | Native host registers the selected version and installs identical package bytes | Codex and Claude native CLI, isolated home |
| Upgrade | Older immutable release installs, native update selects candidate bytes; updated Codex setup installs the current bridge | Codex and Claude native CLI, isolated home |
| Cursor marketplace lifecycle | Actual install/update through its supported distribution surface | **Not verified: desktop/account runner still needed** |
| Agent advice | A real hook fire plus the final assistant answer contains a marker absent from the user prompt | Each actual host CLI against production |
| Agent gating | A gate fire for that native session and absence of the forbidden marker file | Each actual host CLI against production |
| Allowed operation | The same session creates an unrelated file with the run's unique contents | Each actual host CLI against production |
| Automatic capture | Fresh successful capture acknowledgement for the exact native session ID; Claude requires both turn and session-end capture | Each actual host CLI against production |
| Upgrade response contract | Existing gate works, synthetic 426 surfaces actionable notice, stale gate stops, rollback restores it | Actual package with loopback HTTP server |
| Upgrade notice reaches agent | Real agent reports error code, required version, and restart instruction absent from its prompt | Each actual host CLI against loopback HTTP server |

The four real-agent rows (advice, gating, allowed operation, capture) and the
upgrade-notice row run in the `Real agent session` jobs, which are **advisory**:
they report on the PR but are not inputs to `Production plugin readiness`. Two
reasons. They assert on what an LLM chooses to echo (the upgrade-notice check
flipped between pass and fail on byte-identical packages, same pinned CLI and
same model within one hour), and they sit behind the `production-plugin-release`
environment reviewer gate, which would otherwise put a human approval on every
merge. Read their reports before promoting a release; do not treat a red job as
a merge blocker.

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
that asynchronous extraction/search indexing completed. The current PAK cannot
use the JWT-only conversation read/delete routes. Independent readback and full
cleanup need a suitable backend API or a separate authenticated test runner;
do not silently broaden the read-only production token. Synthetic sessions are
retained in the dedicated test account for now, at most one production session
per enabled host/package job per workflow attempt. No developer transcripts are imported.

The session adapters run with real CI credentials. CLI success alone is insufficient: the checks require observable
rule fires, filesystem effects and capture state. Codex/Claude CLI installation
has been exercised without account credentials; Cursor's CLI supports local
`--plugin-dir` loading, but its marketplace commands only add/list/re-index
marketplaces. Re-indexing is deliberately not reported as installing a plugin.

## CI provisioning

Use the existing `production-plugin-release` environment, with required reviewers
approving the exact candidate. Only reviewed same-repository code receives secrets.
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

Session cleanup remains deferred by request. Track backend personal-token deletion
support and the subsequent CI cleanup change in
[ENG-1074](https://linear.app/xtrace/issue/ENG-1074/accept-personal-access-tokens-for-owner-scoped-session-deletion-and).
Until that ships, synthetic sessions remain in the dedicated test account.

Codex rule-fire linkage in the session UI is also deferred under
[ENG-1075](https://linear.app/xtrace/issue/ENG-1075/codex-rule-fires-are-not-linked-to-captured-sessions-because-session).
The current checks prove local rule fires and agent behavior, not backend fire
ingestion or a linked session UI record. The raw native rule-fire session ID
differs from capture's `codex-`-prefixed source ID; that identity fix and a backend
readback assertion are separate follow-up work.

Keep `MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED` unset and the production ruleset disabled.
Staging is not part of this setup, and its manifest is not advanced by a production
release.

## Host references

- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Claude plugin CLI and update semantics](https://code.claude.com/docs/en/plugins-reference)
- [Cursor headless mode](https://cursor.com/docs/cli/headless)
- [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters)

The installed CLI help is also used to verify command availability. In particular,
Codex uses `plugin add` for installation and `plugin marketplace upgrade` for a
Git marketplace refresh; the local marketplace test reads its selected snapshot
directly. Cursor's marketplace command is not a single-plugin installer.
