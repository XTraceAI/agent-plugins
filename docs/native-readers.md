# Reading native session files

A native reader understands the session files an agent already writes locally.
The Codex and Cursor readers reuse their existing normalization code. Callers
that need explicit read failures can enable one checked-read mode:

```python
records, metadata = reader.to_canonical(path, strict=True)
```

Checked mode adds validation to the existing normalization. It rejects invalid UTF-8
text, malformed complete JSON rows, non-finite numbers, and Cursor content
shapes the normalizer cannot preserve. Existing Cursor blob hashes, references
and framing are checked before returning the tree. Original files are not
edited, and healthy inputs keep their IDs, usage and timestamps. Checked Codex
reads require a nonempty native session ID before generating record identities.

For JSONL, CR/LF terminates a committed row. An unparseable final fragment
without that delimiter is deferred until a later read, even if its current
bytes cannot be repaired solely by appending. This conservative framing rule
does not attempt incremental JSON-prefix validation. A parseable final row
is consumed and validated even without a delimiter; malformed terminated rows
and invalid UTF-8 still fail.

The Codex adapter retains its existing text/tool projection. Image and other
non-text message blocks are outside that projection; their presence does not
make an otherwise readable session fail. Checked mode validates consumed text
and tool fields, not complete multimodal transcript fidelity. Tool input must
be an object or string, which the normalizer can retain without dropping it.

Cursor store metadata supports the native plain-JSON and hex-encoded JSON
representations. Checked reads validate the decoded tree. Hex metadata retains
the existing insertion-order projection and nullable clocks because changing
that projection would reassign content to previously captured record IDs.

This mode checks the supported input structure; it does not certify that every
optional measurement is available. Missing or unusable native token counters
remain unmeasured, not zero. A readable session is retained when only its usage
sample is unusable. Tool calls need usable names/identifiers so normalization
cannot silently invent a tool identity or disconnect its result. A malformed
working-directory value or creation-time type fails the checked store read
because copying it could assign the wrong project or a fabricated date.

Checked Cursor reads preserve valid usage from empty assistant messages as
usage-only records. Those records have separate stable IDs, preserving existing
record and tool identities. Native lifecycle events remain ignorable; an
unsupported message role reports incomplete input instead of hiding a message.

`session_metadata(path)` reads native identity, surface, start and repository
metadata without deriving a title from a prompt. Missing facts remain unknown.
Checked Codex metadata validates the bounded prefix through its first header;
it leaves later body validation to the complete reader. CR, LF and CRLF are
supported with per-record byte bounds. Cursor schema versions must be integers.
Returned text fields accept text or null, and Codex start times retain their
timezone-aware ISO timestamp. A present git container must be an object.
`list_sessions(..., on_error=callback)` reports inaccessible or skipped sources;
symlinks and non-regular files such as FIFOs are reported and skipped. Callers
should keep healthy sessions available while reporting failures for other files.

`cursor_flush.apply_session_state(records, native_id, strict=True)` restores
saved observations and rejects malformed saved state. Missing optional files
and explicitly unmeasured clock pins are valid. Reads do not advance upload
progress or rewrite saved observations.

Checked parsing does not own SQLite snapshots. A consumer that must avoid
native WAL/shared-memory changes supplies a private snapshot. This PR adds no
reader service, alternate text encoding, full schema validator or repair pass.
The standalone command and snapshots are in #198; Desktop indexing is separate.

Run `python3 tests/readers_validation_test.py` and the existing plugin test
contract. Corruption fixtures demonstrate behavior under synthetic failures;
they are not claims that real user session files were found corrupted.


### Paginated Codex history

The read-only CLI groups validated paginated rollout files that share one native
session ID. It emits each physical rollout's work once, including attempts beyond
a later rewind cutoff. Persisted `token_usage_record` entries provide per-response
accounting even when UI token meters reset. Response IDs deduplicate repeated
ledger entries. From the first ledger entry onward, ledger usage replaces the
parallel UI counters; any older legacy-only prefix keeps its existing handling.
Without a ledger, `history_base` supplies the legacy cumulative baseline when
available; an unavailable baseline leaves the first delta unknown.

Original rollout record IDs retain their existing namespace. Continuations use
the immutable rollout ID within the session namespace. A separate usage overlay
preserves real-record IDs and order, reuses existing usage-only identities where
possible, and adds stable per-response identities for otherwise unrepresented
inferences. Appending a continuation or replaying does not renumber real work. The output has one
session header with the original start time. File snapshots and final revision
checks cover every group member. Ancestors precede their continuations; sibling
rollouts use native start-time order with immutable IDs breaking ties. Paginated
members without a native start time are rejected rather than given an invented
chronology. The last native title update across that order wins; the sidecar is
consulted only when no member contains a native title.

A group must have one original and valid, acyclic references to discovered
rollouts. Missing references, duplicate immutable IDs, inconsistent byte/ordinal
bounds or malformed records remain explicit failures. An explicit paginated path
selects its whole same-session group: the configured native sessions root is
searched for native files, or only the containing directory for exported files.
Keep original rollout filenames in exports. Selecting either the root or a
continuation returns the same group. Missing
parents or unsafe discovery prevent that explicit group from being emitted.
This does not add a native database dependency or change cloud capture.

Regression: `python3 tests/codex_history_test.py` covers abandoned work, inherited
usage, meter resets, response-ledger deduplication, replay/legacy IDs, source
changes, and invalid/incomplete lineage.
