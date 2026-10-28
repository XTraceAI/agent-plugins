"""Bounded pages in final canonical order, sharing Codex conversion and usage.

One flat rollout or one paginated rollout is supported: an original root, or a
fork whose history the planner resolved to a rollout of the session it was
forked from. That other rollout is never part of the page: only its referenced
prefix's usage counters are read, forward through one validated descriptor, so
the fork's first usage delta is its own work. A metadata pass finds final
record IDs after the shared ledger overlay; a second pass retains only those
bodies. Rows are replayed from a validated descriptor, never staged.
"""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path

from readers import codex_history, codex_witness
from readers.strict_json import loads
from readers.jsonl import open_lines, readline_bytes

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SCAN_BYTES = 2 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_PASSES = 32
MAX_RECORDS = 200
MAX_RETAINED_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
CONTEXT_BEFORE = 12
# Read-ahead for a fork's referenced prefix: a 100 MB prefix is about a hundred
# reads, and the prefix is never held whole.
PREFIX_BUFFER_BYTES = 1024 * 1024
# One line of a fork's referenced prefix: the parent can hold large tool outputs
# (screenshots); only its counters are read, one line at a time. The fork's own
# rows keep the exact-detail line limit.
MAX_PREFIX_LINE_BYTES = 64 * 1024 * 1024


