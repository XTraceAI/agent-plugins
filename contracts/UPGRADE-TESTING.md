# Upgrade-capable plugin candidate (ENG-1063)

The 0.55.1 candidate understands structured `PLUGIN_UPGRADE_REQUIRED` HTTP 426
responses on rulebook fetches. It preserves cache bytes for rollback but stops
using cached rules while the backend has rejected the client. A successful
200/304 is required to clear that state; a network failure cannot hide it.
Notices are scoped to backend and credential, contain validated version strings
rather than arbitrary server instructions, and appear once per session/policy
(or at session start) in the agent context.

Run `bash scripts/check-plugin.sh`. The backend's
`tests/test_plugin_upgrade_integration.py` runs this checkout over real loopback
HTTP and verifies normal fetch → rejection → recovery. Point
`MEMHUB_UPGRADE_PLUGIN_ROOT` at this checkout. `scripts/check-plugin-upgrade.py`
requires an isolated cache and refuses non-loopback origins.

Install this checkout through the internal staging marketplace (CONTRIBUTING.md)
to test actual host visibility. Claude receives `additionalContext` and a
`systemMessage`; Codex combines upgrade context with its existing pre-tool
context; Cursor receives `agent_message`/`user_message` on its shell hook
([Cursor hook contract](https://prod.cursor.com/docs/hooks)). These hooks remain
non-blocking for unrelated user actions. Refresh is normally attempted within a
minute. A restarted session may initially use fresh cached data until that
refresh completes. Verify the installed host version actually renders the notice
before activating production rejection.

Previously released plugins do not acquire this handling retroactively. Publish
and verify the upgrade-capable release before raising a production floor. A
version bump merged to main publishes on unpinned channels, so keep this PR
unmerged while testing; the Claude marketplace pin is deliberately unchanged.
