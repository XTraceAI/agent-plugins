# Plugin wire contracts

## Native reader stream

`python3 plugins/memhub/scripts/readers_cli.py --host codex|cursor` reads native
session files and emits JSON Lines on stdout. It uses the same canonical record
normalizers as capture hooks. It does not send an import, create a capture
receipt, or change plugin capture cursors. Python 3.10 or later is required; the
MCP SDK is not required.

Each session starts with one header, followed by its canonical records until
the next header or end of stream:

```json
{"type":"session","host":"codex","native_session_id":"example-session","conversation_id":"codex-example-session","source_surface":"codex_cli","started_at":"2026-01-01T00:00:00Z","cwd":"/synthetic/project","git_branch":"example-branch","title":null,"path":"/synthetic/rollout.jsonl","mtime":1767225600.0}
```

| Field | Meaning |
| --- | --- |
| `host` | `codex` or `cursor`. |
| `native_session_id` | Required native identity; absent identity makes the session unreadable. |
| `conversation_id` | `<host>-<native_session_id>`, matching hook imports. |
| `source_surface` | Raw Codex originator or Cursor store metadata when supplied. Recognized Cursor IDE transcript locations use `cursor-ide`. Legacy stores and arbitrary transcript paths without surface evidence remain `null`. Unknown nonempty values are preserved. |
| `started_at` | Native session start, retaining the original Codex timestamp precision; `null` when unavailable. A first message timestamp or file modification time is not substituted. |
| `cwd`, `git_branch` | Native structural metadata or `null`. |
| `title` | Existing reader title in full mode; always `null` in metadata-only mode. |
| `path` | Native source path on this machine. |
| `mtime` | Unix seconds of the most recent source observation: the native file, or an existing Cursor journal, metadata sidecar or saved hook-pin file. |

Canonical records retain their existing UUIDs, message blocks and usage rules.
Cursor records also restore saved hook usage and timestamp pins through the
existing read-only helper. Saved observations are bound to their recorded native
representation: enumeration and ID selection honor a saved transcript path, while
an explicitly different or ambiguous source is rejected. Observations without
usable source provenance are reported as incomplete instead of attached by
matching record indexes. Missing usage remains missing. Restored pins are
observations, not proof that this reader performed a hook capture.

`--metadata-only` emits headers without records or prompt-derived titles. The
headers still contain structural information such as local paths and branches.
`--since <timestamp-with-timezone>` includes sessions whose `mtime` is at least
that instant. It is a modification-time prefilter; consumers apply their own
event-time selection. `--session <native-id-or-path>` selects one session using
the existing native identity rules. Candidate identities are checked for ambiguity; unrelated Cursor UUIDs are excluded before their content or saved state is parsed.

Exit code `0` means enumeration completed, including a legitimately empty
existing store or a filter matching no sessions. Exit code `2` means arguments
are invalid or coverage is incomplete. Missing roots, unreadable sessions,
unsupported metadata, and source changes during reading do not count as a
healthy empty result. Healthy peer sessions can still be emitted. Consumers
must inspect the exit code before reporting complete coverage. A missing
Python executable is a launcher error, not an empty reader result.

Runtime diagnostics use stderr JSON Lines with `type: "diagnostic"`, `host`,
`code`, and a nullable local `path`. Codes are `discovery_incomplete`,
`session_unavailable`, `session_unreadable`, and `source_changed`; diagnostics
do not include transcript excerpts or raw parser exceptions. Argument errors
use argparse's ordinary stderr help. Symlinked subdirectories and discovered file aliases are skipped and
reported as incomplete to avoid cycles and duplicate native identities. A configured root symlink
remains supported. A session is checked for source changes and JSON encoding
errors before its header is emitted; stdout delivery errors still require
the consumer to discard its incomplete stream.

Full reads reject invalid UTF-8 and malformed complete JSON rows instead of
silently replacing or skipping content. An unfinished final JSON row can wait
for a later read. Enumeration excludes all candidates with duplicate native
identities before emitting any of them; an explicit path can select one.
For Cursor, one store and one transcript may represent the same session, but
multiple stores or multiple transcripts remain ambiguous even when a preferred
representation or a saved source pin would hide the other copies.
Codex reads the complete title index lazily only when a rollout needs its fallback.
Only that session's matching title observation participates in revision checks.
Its native `updated_at` contributes to full-read `mtime` and `--since`; older
index rows without a valid timestamp conservatively use the index file mtime.
A rollout-native title and metadata-only reads never consult this fallback.
Full Codex reads resolve the title before applying the modification-time filter.
The existing capture
readers retain their tolerant defaults. Cursor SQLite files and journals are
copied into a private, temporary snapshot before opening SQLite; the native
directory is never used for shared-memory files or journal recovery. A hot
rollback journal is recovered with writes allowed only in the private copy
before the existing read-only normalizer runs. Source
revisions are compared across the read, and changed sources are reported as
incomplete. Numeric overflow in native metadata rejects that session while
preserving healthy peers.
Existing malformed Cursor observation files or invalid saved timestamp/usage
values also report incomplete coverage. Missing files and explicit unmeasured
timestamp pins remain optional. Full reads validate the Codex title-index text
when that fallback is needed; ordinary capture retains its tolerant defaults.
Complete non-object Cursor transcript rows and malformed recognized message bodies
also reject the session. Native validation is described in [native readers](native-readers.md).

