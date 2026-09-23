# Plugin operation compatibility (ENG-1092)

The Python transport snapshots its package version at import and sends it on
every REST/MCP request, including the SDK auth path. Native host connections
carry the version in `headers.X-MemHub-Plugin-Version` in both MCP configs.
`version_parity_test.py` requires the header to match the package manifest; bump
all four manifests and both headers together in the internal repository, then
promote the tested commit. Keep the MCP URL canonical and unchanged across
releases: Codex hashes the full URL for its OAuth identity, so adding a version
query breaks login. Public CI temporarily permits only the already-shipped
0.76.1 query configuration until the first header-based promotion; it is not a
template for a new release.
Downloading a manifest does not upgrade an already-running connection/process.

Valid upgrade errors are parsed from HTTP 426, JSON-RPC errors, and native MCP
tool-error text. Parsing is bounded and accepts only the known error code and a
numeric three-component floor; server-provided shell text is never executed.
The common status is scoped to backend origin and credential digest, so a
production rejection cannot block staging or another account. It suspends cached
Rulebook state as well as network operations, without blocking unrelated tools.

Queued captures/imports keep their success markers unchanged. Repeated operation
attempts reuse the refusal for 60 seconds, then check compatibility again.
Startup checks run through Claude/Codex capture health and Cursor's first prompt;
notices reach both the agent and user. Recovery requires the active code's version,
an authenticated compatibility result, and a successful protected `list_orgs`
read. A download, reconnect, or successful bootstrap tool listing alone does not
clear the status.

## Host recovery

- Claude Code: `/plugin marketplace update memhub`, then
  `/plugin update memhub@memhub`, followed by a new session. Staging uses
  `memhub-internal` and `memhub-staging@memhub-internal`. The local CLI's
  `claude plugin update --help` explicitly confirms restart is required.
- Codex: refresh the appropriate marketplace and update the plugin through
  Plugins. For bridge changes rerun the installed setup skill, restart Codex,
  and review its MemHub hooks. Package replacement alone cannot update the
  copied hook configuration.
- Cursor: refresh the appropriate marketplace in Settings > Plugins, update
  MemHub, and restart the agent session. Marketplace refresh alone is not proof
  that the running connection loaded the new version.

The notices use ENG-1091's public `/plugin/claude-code`, `/plugin/codex` and
`/plugin/cursor` guides (or `/plugin` when the host is unknown), with the staging
frontend for the staging package. Guide deployment and real-host update/error
visibility checks are release prerequisites. Unsupported release channels keep
their pending work; no independent self-updater is installed.

The backend flag is intentionally off until the candidate has been published
and tested across all three hosts. No live minimum version is changed by this
implementation PR. Refer to the backend's `docs/ops/plugin-operations-policy.md`
for classification, activation, rollback and the standalone-key limitation.

## Session-start update notices

A newer public marketplace version produces an informational “MemHub update
available” notice even when the active plugin is compatible. Claude uses the
marketplace's pinned commit; Codex and Cursor use the version shipped on main.
Cursor's notice explicitly notes that official-directory availability may lag.
Staging is a locally copied marketplace and has no public optional-release feed.
The public lookup sends no credentials, is bounded, and caches results (including
failures) for one hour. An offline lookup never blocks work. Notices are deduplicated
per session; a new session can remind the user again.

A backend refusal takes priority: “UPDATE REQUIRED” names the active and minimum
versions, paused operations, preserved pending captures and exact update/restart
steps. Claude/Codex emit a prominent user-facing warning and mirror it to the agent;
Cursor sends the same required notice to both. Optional notices do not mask other
capture-health failures and never create a blocked-operation record.
