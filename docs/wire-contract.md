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
use argparse's ordinary stderr help. Symlinked subdirectories are skipped and
reported as incomplete to avoid traversal cycles. A configured root symlink
remains supported. A session is checked for source changes and JSON encoding
errors before its header is emitted; stdout delivery errors still require
the consumer to discard its incomplete stream.

Full reads reject invalid UTF-8 and malformed complete JSON rows instead of
silently replacing or skipping content. An unfinished final JSON row can wait
for a later read. Enumeration excludes all candidates with duplicate native
identities before emitting any of them; an explicit path can select one.
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
capture consumers. Automatic Claude turn/backstop, Codex and Cursor conversation
hooks adopt it at their executable entrypoints. Cloud-service functions in
`_memhub_auth` retain their existing routing. This selection does not redirect
manual imports, artifact uploads, login, recall or brain briefs.

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
`resolve_capture_sinks()` returns every explicitly active destination as an
immutable tuple for callers that provide independent delivery.
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
`python3 tests/capture_routing_test.py` invokes all four real hook entrypoints
against loopback receivers with a synthetic home. It verifies file selection,
environment/token overrides, inactive/unknown membership, local room-lookup
exclusion, raw/absent surface identity, failed-send retry, endpoint switching,
shared Cursor pins and separate capture/cloud-service health.

### Conversation hook delivery

Each delivery freezes its selected destination for authentication, state,
room routing and async work. Empty or invalid selection skips delivery. Cursor
still preserves local per-generation usage and timestamp observations for a
later read or configured delivery; this does not authenticate or upload.
Claude per-turn and SessionEnd/commit/PR backstops, Codex and Cursor capture
support multiple active destinations.
The installed endpoint keeps its existing room-cache namespace. Other remote
endpoints use a digest of their complete URL, so one server cannot read or
overwrite another server's cached room ID. Loopback never resolves cloud rooms.

The existing installed cloud retains its legacy state. Other named or explicit
environment destinations store progress under the hook state directory, then
sink name and a digest of the full endpoint. Changing an endpoint under the same
name starts independently; returning to it resumes its own progress. Credentials
are excluded from this identity. This prevents cloud cursors or dormancy from
skipping an initial local import. An explicit environment override can therefore
re-send an existing session once; server UUID deduplication handles that replay.

Cursor usage observations, native source metadata and first-seen timestamp pins
remain in the original shared session file consumed by the reader CLI. Only
upload watermarks, accepted provenance and failures belong to the destination.
A short shared observation lock serializes native reads and pin updates before
network work. Destination upload locks are independent. Observations are saved
before selection and upload locking, including when capture is disabled or
every destination is busy. Destination files cannot override shared pins.

Literal loopback capture skips cloud room resolution and adds native session ID
plus an observed raw surface when available. Claude reads explicit `source_surface`
or `entrypoint`; Codex reads native `originator`; Cursor reads explicit metadata
or its recognized native source location. Missing surfaces remain omitted. An explicit old-cloud optional-field rejection
permits one compatibility retry. Canonical host-prefixed conversation IDs and
record UUIDs retain the existing reader convention.

Capture health reads failures only from the selected destination. A success at
another destination cannot clear that failure, and a constant local token cannot
certify cloud login. Cloud-service authentication and rulebook issues are reported
separately. The health hook does not contact either destination or report a
connection as proven merely because credentials exist.
An expired access token with same-origin renewal metadata and a saved refresh
token does not produce a missing-credential warning. This is a local capability
check; it makes no network request and does not clear a recorded delivery error.

### Claude per-turn delivery to multiple destinations

The turn hook takes an immutable active selection and visits loopback endpoints
first. Registry-only destinations never receive a request. It reserves a share
of the remaining time for each destination, within a total network deadline of
60 seconds (or a smaller positive `MEMHUB_TURN_FLUSH_TIMEOUT_S`). Auth and HTTP
work use daemon workers around the existing transport, so a slow-dripping body
cannot keep the hook alive after cancellation. A late result never advances
upload progress; an uncertain response is retried with the same record UUIDs.

Each destination has its own session lock, cursor, failure and unsupported-server
state. Only the unchanged installed cloud uses the legacy flat cursor. A held
cloud lock or unsupported server does not disable local capture. The prefilter
skips only when every active destination is caught up, dormant or already held.
Health identifies the destination whose delivery failed.