## Verification

Run `python3 tests/readers_cli_test.py` for real CLI output against synthetic
Codex and Cursor fixtures. Goldens fix session headers and canonical records;
the same records are compared with the existing readers. Additional cases
cover raw/unknown surfaces, precise or absent native starts, metadata-only
content exclusion, saved pin restoration without state writes, WAL and pin
modification filtering, malformed peers, incomplete discovery, source changes,
encoding failures, and SQLite paths containing URI characters.

`bash scripts/check-plugin.sh` runs every discovered suite plus the flush shell
tests under isolated bare Python and MCP SDK environments. Existing reader,
Cursor usage and timestamp suites continue to cover cumulative usage,
deduplication and unobserved usage. No network or MCP import is needed by the
reader CLI; subprocess tests reject both.

## Capture destination API

`plugins/memhub/scripts/sinks.py` provides a read-only destination resolver for
capture consumers. Callers must explicitly adopt it. Existing hooks and the
cloud-service functions in `_memhub_auth` keep their current routing; adding
this configuration file alone does not change a running capture pipeline.

The resolver reads `~/.config/memhub-plugin/config.json`:

```json
{
  "version": 1,
  "sinks": [
    {"name": "local", "url": "http://127.0.0.1:47421", "token": "local"}
  ],
  "active": ["local"]
}
```

`sinks` is a registry; `active` is its ordered membership. Adding an entry does
not activate it. Names contain 1–64 ASCII letters, digits, underscores or
hyphens, and must be unique. The optional `mcp_path` defaults to
`/mcp-server/mcp` and is appended to the base URL. A base URL cannot contain
userinfo, a query or fragment. Remote destinations require HTTPS; HTTP is
accepted only for literal `localhost`, `127.0.0.1` or `::1`. Tokens are optional
nonempty ASCII bearer values without whitespace/control characters.
Endpoints require standard ASCII DNS names (IDNs use their ASCII form) or IP
literals. Underscore/escaped host spellings are refused before credential
lookup, preventing collisions in the existing host/port cache naming format.
An independent loopback service requires an explicit token; it cannot borrow
a legacy token cached for another protocol on the same host/port.

Selection follows this order:

1. `MEMHUB_MCP_BASE_URL` selects one explicit endpoint, with the existing
   `MEMHUB_MCP_SERVER_PATH` behavior, ignoring file and named selection.
2. Otherwise `MEMHUB_SINKS` selects comma-separated registry names when present.
3. Otherwise the file's `active` names are used.
4. A missing, unreadable or corrupt file preserves the legacy cloud destination
   from the installed `.mcp.json` and existing install-path fallback.

Selecting the name `cloud` without a registry entry synthesizes that legacy
destination. Repeated selected names are deduplicated. An explicitly empty
active list or empty `MEMHUB_SINKS` disables selection. Unknown names, unsupported
integer configuration versions, unsafe endpoints, duplicate sink names and
invalid explicit destinations raise `SinkConfigError` without selecting another
destination. Files are bounded to 64 KiB; malformed JSON, duplicate JSON keys
and malformed outer structure are treated as corrupt-file fallback.

`resolve_capture_sink()` returns an immutable `Sink` or `None` when explicitly
disabled. It rejects multiple active destinations until a caller implements
independent delivery. `active_sink_names()` exposes the validated ordered names.
A `Sink.url` is the complete MCP endpoint, `mcp_path` retains its path/query,
and `is_local` describes a literal loopback destination. Its representation
omits URLs and credentials.

`resolve_capture_auth(sink)` returns `(endpoint, bearer)` with this precedence:
explicit `MEMHUB_TOKEN`, selected sink token, then existing credentials looked
up for the selected endpoint. Existing caches remain keyed by backend host and
port; this does not add path-specific credential storage. The resolver never
borrows the default cloud host's credential for a different host. OAuth refresh
uses installed-plugin metadata only for that same backend origin. When the
selected URL has no stored credential, an equivalent origin with different
hostname casing or an explicit default port can reuse the installed URL's
credential; requests still use the selected endpoint. A different
origin can use its own stored PAK or still-valid cached token without sending
its refresh token to the installed backend's authorization server.
A trailing DNS dot remains part of the URL host, so `https://example.test`
and `https://example.test.` require their own credentials even if DNS resolves
them to the same address ([URL Standard](https://url.spec.whatwg.org/#host-equivalence)).

File-based capture selection does not change `default_url()`, unqualified
`resolve_bearer()` or `resolve_url_and_auth()`. Login, recall, brain overview and
artifact consumers continue using their existing cloud-service resolver.
The explicit environment variables retain their existing global semantics.

`python3 tests/sinks_test.py` checks precedence, corruption fallback, explicit
disablement, invalid/unknown selections, loopback/TLS restrictions, distinct
stored backend credentials, refresh-origin isolation and unchanged service
resolution. It checks configuration bytes and absence of account writes, and
runs under bare Python while rejecting MCP imports and network access.
Hook delivery and capture-health integration are separate acceptance work.
