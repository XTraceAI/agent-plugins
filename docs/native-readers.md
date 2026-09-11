# Reading native session files

A native reader understands the session files an agent already writes locally.
The Codex and Cursor readers reuse their existing normalization code. Callers
that need explicit read failures can enable one checked-read mode:

```python
records, metadata = reader.to_canonical(path, strict=True)
```

Default capture behavior stays unchanged. Checked reads reject invalid UTF-8
text, malformed complete JSON rows, non-finite numbers, and Cursor content
shapes the normalizer cannot preserve. Existing Cursor blob hashes, references
and framing are checked before returning the tree. An unfinished final JSON
record is deferred because native writers append their transcripts. Original
files are not edited, and healthy inputs keep their IDs, usage and timestamps.

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
`list_sessions(..., on_error=callback)` reports inaccessible or skipped sources;
symlinked subdirectories are reported rather than followed into cycles. Callers
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
