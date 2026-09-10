# Native reader validation

The existing Codex and Cursor readers expose opt-in strict reads for consumers
that must distinguish a damaged source from complete coverage:

```python
records, metadata = reader.to_canonical(path, strict_utf8=True, strict_json=True)
```

Strict reads reject invalid UTF-8 and malformed complete JSON rows. Cursor JSONL
rows must be objects, and recognized message roles need an object message body.
An unfinished final JSON record remains deferred because native writers append
their transcripts. Existing capture callers retain the default tolerant behavior.
Canonical record IDs, usage normalization and timestamp rules are unchanged.

`session_metadata(path)` returns native identity, raw observed surface, native
start and repository metadata without deriving a title from a prompt. Missing
surface or start remains unknown. `list_sessions(..., on_error=callback)` reports
unreadable or skipped discovery sources; symlinked subdirectories are reported
rather than followed into cycles.

`cursor_flush.apply_session_state(records, native_id, strict=True)` restores
saved observations while rejecting malformed existing state. Missing optional
files and explicitly unmeasured timestamp pins remain valid. Reads do not advance
upload progress or rewrite saved observations.

Strict decoding is separate from SQLite snapshot ownership: a consumer that must
avoid native WAL/shared-memory changes must supply a private snapshot. This API
change adds no standalone stream command, destination selection or network call.

Run `python3 tests/readers_validation_test.py` for strict/legacy parity, malformed
rows, incomplete tails, byte decoding, saved observations, discovery errors and
native metadata. Existing reader, usage and timestamp suites remain required.