class ContextRefusal(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def selection(text):
    try:
        value = loads(text, strict=True)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError()
        kind = value.get("kind")
        keys = {"version", "kind"}
        if kind in ("before", "after"):
            keys |= {"position", "generation"}
            if type(value.get("position")) is not int or not 0 <= value["position"] <= 100_000:
                raise ValueError()
        elif kind == "anchor":
            keys |= {"record_uuid", "block", "expected", "generation"}
            if not isinstance(value.get("record_uuid"), str) or not 0 < len(value["record_uuid"]) <= 4096:
                raise ValueError()
            if value.get("block") is not None and (type(value["block"]) is not int or not 0 <= value["block"] <= 2**32 - 1):
                raise ValueError()
            if value.get("expected") not in ("message", "tool_call", "tool_block"):
                raise ValueError()
        elif kind != "first":
            raise ValueError()
        generation = value.get("generation")
        if generation is not None and (not isinstance(generation, str) or len(generation) != 64 or
                any(c not in "0123456789abcdef" for c in generation)):
            raise ValueError()
        if set(value) != keys or (kind in ("before", "after") and generation is None):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ContextRefusal("context_invalid_cursor") from None


class Window(io.RawIOBase):
    """An unbuffered descriptor read up to its observed length, every byte charged.

    Reading stops at the observed length; a file that ends earlier has shrunk.
    """
    def __init__(self, handle, length, charge, budget, api):
        super().__init__()
        self.handle, self.length, self.charge, self.budget, self.api = handle, length, charge, budget, api

    def readable(self):
        return True

    def readinto(self, buffer):
        self.budget.check()
        remaining = self.length - self.handle.tell()
        if not remaining:
            return 0
        read = self.handle.readinto(memoryview(buffer)[:remaining])
        if not read:
            raise self.api.SourceChanged("source shrank")
        self.charge(read)
        return read


class Prefix:
    """A referenced rollout read forward line by line, for its prefix counters.

    Buffered, so a long prefix is not one system call per byte. Every byte is
    charged to the page's scan counter, each line is held to the prefix line
    bound and counted as a native row through ``count``, and the deadline is
    checked per line. Lines end at a newline only, as
    ``codex_history.prefix_usage`` requires.
    """
    def __init__(self, handle, length, charge, count, budget, api):
        self.count, self.budget, self.api = count, budget, api
        self.lines = io.BufferedReader(Window(handle, length, charge, budget, api),
                                       buffer_size=PREFIX_BUFFER_BYTES)

    def readline(self):
        self.budget.check()
        raw = self.lines.readline(MAX_PREFIX_LINE_BYTES + 1)
        if len(raw) > MAX_PREFIX_LINE_BYTES:
            raise self.api.DetailLimit("detail_limit_line_bytes")
        if raw:
            self.count()
        return raw


class Rows:
    """An index of complete row byte offsets, with bounded seek/read replay."""
    def __init__(self, handle, length, budget, api):
        self.handle, self.length, self.budget, self.api = handle, length, budget, api
        self.scanned = self.passes = 0
        self.offsets = []
        handle.seek(0)
        offset = count = 0
        self.count = 0
        with open_lines(Window(handle, length, self.charge, budget, api)) as lines:
            while offset < length:
                budget.check()
                try:
                    raw = readline_bytes(lines, budget.max_line_bytes)
                except ValueError:
                    raise api.DetailLimit("detail_limit_line_bytes") from None
                if not raw:
                    raise api.SourceChanged("source shrank")
                count += 1
                self.count = count
                if count > budget.max_native_rows:
                    raise api.DetailLimit("detail_limit_native_rows")
                try:
                    row = loads(raw.decode("utf-8").strip(" \t\r\n"), strict=True) if raw.strip(b" \t\r\n") else None
                except json.JSONDecodeError:
                    if raw.endswith((b"\n", b"\r")):
                        raise
                    offset += len(raw)
                    continue
                if raw.strip(b" \t\r\n"):
                    if not isinstance(row, dict):
                        raise ValueError("native row is not an object")
                    self.offsets.append((offset, len(raw)))
                offset += len(raw)

    def charge(self, size):
        self.budget.check()
        self.scanned += size
        if self.scanned > MAX_SCAN_BYTES:
            raise self.api.DetailLimit("detail_limit_source_bytes")

    def line(self, span):
        offset, length = span
        self.handle.seek(offset)
        raw = bytearray()
        while len(raw) < length:
            self.budget.check()
            part = self.handle.read(min(length-len(raw), 65536))
            if not part:
                raise self.api.SourceChanged("source shrank")
            self.charge(len(part))
            raw.extend(part)
        return bytes(raw)

    def __len__(self):
        return len(self.offsets)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return (self[i] for i in range(*index.indices(len(self))))
        raw = self.line(self.offsets[index])
        row = loads(raw.decode("utf-8"), strict=True)
        if not isinstance(row, dict):
            raise ValueError("native row is not an object")
        return row

    def __iter__(self):
        self.passes += 1
        if self.passes > MAX_PASSES:
            raise self.api.DetailLimit("detail_limit_native_rows")
        return (self[i] for i in range(len(self)))


class RetainedOwnership:
    """Encoded metadata ownership, not a claim about exact Python heap use."""
    def __init__(self, api, budget, body_owned):
        self.api, self.budget, self.body_owned = api, budget, body_owned
        self.bytes = 0
        self.records = {}
        self.bodies = {}

    def guard(self, record, response_key=None, total=None):
        self.budget.check()
        uuid = record["uuid"]
        size = len(self.api.encode({**record, "message": {**record["message"], "content": []}})) + 1
        prior = self.records.get(uuid, 0)
        # Keep the high-water charge: removed records can remain referenced by
        # the history reconciler, and must not fund another retained response.
        record_size = max(prior, size)
        response_size = (len(self.api.encode([response_key, total])) + 1
                         if response_key is not None else 0)
        proposed_bytes = self.bytes + record_size - prior + response_size
        bodies = {key: value for key, value in self.bodies.items() if self.body_owned(key)}
        if self.body_owned(uuid):
            bodies[uuid] = len(self.api.encode(record)) + 1
        if proposed_bytes > MAX_METADATA_BYTES:
            raise self.api.DetailLimit("detail_limit_output_bytes")
        if sum(bodies.values()) > MAX_RETAINED_BYTES:
            raise ContextRefusal("context_record_too_large")
        # Atomic admission: all checks pass before any accounting is committed.
        self.bytes = proposed_bytes
        self.records[uuid] = record_size
        self.bodies = bodies


def read(reader, path, revision, header, request, budget, api, group=None, references=()):
    """One page of ``path``'s session; ``references`` as ``codex_history.references``."""
    sid = header["native_session_id"]
    base = None
    if group is not None:
        if len(group) != 1:
            raise api.DetailUnsupported("context requires one rollout")
        base = group[0][4]
    # A history reference is read only when discovery resolved the rollout it
    # names; the shared read validates the rest of the chain.
    if (base is None) != (not references) or (base is not None and references[-1][0] != base["thread_id"]):
        raise api.DetailUnsupported("context requires a resolved history reference")
    referenced = {str(item[1]) for item in references}
    digest = hashlib.sha256(json.dumps(["memhub.codex.context.v1", sid, str(path), revision],
                                     separators=(",", ":")).encode()).hexdigest()
    if request.get("generation") is not None and request["generation"] != digest:
        raise ContextRefusal("context_changed")
    with api.regular_source(path, api.revision_identity(revision, path), buffered=False) as (handle, observed):
        budget.charge_file(path, observed)
        rows = Rows(handle, observed.st_size, budget, api)
        opening = rows[0] if rows else {}
        payload = opening.get("payload", {})
        if ((base is None and payload.get("forked_from_id") is not None) or
                payload.get("history_base") != base or
                payload.get("subagent_history_start_ordinal") not in (None, 0, 1)):
            raise api.DetailUnsupported("context requires one rollout without inherited context")
        if group is None:
            names = codex_witness.rollout_name(path.name)
            if names != (sid, None) or not codex_witness.flat_header(opening, sid):
                raise api.DetailUnsupported("context requires one flat rollout")
        else:
            # The shared planner has already proved a single root, original or
            # forked. The shared read validates every row ordinal and ledger.
            if opening.get("ordinal") != (0 if base is None else base["end_ordinal_exclusive"]):
                raise ValueError("invalid root ordinal")

        @contextmanager
        def acquire(source, host, source_revision):
            if str(source) == str(path):
                yield rows
                return
            if str(source) not in referenced:
                raise api.DetailUnsupported("context acquires only its own sources")
            # A referenced rollout of another session: opened once per pass
            # through the identity discovery anchored, charged against this
            # page's source cap, and read forward only as far as its prefix.
            source = Path(source)
            with api.regular_source(source, api.revision_identity(source_revision, source),
                                    buffered=False) as (prefix, seen):
                budget.charge_file(source, seen)
                yield Prefix(prefix, seen.st_size, rows.charge, count_row, budget, api)
                after = os.fstat(prefix.fileno())
                if api.identity(after) != api.identity(seen) or api.file_stamp(after) != api.file_stamp(seen):
                    raise api.SourceChanged("referenced history changed")

        # Native rows one pass reads: the fork's own and its referenced prefix
        # lines, as the whole read counts them. Each pass starts again.
        pass_rows = rows.count

        def count_row():
            nonlocal pass_rows
            pass_rows += 1
            if pass_rows > budget.max_native_rows:
                raise api.DetailLimit("detail_limit_native_rows")

        start = end = target = None
        positions = {}
        def normalize(selected):
            nonlocal pass_rows
            pass_rows = rows.count
            bodies = 0
            retained = []
            ownership = RetainedOwnership(api, budget, lambda uuid: uuid in selected and
                start <= positions[uuid] < end)
            def project(record):
                nonlocal bodies, start, end
                ordinal = positions.get(record["uuid"])
                if record["uuid"] in selected and start <= ordinal < end:
                    size = len(api.encode(record)) + 1
                    retained.append((ordinal, record, size))
                    bodies += size
                    while bodies > MAX_RETAINED_BYTES:
                        front = request["kind"] == "before" if target is None else (
                            target - retained[0][0] > retained[-1][0] - target)
                        index = 0 if front else -1
                        removed, item, size = retained.pop(index)
                        if removed == target:
                            raise ContextRefusal("context_record_too_large")
                        item["message"]["content"] = []
                        bodies -= size
                        if front:
                            start = removed + 1
                        else:
                            end = removed
                else:
                    record["message"]["content"] = []
                ownership.guard(record)
            if group is None:
                records, native = reader.rollout_to_claude_records(
                    rows, strict=True, title_index={}, record_guard=budget.hold_records,
                    record_projection=project, retention_guard=ownership.guard)
            else:
                records, native = codex_history.read(group, acquire, title_index={},
                    record_guard=budget.hold_records, row_provider=lambda source: source,
                    record_projection=project, retention_guard=ownership.guard,
                    references=references)
            if native["session_id"] != sid:
                raise api.SourceChanged("native identity changed")
            return records, native

        planned, _ = normalize(set())
        total = len(planned)
        kind, position, target = request["kind"], request.get("position", 0), None
        if kind == "anchor":
            matches = [i for i, record in enumerate(planned) if record["uuid"] == request["record_uuid"]]
            if len(matches) != 1:
                raise ContextRefusal("context_target_missing" if not matches else "context_target_ambiguous")
            target = matches[0]
            start, end = max(0, target - CONTEXT_BEFORE), min(total, target + MAX_RECORDS - CONTEXT_BEFORE)
        elif kind == "before":
            if not 0 < position <= total:
                raise ContextRefusal("context_invalid_cursor")
            start, end = max(0, position - MAX_RECORDS), position
        else:
            if position > total or (kind == "after" and position == total and total):
                raise ContextRefusal("context_invalid_cursor")
            start, end = position, min(total, position + MAX_RECORDS)
        chosen = {record["uuid"] for record in planned[start:end]}
        expected = [record["uuid"] for record in planned]
        positions = {uuid: i for i, uuid in enumerate(expected)}
        del planned
        records, native = normalize(chosen)
        if [record["uuid"] for record in records] != expected:
            raise api.SourceChanged("canonical order changed between passes")
        if target is not None:
            record = records[target]
            block, wanted = request["block"], request["expected"]
            content = record["message"].get("content")
            block_kind = (content[block].get("type") if isinstance(content, list) and
                          block is not None and block < len(content) else None)
            if not (block is None if wanted == "message" else block_kind == "tool_use" if
                    wanted == "tool_call" else block_kind in ("tool_use", "tool_result")):
                raise ContextRefusal("context_block_missing")
        if (api.identity(os.fstat(handle.fileno())) != api.identity(observed) or
                api.file_stamp(os.fstat(handle.fileno())) != api.file_stamp(observed)):
            raise api.SourceChanged("source changed")
    records = records[start:end]
    if total and not records:
        raise ContextRefusal("context_record_too_large")
    while True:
        metadata = {"version": 1, "digest": digest, "start": start, "end": end,
                    "total": total, "target": target}
        result_header = {**header, "title": native.get("title"), "context_page": metadata}
        encoded = "\n".join(map(api.encode, [result_header, *records])) + "\n"
        budget.check()
        if len(encoded) <= min(MAX_OUTPUT_BYTES, budget.max_output_bytes):
            return encoded
        if len(records) <= 1:
            raise ContextRefusal("context_record_too_large")
        front = kind == "before" if target is None else target - start > end - 1 - target
        if front:
            records.pop(0)
            start += 1
        else:
            records.pop()
            end -= 1