Catch-up uses at most 2,000 native records or approximately 3.5 MB of source per
batch. A larger individual record is read whole and the existing tool-result
and oversized-prose elision bounds its wire payload while retaining its identity.
Read memory therefore scales with the largest single native record. An attachment
prefix that crosses a batch boundary carries a replay of the next complete native
message as context, with the same UUID. Its cursor commits only the prefix byte
span; the physical message is consumed later and safely deduplicates. With no
complete message yet, the prefix remains pending. A final
partial line waits for its newline. Each
successful batch commits its own cursor; later failures retain earlier progress.
Multiple-destination capture keeps retrying an endpoint without durable per-turn
acknowledgements, since this standalone change cannot assume a multi-destination
backstop. Single-destination capture retains legacy backstop dormancy.
The acknowledgement must match the conversation and final sent record UUID,
or explicitly account for records the server received and deliberately dropped.

Redaction reuses a bounded invocation-local cache across destinations. Payloads
get separate copies, and endpoint-specific fields cannot mutate shared content.
Eviction after 8 MiB of cache keys can repeat redaction during large catch-up;
it cannot change or skip records. Both destinations receive native session ID
and an observed raw surface when present. If an older non-loopback endpoint
explicitly rejects those optional argument names, one retry omits only those
fields. Local payloads retain them; unrelated errors are not retried this way.

`python3 tests/multi_sink_test.py` exercises real subprocess hooks and two
loopback receivers. The fake cloud's logical HTTPS URL is mapped to its test
receiver at the existing HTTP boundary, while a socket guard forbids outside
connections. Cases cover explicit membership, identity, local-first order,
401/429/500 recovery, slow-drip deadlines, overlapping hooks, legacy cursors,
per-destination locks/dormancy, old-cloud compatibility, batching, acknowledgement
validation and redaction isolation. No hosted CI job is added.


### Claude session-end, commit and PR backstops

The whole-session backstop uses the same frozen active-destination selection,
local-first ordering and independent state as turn capture. Each destination
has its own backstop lock and health breadcrumb, separate from its turn cursor.
A cloud error or held cloud lock does not prevent a local import. Backstops
continue to resend the transcript-so-far; stable UUIDs and server deduplication
make replay safe without adopting another destination's progress.

A shared deadline defaults to 240 seconds, with a reserved share for each
remaining destination. `MEMHUB_FLUSH_DEADLINE_S` may lower it; non-finite or
nonpositive values fall back to the default. Auth and transport use bounded
cancellable waits. Each request contains at most 2,000 records and retains the
existing byte-sized slices. A timeout or unfinished set of slices records a
failure for retry, including when earlier slices succeeded.

Every destination receives the same redacted records and discovered PR
provenance. Native identity and an observed raw surface are included; an absent
surface remains absent. Only an explicit old-cloud rejection of those optional
fields permits one retry without them. Local acknowledgements must identify
the same conversation and account for the slice. A legacy cloud backstop may
still accept its matching conversation response without a durable watermark;
this never advances the separate per-turn cursor.

Run `python3 tests/backstop_sinks_test.py` for dual delivery, independent cloud
failure/recovery, replay, provenance, old-cloud compatibility, missing surface,
slow response deadlines, bounded record batches and concurrent backstop locks.


### Codex delivery to multiple destinations

Codex Stop and milestone hooks use the shared frozen destination list. Each
endpoint has its own rollout-size watermark, lock, cooldown, unsupported-server
state and pending PR provenance. Only the unchanged installed cloud adopts the
legacy flat state file. A held lock or local state-write failure cannot abort
another destination's capture.

The whole invocation shares a 240-second budget, reserving time per destination
with loopback first. Lock waits consume that destination's allocation; transport,
authentication and repository lookup cannot hold the process open past its wait.
Whole-rollout imports keep the existing canonical reader and redaction. Batches
contain at most 2,000 records and retain byte-sized slices. Every batch must
confirm the same conversation and its final record before the rollout watermark
advances; a failed later batch leaves the previous watermark for safe replay.

`conversation_id` remains `codex-<native_id>`, `native_session_id` remains native,
and an observed originator is forwarded unchanged as `source_surface`. Missing
originator metadata remains unknown. One explicit old-cloud optional-field
rejection permits a compatibility retry without changing the local envelope.
Canonical usage deltas and record UUIDs match the existing Codex reader.

Run `python3 tests/codex_sinks_test.py` for independent failure and recovery,
per-destination dormancy, native identity and usage parity, old-cloud fallback,
legacy state ownership, partial-batch retry, lock deadlines and write failures.


### Cursor delivery to multiple destinations

Cursor hooks retain native usage, source choice and timestamp observations once
per session, then deliver independently to frozen active destinations with
loopback first. Each endpoint owns its upload revision/blob set, acknowledged
usage generations, provenance, backoff, dormancy and long-running upload lock.
A held or slow cloud upload cannot block local capture or shared observations.
The overall 240-second budget includes lock waits and reserves time for every
remaining destination. Authentication and transport use cancellable waits.

The existing canonical reader supplies records; redaction is cached within the
invocation. Requests retain byte-sized slices and contain at most 2,000 records.
Every slice must acknowledge its final record before that destination advances
its whole-source watermark. A later failure retains the previous watermark;
replay uses the same UUIDs and shared pins. Late exact hook usage enriches those
same records, whether the initial read or the usage event arrives first.
An unobserved value stays unknown. Native identity and a known raw surface are
preserved; old-cloud optional-field fallback does not alter record content.

Run `python3 tests/cursor_sinks_test.py` for failure/recovery, both late-usage
arrival orders, canonical parity, raw/unknown surfaces, legacy cloud dormancy,
partial batches, bounded slow responses, busy destination locks and concurrent
observation updates during an in-flight upload. Existing Cursor capture, usage
and timestamp suites remain part of the full plugin check.

Shared Cursor usage observations retain one exact sample per measured record
for the session's lifetime, like timestamp pins. They are not evicted after a
fixed number of generations: a destination that was offline, or is configured
later, must still be able to receive earlier measured usage. Destination
acknowledgements remain separate from this authoritative evidence.

## Rule-event capture destinations

The existing `rulebook_hook.py flush [final]` entrypoint delivers the shared
append-only fire/conversion ledger to each active capture destination. Each
gets its own URL-scoped watermark, nonblocking upload lock, throttle, failure
breadcrumb and rejection log. Only the unchanged installed destination named
`cloud` inherits the legacy flat ledger progress. Foreground rule fetching,
recall and login keep the cloud-service resolver.

Delivery reserves each destination part of one 60-second budget, with loopback
first. A blocked lock or timed-out cloud response cannot hold local progress.
The existing REST client and credential resolver are reused; no new transport
or dependency is introduced. Fire hooks remain attached to the existing Claude
Stop/SessionEnd events; this does not add fire hooks to other hosts.

Every parsed ledger row participates in receiver accounting, including
unchanged duplicate fire IDs. Three input rows with two unique IDs must yield
`accepted: 3, rejected: 0` when all are valid, while storage stays at two fires.
The client advances a batch only when accepted plus rejected accounts for every
input row. Counts must be nonnegative integers and cannot exceed the batch size;
an absent rejected count means zero. Malformed rows are individually rejected
by the receiver. Repeated short-counted batches retain the existing three-attempt
quarantine policy, independently per destination. A final flush bypasses the
ordinary count/time throttle, and each accepted batch saves progress before the
next request.

Remote requests contain exactly the legacy `WIRE_KEYS`. Explicit loopback
requests may additionally carry `origin_sink`, `rule_source_id`,
`source_platform`, `source_surface`, and `excerpt`. Evaluation source and rule
version are recorded when the fire is created; uploading a cloud-evaluated fire
to local storage never relabels it as locally evaluated. Older rows without
source evidence remain unknown. Newly evaluated fires record the known Claude
platform; an explicit source surface or entrypoint is preserved with the same
field precedence as conversation capture, while an unobserved surface remains unknown.
Excerpts are redacted using the existing token
redactor before being capped at 2,048 Unicode characters. This is shape-based
redaction, not a guarantee against arbitrary sensitive text. Projection never
rewrites the shared ledger, and remote payloads never include excerpts or these
local extensions.

The local receiver owns retention: metadata-only mode must reject new excerpt
and ask-result content even on enrichment, without erasing existing content.
The disposable receivers in `tests/rulebook_sinks_test.py` model that contract;
they do not implement or certify the desktop's rule-event intake. Installing
this plugin change alone does not provide a local server or alter its policy.

Validation: `python3 tests/rulebook_sinks_test.py` covers real executable hooks,
replay accounting, rejected rows, independent recovery/quarantine/throttling,
legacy state adoption, both projection orders, bounded/redacted excerpts,
metadata-only receiver behavior and slow/locked cloud isolation. Existing
`rulebook_client_test.py` keeps the direct legacy transport contract, and
`capture_health_test.py` keeps cloud-service warnings covered. The complete
`bash scripts/check-plugin.sh` gate exercises all suites with bare Python and
the MCP SDK.
