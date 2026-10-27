#!/usr/bin/env python3
"""Read-only native session headers and canonical JSONL for local consumers."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import datetime
import hashlib
import io
import itertools
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from readers import codex_history, codex_origin, codex_witness, reader_for, validate_canonical
from codex_context import ContextRefusal
from readers.strict_json import loads as load_json
from readers.jsonl import readline_bytes


def session_selector(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("--session requires a native session ID, latest, or a path")
    return value


def since_instant(value: str) -> float:
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as error:
        raise argparse.ArgumentTypeError("since must be a timestamp with a timezone") from error


def deadline_seconds(value: str) -> float:
    """How long an exact-detail read may take before it refuses.

    The parent process owns the hard deadline -- it can kill what this one
    cannot interrupt. This is the same instant observed from inside, so a read
    that is still working stops at a declared boundary instead of being
    stopped at an arbitrary one.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("deadline must be a number of seconds") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            "deadline must be a finite, non-negative number of seconds")
    return parsed


def native_text(value, *, required=False):
    if not required and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("native identity must be text")
    return value


class SourceChanged(ValueError):
    """A native source or sidecar is no longer the file this run validated."""


def identity(observed) -> tuple:
    return observed.st_dev, observed.st_ino


def entry_identity(entry) -> tuple:
    return entry[4], entry[3]


def file_observation(item: Path, observed) -> tuple:
    """One file as a revision entry: name, size, mtime, identity and ctime.

    The change time is last so every existing positional use still holds. It
    is what shows a same-length rewrite whose mtime was put back (see
    ``file_stamp``) -- an operational change detector, not a proof of bytes.
    """
    return (str(item), observed.st_size, observed.st_mtime_ns, observed.st_ino, observed.st_dev,
            observed.st_ctime_ns)


def basis_unchanged(basis) -> bool:
    """Re-observe the file a ranking was based on; absent or altered is a change."""
    item, expected = basis
    try:
        with regular_source(item) as (_, observed):
            return file_observation(item, observed) == expected
    except (OSError, ValueError):
        return False


def revision_identity(revision, path: Path):
    """The identity a revision recorded for one file, or None when absent."""
    return next((entry_identity(item) for item in revision if item[0] == str(path)), None)


@contextmanager
def regular_source(path: Path, expect=None, *, buffered=True):
    """Open one source observation without following or blocking on special files.

    ``expect`` is the identity an earlier observation of this run recorded for
    the same name; a different file there is a source change, not new input.
    ``buffered=False`` yields the descriptor unbuffered, for a caller that must
    account for every byte read from the file: a buffered handle reads ahead
    into memory before its caller has asked for, or been charged for, a byte.
    """
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("native source sidecar is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or identity(before) != identity(observed):
            raise ValueError("native source changed before it was opened")
        if expect is not None and identity(observed) != expect:
            raise SourceChanged("native source is no longer the validated file")
        with os.fdopen(descriptor, "rb", buffering=-1 if buffered else 0) as handle:
            descriptor = -1
            yield handle, observed
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def source_revision(path: Path, host: str, expect=None) -> tuple:
    """Observe the source and its sidecars; ``expect`` anchors the source itself."""
    paths = [path]
    if path.name == "store.db":
        # SQLite can keep current changes in WAL; --since must not skip them.
        paths += [path.with_name("store.db-wal"), path.with_name("store.db-journal"),
                  path.parent / "meta.json"]
    if host == "cursor":
        from cursor_flush import _state_path, _UUID_RE
        sid = path.parent.name if path.name == "store.db" else path.stem
        if _UUID_RE.fullmatch(sid):
            # A hook can add usage/timestamp pins after the native file stops
            # changing. Those observations must participate in --since too.
            paths.append(_state_path(sid))
    revision = []
    for item in paths:
        try:
            with regular_source(item, expect if item == path else None) as (_, observed):
                pass
        except FileNotFoundError:
            if item == path:
                raise
            continue
        revision.append(file_observation(item, observed))
    return tuple(revision)


def cursor_sid(path: Path) -> str:
    return path.parent.name if path.name == "store.db" else path.stem


def cursor_selection(path: Path, *, select_saved=False, anchors=None, budget=None):
    """Never restore index-derived pins onto a different representation.

    Returns ``(source, state, observation)``: the saved state this call
    validated and parsed (``{}`` when the file was absent) and the state
    file's identity as seen by the very read that made this selection, in
    source_revision()'s shape (``None`` when absent). The caller binds the
    selection to its revision baseline through that observation and applies
    the state without any path being reopened afterwards.

    ``budget`` is an exact-detail read's ceilings. The pin is what chooses the
    representation, so its bytes are part of what that read spends and are
    charged and bounded here rather than read unmeasured.
    """
    from cursor_flush import _read_state, _state_path, _UUID_RE, _valid_transcript_path
    from readers import cursor as cursor_reader
    sid = cursor_sid(path)
    if not _UUID_RE.fullmatch(sid):
        return path, {}, None
    saved_text = None
    seen = None
    try:
        with regular_source(_state_path(sid)) as (handle, observed):
            # Parse these exact bytes: reopening by path would let another
            # process swap in a FIFO or symlink after the check.
            saved = (handle.read() if budget is None
                     else checked_read(_state_path(sid), handle, observed, budget))
            saved_text = saved.decode("utf-8")
            seen = file_observation(_state_path(sid), observed)
    except FileNotFoundError:
        # Absent stays absent. Falling through to _read_state would reopen the
        # path, and a FIFO created in the meantime would block there.
        pass
    state = {} if saved_text is None else _read_state(sid, strict=True, text=saved_text)
    kind = state.get("source_kind")
    if kind is None:
        if state.get("usage_events") or any(state.get("record_ts", {}).values()):
            raise ValueError("saved observations have no source provenance")
        return path, state, seen
    if kind == "store":
        # The legacy locator also accepts a working-directory entry named
        # after the UUID; only the native chats root may decide what a saved
        # pin points at.
        if select_saved:
            # Selecting: resolve the pinned store the way safe discovery does,
            # from regular store.db files below unaliased directories, so an
            # alias discovery reports and skips cannot make the pin ambiguous.
            from readers.discovery import paths as safe_paths
            stores = [discovered_path(cursor_reader, item, anchors)[0]
                      for item in safe_paths(cursor_reader._CHATS,
                                             ("*", sid, "store.db"),
                                             lambda error: None,
                                             None if budget is None else budget.observe)]
        else:
            # The explicit source is authorized, but store-only saved state has
            # no path provenance. Keep duplicate-store ambiguity checks before
            # attaching those observations to the chosen representation.
            stores = list(dict.fromkeys(
                item.resolve(strict=True)
                for item in cursor_reader._CHATS.glob(f"*/{sid}/store.db")
                if item.is_file()))
        saved = stores[0] if len(stores) == 1 else None
    elif kind == "transcript":
        if select_saved:
            root = anchors.get(cursor_reader._PROJECTS) if anchors else None
            saved, _ = _valid_transcript_path(state.get("transcript_path"), sid, root=root)
        else:
            raw = state.get("transcript_path")
            candidate = Path(raw).expanduser() if isinstance(raw, str) else None
            if (candidate is None or not candidate.is_absolute()
                    or candidate.parent.name != sid or candidate.name != f"{sid}.jsonl"):
                saved = None
            else:
                try:
                    saved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    saved = None
    else:
        saved = None
    if saved is not None:
        if select_saved or saved == path:
            return saved, state, seen
    raise ValueError("saved observations belong to another source")


def cursor_source(path: Path, *, select_saved=False, want_state=False):
    source, state, _ = cursor_selection(path, select_saved=select_saved)
    return (source, state) if want_state else source


def state_observation(revision: tuple, path: Path):
    """The state file entry a source revision recorded for this session."""
    from cursor_flush import _state_path
    expected = str(_state_path(cursor_sid(path)))
    return next((item for item in revision if item[0] == expected), None)


def discovery_roots(reader) -> list[Path]:
    if reader.HOST == "codex":
        return [reader._SESSIONS]
    return [reader._CHATS, reader._PROJECTS]


def root_anchors(reader, on_error=None) -> dict:
    """Where each configured root led before discovery ran.

    A configured root may be a stable symlink. Its target is recorded here,
    before list_sessions(), so a root retargeted after discovery cannot make a
    same-named file under the new target pass as the discovered one. An
    absent root simply has no anchor; a root that exists but cannot be
    resolved (unreadable, or a symlink loop, which raises RuntimeError before
    Python 3.13) is reported through ``on_error`` as incomplete discovery,
    and rows under it are rejected rather than resolved through the loop.
    """
    anchors = {}
    for root in discovery_roots(reader):
        try:
            anchors[root] = root.resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as error:
            if on_error is not None:
                on_error(error)
    return anchors


def discovered_path(reader, path, anchors) -> tuple[Path, tuple]:
    """Resolve one discovered row without following an alias below its root.

    Discovery lstat()s every component beneath a configured root and reports
    aliases as incomplete. A symlink swapped in after that observation must
    not be followed here either, or the command would validate and emit the
    external target under a discovered name. The root itself may be a
    configured symlink, so components stop at the root; the resolved file
    must then sit exactly where the root led before discovery. The recheck
    and the resolution are separate operations, so the result is also
    anchored to the identity the recheck observed: whatever resolve()
    returns must be that very file, and the identity is returned for later
    reads to demand.
    """
    path = Path(path)
    expected = None
    for root in discovery_roots(reader):
        if path.is_relative_to(root):
            components = [item for item in (path, *path.parents) if item != root
                          and item.is_relative_to(root)]
            if root not in anchors:
                raise ValueError("discovered source root appeared after discovery")
            expected = anchors[root] / path.relative_to(root)
            break
    else:
        components = [path]
    leaf = None
    for item in components:
        observed = item.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise ValueError("discovered source became an alias")
        if item == path:
            leaf = observed
    if leaf is None or not stat.S_ISREG(leaf.st_mode):
        raise ValueError("discovered source is not a regular file")
    resolved = path.resolve(strict=True)
    if expected is not None and resolved != expected:
        raise ValueError("discovered source root was retargeted after discovery")
    after = resolved.lstat()
    if not stat.S_ISREG(after.st_mode) or identity(after) != identity(leaf):
        raise ValueError("discovered source changed while it was resolved")
    return resolved, identity(leaf)


def discovered_source(reader, path: Path, discovered, anchors):
    """The anchored identity of the discovered row resolving to ``path``, or None."""
    for row in discovered:
        try:
            resolved, anchor = discovered_path(reader, row["path"], anchors)
        except (OSError, ValueError, RuntimeError):
            continue
        if resolved == path:
            return anchor
    return None


def header_for(reader, path: Path, mtime: float, native=None) -> dict:
    if native is None:
        native = reader.session_metadata(path)
    sid = native_text(native.get("session_id"), required=True)
    start = native_text(native.get("started_at"))
    if start is not None:
        since_instant(start)  # Validate but preserve the original fractional precision.
    return {"type": "session", "host": reader.HOST, "native_session_id": sid,
            "conversation_id": f"{reader.HOST}-{sid}",
            "source_surface": native_text(native.get("source_surface")),
            "started_at": start, "cwd": native_text(native.get("cwd")),
            "git_branch": native_text(native.get("git_branch")), "title": None,
            "path": str(path), "mtime": mtime}


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def historical_titles(reader, session_ids):
    """Read the complete title index once, keeping only selected identities."""
    found = {}
    try:
        source = regular_source(reader._SESSION_INDEX)
    except FileNotFoundError:
        return found
    try:
        with source as (binary, _), io.TextIOWrapper(
                binary, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            while raw := readline_bytes(handle, reader._INDEX_TAIL_BYTES):
                text = raw.decode("utf-8")
                if not text.strip():
                    continue
                try:
                    row = load_json(text, strict=True)
                except json.JSONDecodeError:
                    if raw.endswith((b"\n", b"\r")):
                        raise
                    break
                if not isinstance(row, dict):
                    raise ValueError("Codex title index row is not an object")
                sid, name = row.get("id"), row.get("thread_name")
                if isinstance(sid, str) and sid in session_ids:
                    if name is not None and not isinstance(name, str):
                        # A matching row is the native title; ignoring a
                        # malformed one would hide it behind a derived name.
                        raise ValueError("Codex title index row has a malformed thread name")
                    if isinstance(name, str) and name.strip():
                        found[sid] = (reader._one_line(name), row.get("updated_at"))
    except FileNotFoundError:
        return found
    return found


class TitleIndex:
    """Lazy complete lookup; revisions compare only the used fallback row."""
    def __init__(self, reader, session_ids):
        self.reader, self.session_ids = reader, session_ids
        self.stamp, self.rows = object(), {}

    def current_stamp(self):
        try:
            stat = self.reader._SESSION_INDEX.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_ino
        except FileNotFoundError:
            return None

    def get(self, sid):
        stamp = self.current_stamp()
        if stamp != self.stamp:
            rows = historical_titles(self.reader, self.session_ids)
            if stamp != self.current_stamp():
                raise ValueError("title index changed while reading")
            self.rows, self.stamp = rows, stamp
        return self.rows.get(sid, (None, None))

    def stamp_bound(self, observation):
        """True when this observation's time falls back to the index file's
        stamp, so a changed index invalidates it even if the row is identical."""
        title, updated = observation
        if title is None:
            return False
        try:
            since_instant(updated)
            return False
        except (TypeError, AttributeError, argparse.ArgumentTypeError):
            return True

    def mtime(self, observation):
        title, updated = observation
        if title is None:
            return 0
        try:
            return since_instant(updated)
        except (TypeError, AttributeError, argparse.ArgumentTypeError):
            # Older index rows carry no per-session timestamp. Only those
            # fallback consumers conservatively use the index file's mtime.
            return self.stamp[1] / 1_000_000_000 if self.stamp else 0


@contextmanager
def source_snapshot(path: Path, host: str, revision=()):
    # Every source is read from a private snapshot taken through the validated
    # descriptor, never by reopening the native path: a FIFO or symlink swapped
    # in after source_revision() would otherwise block the read or be followed
    # before the final revision check can report source_changed. Each copied
    # file must also be the very file the revision baseline observed, so a
    # swap that is reverted before the final comparison cannot feed the read.
    with tempfile.TemporaryDirectory(prefix="native-reader-") as temporary:
        # Stores derive identity from the session directory name; a source
        # directly under a filesystem root has no parent name, so the snapshot
        # always gets its own nonempty subdirectory.
        directory = Path(temporary) / (path.parent.name or "root")
        directory.mkdir(mode=0o700)
        if host != "cursor" or path.name != "store.db":
            # Codex rollouts and Cursor transcripts derive identity from the
            # file name, so the snapshot keeps <parent>/<name>. Only stores
            # carry a meta.json sidecar; an unrelated sibling of that name
            # beside a transcript is never consulted, so it is never copied.
            target = directory / path.name
            with regular_source(path, revision_identity(revision, path)) as (handle, _):
                target.touch(mode=0o600, exist_ok=False)
                with target.open("wb") as output:
                    shutil.copyfileobj(handle, output)
            yield target
            return
        # SQLite mode=ro may still create an SHM file. Copy stable source bytes
        # to a private snapshot so journal handling never writes in the store.
        for name in ("store.db", "store.db-wal", "store.db-journal", "meta.json"):
            source, target = path.parent / name, directory / name
            try:
                with regular_source(source, revision_identity(revision, source)) as (handle, _):
                    target.touch(mode=0o600, exist_ok=False)
                    with target.open("wb") as output:
                        shutil.copyfileobj(handle, output)
            except FileNotFoundError:
                if name in {"store.db", "meta.json"}:
                    raise
        # A hot rollback journal needs writes to recover the last committed
        # state. Permit recovery only in this owned copy, then let the existing
        # read-only native normalizer consume the recovered database.
        with closing(sqlite3.connect(directory / path.name)) as database:
            database.execute("PRAGMA schema_version").fetchone()
        yield directory / path.name


# --- exact session detail ---------------------------------------------------
#
# An opt-in mode for a consumer that needs one identified session's own text
# rather than an index of it. It differs from the ordinary export in exactly
# two ways, and both are the point: no part of a source is copied to disk, and
# every quantity the read can spend is declared here and checked before it is
# spent. Discovery, identity, ambiguity and revision baselines are the same
# code the ordinary export runs, and the records come out of the same
# converters -- this mode adds a way to feed them, not a second normalizer.

# Two budgets, because a read does two different kinds of work. Identifying
# candidates means reading a small header from every rollout discovery found,
# almost all of them other sessions; acquiring the selected session means
# holding its whole body. Charging the first against the second made one
# large unrelated history refuse a tiny selected session as though *it* were
# too large, so each has its own ceilings and its own honest reason.
#
# Body: the most source bytes one exact-detail read may hold, across the files
# it actually acquires -- the rollouts of the selected paginated group, a
# Cursor transcript, and the saved pin that chose it. Reserved before any of
# them is read.
DETAIL_MAX_SOURCE_BYTES = 24 * 1024 * 1024
# Body: the most distinct files one read may acquire. A paginated Codex session
# is a handful of rollouts; a group past this is refused rather than walked.
DETAIL_MAX_FILES = 64
# Probe: the most bytes one identity probe may consume. The reader's own header
# scan admits rows up to 1 MiB; twice that covers the read-ahead a text reader
# performs, so no header the reader would accept is refused here, while one
# probe can no longer cost what the scan alone allows (200 rows of 1 MiB).
DETAIL_MAX_PROBE_BYTES = 2 * 1024 * 1024
# Probe: identity probes one read may make. Discovery admits 4096 entries and a
# rollout costs at least two of them (its walk entry and its row), so this is
# the most a home discovery accepts can require; it binds a rescoped or
# repeated probe, and otherwise backs the entry ceiling rather than preceding it.
DETAIL_MAX_PROBES = 2048
# Probe: bytes all identity probes together may consume. Probe bytes are parsed
# and dropped -- only a header that names the requested session is kept -- so
# this bounds the work of identification, not memory held, which one probe's
# ceiling already bounds. Sized so the most probes allowed can each read 32 KiB:
# a real 19 KB session header plus one read-ahead chunk.
DETAIL_MAX_PROBE_TOTAL_BYTES = 64 * 1024 * 1024
# Native JSONL rows parsed, and canonical records held, for one session.
DETAIL_MAX_NATIVE_ROWS = 200_000
DETAIL_MAX_RECORDS = 100_000
# The encoded session, header line included. A session whose text does not fit
# is refused whole; this mode never writes a truncated session.
DETAIL_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
# One native row, matching the bound the Cursor transcript reader already
# applies, so this mode refuses no row the ordinary export would accept.
DETAIL_MAX_LINE_BYTES = 8 * 1024 * 1024
# Entries enumeration may consider before refusing -- directories and files as
# the walk reaches them, and every row built from its results. The walk is
# stopped from inside rather than judged after it finishes.
DETAIL_MAX_DISCOVERY_ENTRIES = 4096
# The deadline an exact-detail read gets when its caller names none. A read
# with no deadline at all is not bounded, whatever else is: work this process
# cannot interrupt is work the parent has to kill.
DETAIL_DEFAULT_DEADLINE_SECONDS = 30.0
# How often the deadline is consulted inside work that is already bounded but
# can still be long: one row in this many, rather than one clock read per row.
DETAIL_DEADLINE_STRIDE = 4096
# The emitted session header line. The native header probe is bounded
# separately by the reader that performs it.
DETAIL_MAX_HEADER_BYTES = 1024 * 1024


def detail_now() -> float:
    """The clock the deadline is measured on, named so tests can drive it."""
    return time.monotonic()


def file_stamp(observed) -> tuple:
    """What an exact read compares to decide a file is still the same file.

    Length and modification time say a file was written to. Change time says
    so as well, and says it for the one case the other two miss: a same-length
    rewrite whose modification time was put back afterwards, which a process
    can do while it cannot restore a change time. On Windows ``st_ctime`` is
    the creation time rather than the change time, so there the pair of the
    other two carries this comparison.
    """
    return observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns


class DetailLimit(ValueError):
    """A declared exact-detail ceiling was reached. ``code`` is the diagnostic."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DetailUnsupported(ValueError):
    """The selected representation is not the JSONL this mode is defined over."""


class DetailBudget:
    """What one exact-detail read may spend, counted before it is spent.

    Body bytes and files are charged per distinct file identity, so a file
    this read consults twice -- a Cursor pin re-read after the records are
    built, a Codex prefix re-read from the segment already acquired for it --
    costs what it costs once. Charging one identity a different length or
    change time is not a second file: it is that file changing underneath this
    read, and it is refused as a source change rather than paid for twice.

    Identity probes are charged separately, by the bytes they actually
    consume, and remember the stamp each file had when it was probed, so a
    file that changes between being identified and being acquired is refused
    the same way.
    """

    def __init__(self, deadline=None):
        if deadline is None:
            deadline = DETAIL_DEFAULT_DEADLINE_SECONDS
        self.deadline = detail_now() + deadline
        self.max_source_bytes = DETAIL_MAX_SOURCE_BYTES
        self.max_files = DETAIL_MAX_FILES
        self.max_native_rows = DETAIL_MAX_NATIVE_ROWS
        self.max_records = DETAIL_MAX_RECORDS
        self.max_output_bytes = DETAIL_MAX_OUTPUT_BYTES
        self.max_line_bytes = DETAIL_MAX_LINE_BYTES
        self.max_discovery_entries = DETAIL_MAX_DISCOVERY_ENTRIES
        self.max_header_bytes = DETAIL_MAX_HEADER_BYTES
        self.max_probe_bytes = DETAIL_MAX_PROBE_BYTES
        self.max_probes = DETAIL_MAX_PROBES
        self.max_probe_total_bytes = DETAIL_MAX_PROBE_TOTAL_BYTES
        self.files = {}
        self.probed = {}
        self.probes = 0
        self.probe_bytes = 0
        self.rows = 0
        self.records = 0
        self.entries = 0

    @property
    def spent_bytes(self) -> int:
        return sum(stamp[0] for stamp in self.files.values())

    def check(self) -> None:
        """Refuse before expensive work once the caller's deadline has passed."""
        if detail_now() >= self.deadline:
            raise DetailLimit("detail_deadline")

    def observe(self) -> None:
        """One enumerated entry, counted as enumeration reaches it.

        Passed to the readers' discovery as their per-entry hook, so a home
        with more history than this read may consider stops the walk instead
        of being measured once the walk has already finished.
        """
        self.entries += 1
        if self.entries > self.max_discovery_entries:
            raise DetailLimit("detail_limit_discovery_entries")
        self.check()

    def hold_records(self, count: int) -> None:
        """The records this read may hold, checked while they accumulate.

        The converters call this for every record they keep, which also makes
        it where the deadline lands inside a long conversion -- the other
        stretch of this read that is bounded in size but not in time.
        """
        self.records = max(self.records, count)
        if count > self.max_records:
            raise DetailLimit("detail_limit_records")
        if not count % DETAIL_DEADLINE_STRIDE:
            self.check()

    def count_probe(self) -> None:
        """Charge a probe without changing any earlier acquisition stamp."""
        self.check()
        self.probes += 1
        if self.probes > self.max_probes:
            raise DetailLimit("detail_limit_probes")

    def begin_probe(self, observed) -> None:
        """Count one identity probe and remember what the file was when probed."""
        self.count_probe()
        key, stamp = identity(observed), file_stamp(observed)
        if self.probed.setdefault(key, stamp) != stamp:
            raise SourceChanged("a source changed between two probes of it")

    def probe_allowance(self, consumed: int) -> int:
        """How many more bytes the current probe may read, before it reads them."""
        if consumed >= self.max_probe_bytes:
            raise DetailLimit("detail_limit_header_probe_bytes")
        if self.probe_bytes >= self.max_probe_total_bytes:
            raise DetailLimit("detail_limit_probe_bytes")
        return min(self.max_probe_bytes - consumed,
                   self.max_probe_total_bytes - self.probe_bytes)

    def charge_file(self, path: Path, observed) -> None:
        """Admit one file's body, or refuse before a single byte of it is read."""
        key = identity(observed)
        stamp = file_stamp(observed)
        if self.probed.get(key, stamp) != stamp:
            raise SourceChanged("a source changed between being identified and acquired")
        known = self.files.get(key)
        if known is not None:
            if known != stamp:
                raise SourceChanged("a source changed between two reads of it")
            return
        if len(self.files) >= self.max_files:
            raise DetailLimit("detail_limit_files")
        if observed.st_size > self.max_source_bytes - self.spent_bytes:
            raise DetailLimit("detail_limit_source_bytes")
        self.files[key] = stamp

    def charge_rows(self, rows: int) -> None:
        self.rows += rows
        if self.rows > self.max_native_rows:
            raise DetailLimit("detail_limit_native_rows")


def find_delimiter(data: bytes, token: bytes, start: int) -> int:
    """The next ``token`` at or after ``start``, or -1.

    Every delimiter search the row scan makes goes through here, so what the
    scan costs is the sum of the stretches these calls examine -- which is
    what makes "one pass over the bytes" a thing a test can count rather than
    a thing it has to time.
    """
    return data.find(token, start)


def bounded_rows(data: bytes, budget: DetailBudget) -> int:
    """Count native rows and bound each one before any of them is parsed.

    The split is the one both parsers frame on -- a row ends at LF, CR or CRLF
    -- and this is a scan over bytes already in memory, so an oversized row or
    an impossible row count is reached before a single JSON value is built.
    """
    rows = 0
    start = 0
    size = len(data)
    allowance = budget.max_native_rows - budget.rows
    # Each delimiter is searched for once and then followed forward: a cursor
    # is only re-sought when the row boundary has passed it, and the searches
    # that result cover disjoint stretches. Seeking both from every row start
    # instead would rescan the tail once per row -- one pass over the bytes,
    # not one per row.
    feed = find_delimiter(data, b"\n", 0)
    carriage = find_delimiter(data, b"\r", 0)
    while start < size:
        if 0 <= feed < start:
            feed = find_delimiter(data, b"\n", start)
        if 0 <= carriage < start:
            carriage = find_delimiter(data, b"\r", start)
        if feed < 0 and carriage < 0:
            end = size
        elif feed < 0 or 0 <= carriage < feed:
            end = carriage + 1
            if end < size and data[end] == 0x0A:
                end += 1  # CRLF is one boundary, not an empty row.
        else:
            end = feed + 1
        if end - start > budget.max_line_bytes:
            raise DetailLimit("detail_limit_line_bytes")
        rows += 1
        if rows > allowance:
            raise DetailLimit("detail_limit_native_rows")
        if not rows % DETAIL_DEADLINE_STRIDE:
            budget.check()
        start = end
    budget.charge_rows(rows)
    return rows


class ProbeReader(io.RawIOBase):
    """One source as an identity probe sees it: each read is admitted first.

    The header scan is the reader's own, unchanged; this is only the stream it
    reads from. Every request is cut down to what the probe may still consume
    before any byte is read into it, so neither one probe nor all of them
    together can read past their ceilings, and a header larger than a probe
    may hold is refused as that rather than as an oversized session. The end
    of the probe's view is the length observed at open: growth after that is a
    source change for the revision checks to report, not more header to read.
    The underlying descriptor belongs to its opener and is not closed here.

    It must sit directly on the descriptor, below every read-ahead buffer: a
    buffered handle underneath would pull far more from the file than this
    ever admits, and the budget would be counting what came out of that
    buffer rather than what was read. So it refuses a buffered handle.
    """

    def __init__(self, handle, observed, budget: DetailBudget, digest=None):
        if isinstance(handle, io.BufferedIOBase):
            raise TypeError("a probe must read the descriptor, not a buffer over it")
        super().__init__()
        self.handle, self.size, self.budget = handle, observed.st_size, budget
        self.consumed = 0
        self.digest = digest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self.consumed >= self.size:
            return 0
        allowed = self.budget.probe_allowance(self.consumed)
        self.budget.check()
        view = memoryview(buffer)[:min(len(buffer), allowed, self.size - self.consumed)]
        read = self.handle.readinto(view)
        if self.digest is not None:
            self.digest.update(view[:read])
        self.consumed += read
        self.budget.probe_bytes += read
        return read


def probe_header(reader, handle, observed, budget, proof=None):
    """The reader's header probe, under the probe budget when there is one."""
    if budget is None:
        return reader._session_header(handle)
    budget.begin_probe(observed)
    digest = hashlib.sha256() if proof is not None else None
    raw = ProbeReader(handle, observed, budget, digest)
    header = reader._session_header(io.BufferedReader(raw))
    if proof is not None:
        proof.update(size=observed.st_size, length=raw.consumed, digest=digest.digest(),
                     identity=identity(observed))
    return header


class ContextObservation:
    """Keep every context revision/open observation bound to its first file.

    Growth raises the size floor. A later healthy observation cannot erase a
    different identity or a smaller size that this read already observed.
    """

    def __init__(self, entry):
        self.identity, self.size = entry_identity(entry), entry[1]

    def observe(self, key, size):
        if key != self.identity or size < self.size:
            raise SourceChanged("context source identity changed or size decreased")
        self.size = size

    def revision(self, revision):
        self.observe(entry_identity(revision[0]), revision[0][1])

    def opened(self, observed):
        self.observe(identity(observed), observed.st_size)


def context_prefix_held(path, proof, observation, budget):
    """Recheck a changed foreign file's original identity bytes, not its body.

    The initial header probe established its native SID. A later append does
    not invalidate that ownership if the same anchored file still holds every
    byte the probe actually consumed. This proof spends the original probe
    budget without replacing the stamp used to acquire selected bodies.
    """
    with regular_source(path, proof["identity"], buffered=False) as (handle, observed):
        observation.opened(observed)
        budget.count_probe()
        digest = hashlib.sha256()
        raw = ProbeReader(handle, observed, budget, digest)
        buffer = bytearray(io.DEFAULT_BUFFER_SIZE)
        while raw.consumed < proof["length"]:
            remaining = proof["length"] - raw.consumed
            if not raw.readinto(memoryview(buffer)[:min(len(buffer), remaining)]):
                raise SourceChanged("context identity prefix disappeared")
        observation.opened(os.fstat(handle.fileno()))
        if digest.digest() != proof["digest"]:
            raise SourceChanged("context identity prefix changed")
    with regular_source(path, proof["identity"]) as (_, after):
        observation.opened(after)
    budget.check()


def reserve_selected(budget: DetailBudget, members) -> None:
    """Reserve every body this read will acquire, before reading any of them.

    ``members`` are the selected session's files with the revisions that
    anchored them. Each is opened only to observe it; its identity must be the
    one discovery anchored and its stamp the one it was probed with. A group
    that does not fit is refused here, with nothing of it read.
    """
    for path, revision in members:
        budget.check()
        with regular_source(path, revision_identity(revision, path)) as (_, observed):
            budget.charge_file(path, observed)


def checked_read(path: Path, handle, before, budget: DetailBudget) -> bytes:
    """Read this open source whole, and prove it is still the file it was.

    ``regular_source`` already refuses a name that is not the regular file it
    was observed to be, which closes the window before the read. This closes
    the window inside it, and every read this mode makes goes through here --
    a session's own bytes and the saved Cursor pin that chose them alike,
    because a pin read without these checks is a second way in for exactly the
    change the source read refuses.

    Two observations are compared against the one taken at open: the
    descriptor's, which is the file that was actually read, and the name's,
    which must still be that same file. Each carries device, inode, length,
    modification time and change time -- the last because a same-length
    rewrite can put a modification time back and cannot put a change time
    back. A file that grew, was truncated, was rewritten in place, or was
    replaced under its name while it was being read is refused: half of one
    generation beside half of another is not a session anyone had.
    """
    # Admitted before a byte is read, so an oversized source costs the stat
    # that measured it and nothing more.
    budget.charge_file(path, before)
    data = handle.read(before.st_size + 1)
    after = os.fstat(handle.fileno())
    if len(data) != before.st_size:
        raise SourceChanged("native source changed length while it was read")
    if identity(after) != identity(before) or file_stamp(after) != file_stamp(before):
        raise SourceChanged("native source changed while it was read")
    try:
        named = path.lstat()
    except OSError as error:
        raise SourceChanged("native source name vanished while it was read") from error
    if identity(named) != identity(before) or file_stamp(named) != file_stamp(before):
        raise SourceChanged("native source name no longer holds the file that was read")
    return data


def acquire_bytes(path: Path, budget: DetailBudget, *, expect=None) -> bytes:
    """One file, read whole into memory through one validated descriptor."""
    budget.check()
    with regular_source(path, expect) as (handle, before):
        data = checked_read(path, handle, before, budget)
    bounded_rows(data, budget)
    return data


def detail_acquirer(budget: DetailBudget):
    """``source_snapshot``'s call shape, held in memory instead of on disk.

    The same ``(path, host, revision)`` call the snapshot takes, so the
    existing Codex history reader consumes either one unchanged.
    """
    @contextmanager
    def acquire(path, host, revision=()):
        path = Path(path)
        if host == "cursor" and path.name == "store.db":
            raise DetailUnsupported("a Cursor store is not a JSONL source")
        yield acquire_bytes(path, budget, expect=revision_identity(revision, path))
    return acquire


def read_detail(reader, path: Path, revision, host: str, budget: DetailBudget, group,
                witness=None, origin=None, references=()):
    """Normalize one session from bytes this process holds.

    The converters are the ordinary export's. The global Codex title index is
    not one of them: a name that is not in this session's own bytes is not
    part of its exact detail, and reading that index is a whole-file read of
    an unrelated file that these ceilings do not cover. A session Codex named
    only there carries its derived title here, with or without a witness.
    """
    acquire = detail_acquirer(budget)
    guard = budget.hold_records
    members = ([(segment, segment_revision) for _, segment, segment_revision, _, _ in group]
               if group is not None else [(path, revision)])
    # A fork's referenced rollouts are acquired too, and are charged the same.
    members += [(source, held) for _, source, held, _ in references]
    if host == "cursor" and path.name == "store.db":
        raise DetailUnsupported("a Cursor store is not a JSONL source")
    reserve_selected(budget, members)
    if group is not None:
        if witness is not None:
            raise WitnessNotFlat("a paginated session is never witnessed")
        from readers import codex_history
        options = {"origin": origin} if origin is not None else {}
        return codex_history.read(group, acquire, title_index={}, record_guard=guard,
                                  references=references, **options)
    with acquire(path, host, revision) as data:
        budget.check()
        if host == "codex":
            options = {"origin": origin} if origin is not None else {}
            return reader.to_canonical(data, strict=True, title_index={},
                                       record_guard=guard, witness=witness, **options)
        return reader.to_canonical(path, strict=True, source_bytes=data, record_guard=guard)


def emit_detail(header: dict, records: list, budget: DetailBudget, stream=None) -> None:
    """Encode one validated session into a bounded buffer, then write it once.

    The ordinary export builds a list of encoded lines beside the records it
    already holds. Here the encoding accumulates in a single buffer the output
    ceiling bounds, so the most this mode holds is the session plus one
    bounded copy of its text -- and a session whose text does not fit is
    refused whole rather than delivered in part.
    """
    stream = sys.stdout if stream is None else stream
    first = encode(header)
    if len(first) + 1 > budget.max_header_bytes:
        raise DetailLimit("detail_limit_header_bytes")
    buffer = io.StringIO()
    total = 0
    for line in itertools.chain((first,), map(encode, records)):
        # ``encode`` escapes to ASCII, so one character is one byte.
        total += len(line) + 1
        if total > budget.max_output_bytes:
            raise DetailLimit("detail_limit_output_bytes")
        buffer.write(line)
        buffer.write("\n")
    stream.write(buffer.getvalue())
    # Deliver here rather than at interpreter exit, so a consumer that stopped
    # reading is reported as a failed read instead of a traceback after the
    # status has already been decided.
    stream.flush()


# --- source witness -----------------------------------------------------------
#
# An opt-in companion for a consumer that must bind each canonical record to
# the exact native row it came from. The reader describes every record from
# the rows its own conversion consumed; this layer adds what only the opener
# of the file knows -- which physical file that was and what it looked like --
# and refuses the session if either the file or any record's description is
# not what this read established.


WITNESS_UNOWNED = "witness_source_unowned"
WITNESS_NOT_FLAT = "witness_source_not_flat"


class WitnessUnowned(ValueError):
    """A witness source is not provably one of the Codex store's own rollouts.

    Reported with one fixed code and no path: the name or identity that failed
    is exactly what must not be echoed.
    """
    code = WITNESS_UNOWNED


class WitnessNotFlat(WitnessUnowned):
    """The selected session is not positively one flat rollout.

    A witness covers flat rollouts only. A paginated root is not the whole
    session, and its other files may sit where this read cannot prove it saw
    them all, so any paginated marker refuses the session rather than letting
    one file stand for it.
    """
    code = WITNESS_NOT_FLAT


def owned_rollout(reader, path, anchors) -> tuple[Path, tuple]:
    """An explicit witness source, only when it is the Codex store's own rollout.

    Exact detail follows whatever path its caller names. A witness says the
    records came from a Codex session, so its source must sit beneath the
    configured sessions root -- named through that root or through the target
    it led to before this run looked -- with no alias at or below the root,
    and must carry a rollout's exact file name. The root itself may be a
    configured symlink. What resolve() returns must be the target the anchor
    recorded, and the very file the component check observed.
    """
    root = reader._SESSIONS
    physical = anchors.get(root)
    if physical is None:
        raise WitnessUnowned("the sessions root is unavailable")
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    base = next((item for item in (root, physical) if path.is_relative_to(item)), None)
    relative = path.relative_to(base) if base is not None else None
    if (relative is None or not relative.parts or ".." in relative.parts
            or codex_witness.rollout_name(path.name) is None):
        raise WitnessUnowned("witness source is not a rollout beneath the sessions root")
    leaf = None
    for depth in range(1, len(relative.parts) + 1):
        observed = base.joinpath(*relative.parts[:depth]).lstat()
        kind = stat.S_ISREG if depth == len(relative.parts) else stat.S_ISDIR
        if stat.S_ISLNK(observed.st_mode) or not kind(observed.st_mode):
            raise WitnessUnowned("witness source is reached through an alias")
        leaf = observed
    resolved = path.resolve(strict=True)
    if resolved != physical / relative:
        raise WitnessUnowned("witness source resolved outside the anchored root")
    after = resolved.lstat()
    if not stat.S_ISREG(after.st_mode) or identity(after) != identity(leaf):
        raise SourceChanged("witness source changed while it was resolved")
    return resolved, identity(leaf)


def witness_claim(path: Path, source_header, selected=None) -> bool:
    """Whether a probed rollout is the selection, refusing one whose file name
    and native header disagree about it.

    A rollout the selection names -- by its file name or by its header -- must
    state one canonical session UUID in both, and that must be the selected
    one. Any other rollout is outside the selection and only counted.
    """
    names = codex_witness.rollout_name(path.name)
    payload = source_header.get("payload") if isinstance(source_header, dict) else None
    claimed = payload.get("id") if isinstance(payload, dict) else None
    if names is None:
        raise WitnessUnowned("witness candidate is not named as a rollout")
    if selected is not None and selected not in (names[0], claimed):
        return False
    if codex_witness.session_id(claimed) is None or claimed != names[0] or (
            selected is not None and claimed != selected):
        raise WitnessUnowned("rollout name and native header disagree")
    return True


def witness_flat(path: Path, source_header, sid: str) -> None:
    """A claimed rollout must positively be the whole of one flat session.

    Its name is this session's unsuffixed rollout, and its header states a
    flat history (``codex_witness.flat_header``). A continuation name, a
    paginated mode, a history reference, or an unknown or malformed marker
    refuses the session: a witness never lets one file of a paginated
    session stand for all of it.
    """
    names = codex_witness.rollout_name(path.name)
    if names is None or names[0] != sid:
        raise WitnessUnowned("a witnessed source is not a rollout of this session")
    if names[1] is not None or not codex_witness.flat_header(source_header, sid):
        raise WitnessNotFlat("the witnessed session is not one flat rollout")


def witness_root_held(reader, anchors) -> None:
    """The configured sessions root still leads where it was first anchored.

    Checked immediately before a witness is written: a root retargeted after
    selection could have hidden another claimant, so even a flat source is
    then not provably the root's own.
    """
    root = reader._SESSIONS
    try:
        current = root.resolve(strict=True)
    except (OSError, RuntimeError):
        current = None
    if anchors.get(root) is None or current != anchors[root]:
        raise WitnessUnowned("the sessions root was retargeted")


def witness_observation(path: Path, revision) -> dict:
    """One segment's file facts, which must be the ones its revision recorded.

    This is an operational change detector over device, inode, length and
    modification/change times -- not a cryptographic proof of the bytes.
    """
    with regular_source(path, revision_identity(revision, path)) as (_, observed):
        facts = file_observation(path, observed)
    if facts not in revision:
        raise SourceChanged("a witnessed segment is no longer its recorded revision")
    # Reported from the very tuple the revision holds, so what is compared at
    # discovery, before the read and after it is what the witness states.
    _, size, mtime_ns, inode, device, ctime_ns = facts
    return {"kind": "operational_change_detector", "device": device, "inode": inode,
            "size": size, "mtime_ns": mtime_ns, "ctime_ns": ctime_ns}


def witness_segments(path: Path, revision, group) -> dict:
    """The one physical file a flat session's records come from, keyed as the
    witness keys it: ``None``, a flat rollout having no rollout ID."""
    if group is not None:
        raise WitnessNotFlat("a paginated session is never witnessed")
    return {None: (path, witness_observation(path, revision))}


def witnessed(records: list, described: dict, segments: dict):
    """Each record paired with its own witness; any record without one refuses.

    The witness travels on the record's own line, beside its UUID, so a
    consumer never has to join two streams to know where a record came from.
    """
    if len(described) != len(records):
        raise ValueError("source witnesses do not cover the records exactly")
    for record in records:
        entry = described.get(id(record))
        if entry is None or entry[0] is not record:
            raise ValueError("a record has no source witness")
        witness = dict(entry[1])
        key = witness["segment"]["rollout_id"]
        if key not in segments:
            raise ValueError("a record names a segment this read did not open")
        if any(contributor["rollout_id"] != key for contributor in witness["contributors"]):
            raise ValueError("a record's contributors span segments")
        segment, observation = segments[key]
        witness["segment"] = {**witness["segment"], "path": str(segment),
                              "observation": observation}
        yield {**record, "source_witness": witness}


# --- origin evidence ------------------------------------------------------------
#
# An opt-in claim, on the record's own line, that Codex itself injected the
# native item the record came from (see ``readers.codex_origin``). The reader
# makes each claim from the rows its conversion consumed; this layer admits the
# mode only for a session the Codex sessions root owns, flat or paginated, and
# joins each claim to the one record it names. A record with no claim is
# emitted exactly as it would be without the flag.


ORIGIN_UNOWNED = "origin_source_unowned"
# The bulk export's one notice: this session's lines carry no origin evidence
# because the run could not vouch for its ownership. Not incompleteness.
ORIGIN_WITHHELD = "origin_evidence_withheld"


class OriginUnowned(ValueError):
    """An origin-evidence source is not provably the sessions root's own.

    Reported with one fixed code and no path, like ``WitnessUnowned``.
    """
    code = ORIGIN_UNOWNED


def origin_members(path: Path, sid: str, group) -> None:
    """Every rollout an origin claim may name is the session's own by name.

    A flat source is this session's unsuffixed rollout. A paginated group's
    members are its rollouts too, each named exactly -- the root (the one
    member with no history reference, or a fork's first rollout, whose
    reference names another session's rollout that is never a member)
    unsuffixed, each continuation by the
    rollout ID the group keys it under -- so the rollout ID a claim states is
    the session UUID for the root and that file's own suffix otherwise.
    """
    members = ([(None, path, None)] if group is None
               else [(rid, segment, base) for rid, segment, _, _, base in group])
    for n, (rid, segment, base) in enumerate(members):
        names = codex_witness.rollout_name(Path(segment).name)
        # The root -- original, or a fork's first rollout -- is unsuffixed.
        suffix = None if base is None or n == 0 else rid
        if names != (sid, suffix) or (
                group is not None and rid != (suffix or sid)):
            raise OriginUnowned("an origin source is not named as this session's rollout")


def origin_dispute(path: Path, source_header) -> set:
    """The sessions a probed rollout's file name and header disagree about.

    Empty when the name is exactly a rollout's and the header states the same
    canonical session UUID. Otherwise each session either side names -- the
    one the name states and the one the header claims -- may have a claimant
    it cannot be told apart from, and the bulk export withholds its claims.
    This is ``witness_claim``'s check without a selection: it names what is
    disputed instead of refusing the request.
    """
    names = codex_witness.rollout_name(path.name)
    payload = source_header.get("payload") if isinstance(source_header, dict) else None
    claimed = payload.get("id") if isinstance(payload, dict) else None
    if names is not None and codex_witness.session_id(claimed) == names[0]:
        return set()
    return {sid for sid in (names[0] if names else None, claimed) if isinstance(sid, str)}


def origin_claims(records: list, claimed: dict) -> list:
    """Each record's origin claim or None, in record order.

    Every claim must name a record this read is emitting, once, under the UUID
    that record carries; anything else refuses the session rather than
    attaching a claim to a record it was not made for.
    """
    positions = {id(record): n for n, record in enumerate(records)}
    claims = [None] * len(records)
    for key, (record, evidence) in claimed.items():
        n = positions.get(key)
        if n is None or records[n] is not record or claims[n] is not None:
            raise ValueError("an origin claim names no emitted record")
        if evidence.get("record_uuid") != record.get("uuid"):
            raise ValueError("an origin claim names another record identity")
        claims[n] = evidence
    return claims


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "cursor"), required=True)
    parser.add_argument("--since", type=since_instant)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--session", type=session_selector,
                        help="Select one native session ID, latest, or an explicit path")
    parser.add_argument("--exact-detail", action="store_true",
                        help="Read one identified JSONL session whole, in memory, under fixed ceilings")
    parser.add_argument("--context-page", help="Versioned bounded complete-record page of a flat Codex session")
    parser.add_argument("--deadline-seconds", type=deadline_seconds,
                        help="Refuse an --exact-detail read that is still working after this long")
    parser.add_argument("--source-witness", action="store_true",
                        help="With --host codex --exact-detail --session: add each record's "
                             "metadata-only source witness to its line")
    parser.add_argument("--origin-evidence", action="store_true",
                        help="With --host codex, either --exact-detail --session or the full "
                             "export alone: add origin evidence to each record Codex converted "
                             "from an injected skill item")
    parser.add_argument("--human-input-adjustments", action="store_true",
                        help="With the full Codex export: add metadata-only image wrapper lengths")
    parser.add_argument("--automated-input-evidence", action="store_true",
                        help="With the full Codex or Cursor export: add metadata-only evidence "
                             "to each user record the host or this reader wrote itself")
    args = parser.parse_args(argv)
    if args.automated_input_evidence and (args.exact_detail or args.session is not None
            or args.since is not None or args.metadata_only):
        # Like the other export evidence: every session, with its records,
        # once discovery and every header have been counted.
        parser.error("--automated-input-evidence requires the full export")
    if args.human_input_adjustments and (args.host != "codex" or args.exact_detail
            or args.session is not None or args.since is not None or args.metadata_only):
        parser.error("--human-input-adjustments requires the full Codex export")
    if args.source_witness and (args.host != "codex" or not args.exact_detail):
        # A witness binds a record on its first accepted ingestion, so it is
        # only produced for one selected session read under the exact-detail
        # ceilings -- never for a bulk, --since or metadata export.
        parser.error("--source-witness describes one selected Codex session; "
                     "it requires --host codex --exact-detail --session")
    if args.origin_evidence and args.host != "codex":
        parser.error("--origin-evidence describes Codex sessions; it requires --host codex")
    if args.origin_evidence and not args.exact_detail and (
            args.session is not None or args.since is not None or args.metadata_only):
        # Outside exact detail, origin evidence rides only on the full export:
        # every session, with its records. A selection or a filter would
        # leave out the headers the ownership checks count.
        parser.error("--origin-evidence reads one session with --exact-detail --session, "
                     "or the full Codex export; --session, --since and --metadata-only "
                     "do not apply to the export")
    context_request = None
    if args.context_page is not None:
        if (not args.exact_detail or args.host != "codex" or args.source_witness or
                args.origin_evidence or args.session is None or
                codex_witness.session_id(args.session) is None):
            parser.error("--context-page requires --host codex --exact-detail --session UUID alone")
        from codex_context import selection
        try:
            context_request = selection(args.context_page)
        except ContextRefusal:
            parser.error("invalid versioned context-page selection")
    # The exact mode's claims refuse the request over any doubt about the one
    # session selected. The export's claims are made only once discovery and
    # every header have been counted, and are withheld per session instead.
    exact_origin = args.origin_evidence and args.exact_detail
    bulk_origin = args.origin_evidence and not args.exact_detail
    bulk_evidence = bulk_origin or args.human_input_adjustments or args.automated_input_evidence
    if args.exact_detail:
        if args.metadata_only or args.since is not None:
            parser.error("--exact-detail reads one identified session's records; "
                         "--metadata-only and --since do not apply to it")
        if not args.session or args.session == "latest":
            # "latest" is a ranking, not an identity. An exact detail request
            # names the session it wants, so the answer cannot be a different
            # session that happened to be written to most recently.
            parser.error("--exact-detail requires --session with a native session ID or a path")
    elif args.deadline_seconds is not None:
        parser.error("--deadline-seconds applies to --exact-detail")
    budget = DetailBudget(args.deadline_seconds) if args.exact_detail else None
    if context_request is not None:
        from codex_context import MAX_SOURCE_BYTES
        budget.max_source_bytes = MAX_SOURCE_BYTES
    detail_emitted = False
    reader = reader_for(args.host)
    explicit_path = False
    explicit_basis = None
    path_reader = reader
    latest = None
    latest_seen = None
    latest_basis = None
    incomplete = False
    cursor_counts = Counter()

    def notice(code: str, path=None):
        # Static codes keep parser errors and transcript excerpts out of diagnostics.
        print(json.dumps({"type": "diagnostic", "host": args.host,
                          "code": code, "path": str(path) if path else None}), file=sys.stderr)

    def diagnostic(code: str, path=None):
        nonlocal incomplete
        incomplete = True
        notice(code, path)

    def owned(check, *arguments):
        """One of the witness's ownership checks, refused in the terms of the
        mode that asked: origin evidence alone reports its own code."""
        try:
            return check(*arguments)
        except WitnessUnowned as refused:
            if args.source_witness:
                raise
            raise OriginUnowned(str(refused)) from None

    try:
        if budget is not None:
            budget.check()          # before locate(), any walk, or any probe
        path_syntax = bool(args.session and any(mark in args.session for mark in ("/", "\\")))
        explicit_path = args.session is not None and args.session != "latest" and path_syntax
        if args.origin_evidence and explicit_path:
            # Origin evidence names a session by its canonical UUID only. That
            # selection counts every rollout header beneath the root that
            # claims the session; a path, flat or paginated, is found by its
            # own name and cannot show its claims come from the session's only
            # sources. Refused before any lookup or probe of that path.
            raise OriginUnowned("origin evidence selects a session UUID, never a path")
        # Where each configured root leads, recorded once before any lookup:
        # discovery rechecks and transcript classification both use it.
        anchors = ({} if explicit_path else
                   root_anchors(reader, lambda error: diagnostic("discovery_incomplete")))
        if args.source_witness or exact_origin:
            # A witness names a Codex session, so its selection is one: a
            # canonical session UUID, or a rollout the sessions root owns.
            # Both are settled before any source body or header is read, and
            # this anchor is the one the root must still hold at output.
            # Origin evidence names a session by its UUID alone (see above).
            if explicit_path:
                witness_anchors = root_anchors(reader)
            elif codex_witness.session_id(args.session) is None:
                raise (WitnessUnowned if args.source_witness else OriginUnowned)(
                    "selection is not a session UUID")
            else:
                witness_anchors = anchors
        if explicit_path:
            path, error = reader.locate(args.session)
            if error or path is None:
                diagnostic("session_unavailable")
                return 2
            sessions = [{"path": str(path)}]
            if args.host == "codex":
                if args.source_witness:
                    selected_path, selected_anchor = owned_rollout(reader, path, witness_anchors)
                    if codex_witness.rollout_name(selected_path.name)[1] is not None:
                        # A continuation is refused by its name, before its
                        # header is probed.
                        raise WitnessNotFlat("a continuation is never witnessed")
                else:
                    selected_path, selected_anchor = Path(path).resolve(strict=True), None
                revision = source_revision(selected_path, "codex", expect=selected_anchor)
                with regular_source(selected_path, revision_identity(revision, selected_path),
                                    buffered=budget is None) as (handle, observed):
                    selected_header = probe_header(reader, handle, observed, budget)
                if args.source_witness:
                    witness_claim(selected_path, selected_header)
                    # Flat, or refused here: a witness never reaches the
                    # group discovery below, nor its selected_path.parent scope.
                    witness_flat(selected_path, selected_header,
                                 selected_header["payload"]["id"])
                payload = selected_header.get("payload", {})
                if payload.get("history_mode") == "paginated" or payload.get("history_base") is not None:
                    from readers.discovery import paths
                    from readers.codex_history import _NAME
                    sid = native_text(payload.get("id"), required=True)
                    configured = reader._SESSIONS.resolve()
                    scope = configured if selected_path.is_relative_to(configured) else selected_path.parent
                    path_reader = SimpleNamespace(HOST="codex", _SESSIONS=scope)
                    anchors = root_anchors(path_reader, lambda error: diagnostic("discovery_incomplete"))
                    pattern = ("**", "rollout-*.jsonl") if scope == configured else ("rollout-*.jsonl",)
                    candidates = paths(scope, pattern, lambda error: diagnostic("discovery_incomplete"),
                                       None if budget is None else budget.observe)
                    if incomplete:
                        return 2
                    sessions = [{"path": str(candidate)} for candidate in candidates
                                if (match := _NAME.fullmatch(candidate.name)) and match[1] == sid]
                    if selected_path not in [Path(item["path"]) for item in sessions]:
                        raise ValueError("explicit rollout was excluded by discovery")
                    explicit_basis = (selected_path, revision)
                    explicit_path = False
                    args.session = sid
        else:
            options = {"include_representations": True} if args.host == "cursor" else {}
            if budget is not None:
                options["observe"] = budget.observe
            sessions = reader.list_sessions(None, on_error=lambda error: diagnostic("discovery_incomplete"), **options)
            if args.host == "cursor":
                from cursor_flush import _UUID_RE
                # A native transcript lives at <uuid>/<uuid>.jsonl, the layout the
                # capture path enforces too. A discovered file whose name and
                # directory disagree has no established identity: it must not be
                # exported under its file name nor vouch for either session.
                kept = []
                for row in sessions:
                    path = Path(row["path"])
                    valid_store = (path.name == "store.db" and
                                   bool(_UUID_RE.fullmatch(path.parent.name)))
                    valid_transcript = (path.name != "store.db" and
                                        bool(_UUID_RE.fullmatch(path.stem)) and
                                        path.stem == path.parent.name)
                    if not (valid_store or valid_transcript):
                        diagnostic("discovery_incomplete", path)
                        continue
                    kept.append(row)
                sessions = kept
            discovered = list(sessions)
            if args.host == "cursor":
                # One store and one transcript are alternative representations.
                # Multiple copies of either kind remain ambiguous even when a
                # preferred store or saved pin would otherwise hide them.
                groups = {}
                for row in sessions:
                    path = Path(row["path"])
                    kind = "store" if path.name == "store.db" else "transcript"
                    sid = path.parent.name if kind == "store" else path.stem
                    groups.setdefault(sid, {"store": [], "transcript": []})[kind].append(row)
                sessions = []
                for sid, kinds in groups.items():
                    cursor_counts[f"cursor-{sid}"] = max(map(len, kinds.values()))
                    sessions.extend(kinds["store"] or kinds["transcript"])
            if args.session == "latest":
                if not discovered:
                    diagnostic("session_unavailable")
                    return 2
                ranked = {}

                def latest_mtime(row):
                    # Ranking keeps the observation it consumed: the winner's
                    # clock file must still match when the baseline is taken
                    # and when the session is emitted, or a fresh invocation
                    # could rank differently than the selection being emitted.
                    path = Path(row["path"])
                    if args.host == "cursor" and path.name == "store.db":
                        # Ranking consumes only the update clock. Full metadata
                        # validation belongs to the selected session below.
                        clock = path.parent / "meta.json"
                        with regular_source(clock) as (handle, observed):
                            metadata = load_json(handle.read().decode("utf-8"), strict=True)
                        ranked[row["path"]] = (clock, file_observation(clock, observed))
                        value = metadata.get("updatedAtMs") if isinstance(metadata, dict) else None
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ValueError("Cursor latest ordering requires a finite native timestamp")
                        reader._iso_ms(value, strict=True)
                        return value / 1000
                    with regular_source(path) as (_, observed):
                        ranked[row["path"]] = (path, file_observation(path, observed))
                    return observed.st_mtime_ns / 1_000_000_000
                winner = max(discovered, key=latest_mtime)
                latest_basis = ranked[winner["path"]]
                latest, _ = discovered_path(reader, winner["path"], anchors)
                if args.host == "cursor":
                    latest, _, latest_seen = cursor_selection(latest, select_saved=True, anchors=anchors)
                    if discovered_source(reader, latest, discovered, anchors) is None:
                        diagnostic("session_unavailable")
                        return 2
                    # Discovery prefers stores, whereas native latest may
                    # select a newer transcript of the same session. Replace
                    # that one representation without hiding duplicate stores.
                    def cursor_id(path):
                        path = Path(path)
                        return path.parent.name if path.name == "store.db" else path.stem
                    matches = [row for row in sessions
                               if cursor_id(row["path"]) == cursor_id(latest)]
                    # Same-ID copies were counted above. Other UUIDs are not
                    # part of this request and must not prepare metadata/state.
                    sessions = ([{"path": str(latest)}] if len(matches) == 1 else matches)
                if discovered_source(reader, latest, sessions, anchors) is None:
                    sessions.append({"path": str(latest)})
    except DetailLimit as limit:
        # A ceiling reached inside enumeration is that ceiling, not an
        # inaccessible tree: say which one stopped the walk.
        diagnostic(limit.code)
        return 2
    except (WitnessUnowned, OriginUnowned) as refused:
        diagnostic(refused.code)
        return 2
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RuntimeError):
        diagnostic("discovery_incomplete")
        return 2

    context_snapshot = None
    context_proofs = {}
    context_observations = {}
    if context_request is not None:
        try:
            context_snapshot = {str(Path(row["path"])): source_revision(Path(row["path"]), "codex")
                                for row in sessions}
            context_observations = {name: ContextObservation(revision[0])
                                    for name, revision in context_snapshot.items()}
        except (OSError, ValueError, TypeError):
            diagnostic("discovery_incomplete")
            return 2
    if args.host == "cursor" and args.session and args.session != "latest" and not explicit_path:
        # Cursor identities are defined by the native path, unlike Codex.
        # Keep all same-ID copies for ambiguity checks, but never parse an
        # unrelated session's saved state merely to select one UUID.
        sessions = [row for row in sessions if (Path(row["path"]).parent.name
                    if Path(row["path"]).name == "store.db" else Path(row["path"]).stem) == args.session]

    # Cursor identity survives unreadable metadata: a damaged copy still
    # makes the other path ambiguous. Count before preparing either copy.
    if args.host == "cursor" and explicit_path:
        cursor_counts = Counter()
    elif args.host != "cursor":
        cursor_counts = None
    latest_native_id = None
    if args.host == "codex" and args.session == "latest":
        try:
            with regular_source(latest, entry_identity(latest_basis[1])) as (handle, observed):
                if file_observation(latest, observed)[1:] != latest_basis[1][1:]:
                    raise SourceChanged("latest source changed")
                latest_native_id = native_text(reader._session_header(handle).get("payload", {}).get("id"), required=True)
        except (OSError, ValueError, TypeError, AttributeError):
            diagnostic("source_changed", latest)
            return 2
    prepared = []
    codex_headers = {}
    history_groups = {}
    history_references = {}
    rollout_index = {}
    counts = Counter()
    # Bulk origin evidence, from the same pass that counts identities. A
    # candidate whose header this run never read -- a walk that was not
    # complete, a file that failed or changed before its header was parsed --
    # could be a copy of any session, so the whole run is blind and claims
    # nothing. A rollout whose name and header disagree withholds every
    # session either of them names.
    origin_blind = bulk_evidence and incomplete
    origin_disputed = set()
    for session in sessions:
        path = Path(session["path"])
        context_name = str(path)
        probed = False
        try:
            if budget is not None:
                budget.check()
            # An explicit path is the caller's own alias to follow; a
            # discovered row must still be the regular file discovery saw,
            # and that identity anchors every later read of it.
            if explicit_path and args.source_witness:
                path, anchor = owned_rollout(reader, path, witness_anchors)
            elif explicit_path:
                path, anchor = path.resolve(strict=True), None
            else:
                path, anchor = discovered_path(path_reader, path, anchors)
                if context_snapshot is not None and anchor != entry_identity(context_snapshot[context_name][0]):
                    raise SourceChanged("context source identity changed before preparation")
                if args.source_witness:
                    names = codex_witness.rollout_name(path.name)
                    if names is None:
                        # Every candidate that could claim the selection is a
                        # named rollout before its header is probed.
                        raise WitnessUnowned("witness candidate is not named as a rollout")
                    if names[0] == args.session and names[1] is not None:
                        # The selection has a continuation: it is paginated.
                        raise WitnessNotFlat("a paginated session is never witnessed")
            if args.host == "cursor":
                from cursor_flush import _UUID_RE
                select_saved = not args.session or args.session == "latest" or bool(_UUID_RE.fullmatch(args.session))
                path, _, seen = cursor_selection(path, select_saved=select_saved,
                                                 anchors=anchors, budget=budget)
                if budget is not None and path.name == "store.db":
                    # The selected representation is a database. This mode is
                    # defined over JSONL only, and the other representation of
                    # the same session -- where one exists at all -- is the one
                    # the selection did not choose. Reading that instead would
                    # answer an exact request with an older conversation, so
                    # the request is refused here: before any database is
                    # opened, any journal is recovered, and anything is copied.
                    raise DetailUnsupported("Cursor stores need SQLite support")
                if not explicit_path:
                    anchor = discovered_source(reader, path, discovered, anchors)
                    if anchor is None:
                        raise ValueError("saved Cursor source was excluded by discovery")
            revision = source_revision(path, args.host, expect=anchor)
            if context_snapshot is not None:
                context_observations[context_name].revision(revision)
            if latest_basis is not None and not basis_unchanged(latest_basis):
                # The clock this selection was ranked on changed before the
                # baseline: a fresh invocation might rank another session.
                diagnostic("source_changed", path)
                continue
            if args.host == "cursor" and (state_observation(revision, path) != seen or (
                    args.session == "latest" and cursor_sid(path) == cursor_sid(latest)
                    and seen != latest_seen)):
                # The saved state that chose this representation must be the
                # state the baseline (and the latest selection) observed. A pin
                # that changed or vanished in between is a source change, not a
                # fresh selection: report it rather than emit the stale choice.
                diagnostic("source_changed", path)
                continue
            mtime = max(item[2] for item in revision) / 1_000_000_000
            if not math.isfinite(mtime):
                raise ValueError("invalid mtime")
            # Header probes read through the validated descriptor, like the
            # snapshot below: nothing reopens the native path by name after
            # source_revision(), so a swapped-in FIFO or alias cannot block or
            # redirect the probe before the final revision check.
            if args.host == "codex":
                # Parse once and count the native identity before validating
                # other values; malformed duplicates must not appear unique.
                with regular_source(path, revision_identity(revision, path),
                                    buffered=budget is None) as (handle, observed):
                    proof = {} if context_snapshot is not None else None
                    if proof is not None:
                        context_observations[context_name].opened(observed)
                    source_header = (probe_header(reader, handle, observed, budget) if proof is None else
                                     probe_header(reader, handle, observed, budget, proof))
                if bulk_evidence:
                    origin_disputed |= origin_dispute(path, source_header)
                    probed = True
                # Every candidate is checked before the identity filter below:
                # a rollout whose name or header names the selection must name
                # it in both, so neither can hide a claimant from the count.
                # Origin evidence shares the check but not the flat rule.
                if args.source_witness or exact_origin:
                    selection = owned(witness_claim, path, source_header,
                                      None if explicit_path else args.session)
                    if selection and args.source_witness:
                        witness_flat(path, source_header, source_header["payload"]["id"])
                sid = native_text(source_header.get("payload", {}).get("id"), required=True)
                if proof is not None:
                    proof.update(path=path, sid=sid)
                    context_proofs[context_name] = proof
                    if sid == args.session and (revision[0][1:] != context_snapshot[context_name][0][1:]
                            or file_stamp(observed) != (revision[0][1], revision[0][2], revision[0][5])):
                        raise SourceChanged("selected context source changed before preparation")
                if explicit_basis is not None and sid != args.session:
                    raise ValueError("paginated filename and native identity disagree")
                # Every identified rollout, whichever session it names: a fork
                # finds the rollout its history references here.
                facts = codex_history.reference_facts(path, source_header)
                if facts is not None:
                    rollout_index.setdefault(facts[0], []).append((path, revision, facts[1]))
                if budget is None:
                    codex_headers[path] = source_header
                native = None
            else:
                meta_text = None
                if path.name == "store.db":
                    meta = path.parent / "meta.json"
                    with regular_source(meta, revision_identity(revision, meta)) as (handle, _):
                        meta_text = handle.read().decode("utf-8")
                native = reader.session_metadata(
                    path, meta_text=meta_text,
                    # Explicit files are intentionally outside discovery. Pass
                    # the file itself as a nonmatching classification anchor.
                    projects_root=(path if explicit_path else anchors.get(reader._PROJECTS)))
                sid = native_text(native.get("session_id"), required=True)
            # A readable identity still collides when another header field is bad.
            counts[f"{reader.HOST}-{sid}"] += 1
            if args.host == "codex" and args.session and not explicit_path:
                # Identity must participate in ambiguity checks, but an unrelated
                # header must not make an otherwise healthy selection incomplete.
                if args.session == "latest":
                    if sid != latest_native_id:
                        continue
                elif sid != args.session.removesuffix(".jsonl"):
                    continue
            if budget is not None and args.host == "codex":
                # Only a header that names the requested session is kept: every
                # other probe contributed its identity to the count above and
                # nothing else, so what identification holds stays small.
                codex_headers[path] = source_header
            if native is None:
                native = reader._metadata_from_header(source_header)
            header = header_for(reader, path, mtime, native)
            prepared.append((path, revision, header))
        except ContextRefusal as refused:
            diagnostic(refused.code, path)
        except DetailLimit as limit:
            diagnostic(limit.code, path)
        except DetailUnsupported:
            diagnostic("detail_prerequisite_unsupported", path)
        except SourceChanged:
            diagnostic("source_changed", path)
            origin_blind = origin_blind or (bulk_evidence and not probed)
        except (WitnessUnowned, OriginUnowned) as refused:
            # Not provably this selection's own flat rollout (or, for origin
            # evidence, its own rollouts): nothing else from this request is
            # witnessed or reported.
            diagnostic(refused.code)
            return 2
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                OverflowError, RuntimeError, argparse.ArgumentTypeError):
            diagnostic("session_unreadable", path)
            origin_blind = origin_blind or (bulk_evidence and not probed)
    if explicit_basis is not None and incomplete:
        return 2
    if args.host == "codex":
        grouped = {}
        for item in prepared:
            grouped.setdefault(item[2]["conversation_id"], []).append(item)
        selected = []
        for conversation, items in grouped.items():
            referenced = any(codex_headers[item[0]]["payload"].get("history_base") is not None
                             for item in items)
            paginated = any(codex_headers[item[0]]["payload"].get("history_mode") == "paginated" for item in items)
            if counts[conversation] == 1 and not referenced and not paginated:
                selected.extend(items)
                continue
            if args.source_witness:
                # Only a flat rollout is witnessed; any group is refused, as
                # exact detail refuses a duplicate it cannot plan.
                for item in items:
                    diagnostic("discovery_incomplete", item[0])
                continue
            try:
                if len(items) != counts[conversation]:
                    raise ValueError("a group member was unreadable")
                path, revision, header, group = codex_history.plan(items, codex_headers)
                # A fork's referenced rollouts belong to another session. They
                # are read only for its usage baseline, and a change to them
                # is a change to this read.
                references = codex_history.references(group, codex_headers, rollout_index)
                revision += tuple(entry for _, _, held, _ in references for entry in held)
                history_groups[path] = group
                if references:
                    history_references[path] = references
                if latest in {item[0] for item in items}:
                    latest = path
                selected.append((path, revision, header))
                counts[conversation] = 1
            except (ValueError, KeyError, TypeError, RecursionError):
                for item in items:
                    diagnostic("discovery_incomplete", item[0])
        prepared = selected

    def current_revision(path):
        if path in history_groups:
            members = [segment for _, segment, _, _, _ in history_groups[path]]
            members += [source for _, source, _, _ in history_references.get(path, ())]
            return tuple(entry for segment in members
                         for entry in source_revision(segment, args.host))
        return source_revision(path, args.host)

    # Reject every candidate sharing an actual native identity before emitting
    # any of them. File names alone do not establish Codex session identity.
    if cursor_counts is not None:
        counts |= cursor_counts
    if args.session == "latest":
        # Preserve the native latest-selection rule, but only after all actual
        # identities have participated in ambiguity detection.
        try:
            prepared = [item for item in prepared if item[0] == latest]
            if not prepared:
                diagnostic("session_unavailable")
                return 2
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            diagnostic("session_unavailable")
            return 2
    elif args.session and not explicit_path:
        native_id = args.session.removesuffix(".jsonl") if args.host == "codex" else args.session
        prepared = [item for item in prepared if item[2]["native_session_id"] == native_id]
        if not prepared:
            diagnostic("session_unavailable")
            return 2
    if budget is not None and incomplete:
        # An exact request is answered whole or not at all. Something in this
        # home could not be enumerated, read or identified, so this run cannot
        # say the selection was the only source claiming that identity --
        # an unreadable header is exactly what a second copy would look like.
        # The ordinary export still emits its healthy peers; this does not.
        return 2
    titles = (TitleIndex(reader, {item[2]["native_session_id"] for item in prepared})
              if args.host == "codex" and not args.metadata_only and budget is None else None)
    for path, revision, header in prepared:
        if counts[header["conversation_id"]] > 1:
            diagnostic("discovery_incomplete", path)
            continue
        try:
            if titles is None and args.since is not None and header["mtime"] < args.since:
                if current_revision(path) != revision:
                    diagnostic("source_changed", path)
                continue
            if context_request is not None:
                from codex_context import read
                import sys as context_sys
                encoded = read(reader, path, revision, header, context_request, budget,
                               context_sys.modules[__name__], history_groups.get(path))
                budget.check()
                try:
                    witness_root_held(reader, anchors)
                except WitnessUnowned:
                    raise SourceChanged("context root changed") from None
                current = reader._rollout_files(
                    on_error=lambda error: diagnostic("discovery_incomplete"), observe=budget.observe)
                snapshot = {}
                for current_path in current:
                    budget.check()
                    name = str(current_path)
                    snapshot[name] = source_revision(current_path, "codex")
                    if name in context_observations:
                        context_observations[name].revision(snapshot[name])
                if incomplete or snapshot.keys() != context_snapshot.keys():
                    raise SourceChanged("context sources changed")
                for name, original in context_snapshot.items():
                    budget.check()
                    proof = context_proofs[name]
                    resolved, anchor = discovered_path(reader, name, anchors)
                    if (resolved != proof["path"] or anchor != entry_identity(original[0])
                            or anchor != proof["identity"]):
                        raise SourceChanged("context source identity changed")
                    if snapshot[name] != original:
                        if proof["sid"] == args.session:
                            raise SourceChanged("selected context source changed")
                        context_prefix_held(resolved, proof, context_observations[name], budget)
                if current_revision(path) != revision:
                    raise SourceChanged("context selected sources changed")
                budget.check()
                sys.stdout.write(encoded)
                sys.stdout.flush()
                detail_emitted = True
                continue
            records = []
            used_title = []
            automated_claimed = None
            if not args.metadata_only:
                def fallback_title(sid):
                    observation = titles.get(sid)
                    used_title.append((observation, titles.stamp))
                    header["mtime"] = max(header["mtime"], titles.mtime(observation))
                    return observation[0]
                options = {"title_index": fallback_title} if titles is not None else {}
                if args.source_witness:
                    witness_flat(path, codex_headers[path], header["native_session_id"])
                    # Observed before the read and again after it below, so
                    # the file facts bracket the bytes the records came from.
                    segments = witness_segments(path, revision, history_groups.get(path))
                    described = options["witness"] = {}
                if bulk_evidence and args.host == "cursor":
                    # A Cursor session's records are identified by its own
                    # directory or file name; a session another source also
                    # claims was refused above, and a run that could not
                    # count every source claims nothing.
                    claimed = human_claimed = None
                    if args.automated_input_evidence and not origin_blind:
                        automated_claimed = options["automated"] = codex_origin.Withholding()
                elif bulk_evidence:
                    # The export never refuses over evidence: a session it
                    # cannot vouch for is read exactly as without the flag.
                    claimed = human_claimed = None
                    sid = header["native_session_id"]
                    if (not origin_blind and sid not in origin_disputed
                            and codex_witness.session_id(sid) is not None):
                        try:
                            origin_members(path, sid, history_groups.get(path))
                            if bulk_origin:
                                claimed = options["origin"] = codex_origin.Withholding()
                            if args.human_input_adjustments:
                                human_claimed = options["human"] = codex_origin.Withholding()
                            if args.automated_input_evidence:
                                automated_claimed = options["automated"] = (
                                    codex_origin.Withholding())
                        except OriginUnowned:
                            pass
                elif args.origin_evidence:
                    origin_members(path, header["native_session_id"], history_groups.get(path))
                    claimed = options["origin"] = {}
                if budget is not None:
                    records, native = read_detail(reader, path, revision, args.host,
                                                  budget, history_groups.get(path),
                                                  references=history_references.get(path, ()),
                                                  **options)
                    budget.hold_records(len(records))
                elif path in history_groups:
                    records, native = codex_history.read(
                        history_groups[path], source_snapshot,
                        references=history_references.get(path, ()), **options)
                else:
                    with source_snapshot(path, args.host, revision) as snapshot:
                        records, native = reader.to_canonical(snapshot, strict=True, **options)
                if native.get("session_id") != header["native_session_id"]:
                    raise ValueError("native identity changed during read")
                if args.host == "cursor":
                    from cursor_flush import apply_session_state
                    # Revalidate against the state covered by this revision and
                    # apply the state that validation parsed; an absent file is
                    # an explicit empty state, so no path is reopened here. The
                    # state read must be the one the baseline observed.
                    _, saved_state, seen = cursor_selection(path, anchors=anchors, budget=budget)
                    if seen != state_observation(revision, path):
                        raise SourceChanged("saved state changed during the read")
                    apply_session_state(records, header["native_session_id"],
                                        strict=True, state=saved_state)
                if records and validate_canonical(records):
                    raise ValueError("reader emitted invalid canonical records")
                header["title"] = native_text(native.get("title"))
            if used_title:
                observation, stamp_used = used_title[0]
                current = titles.get(header["native_session_id"])
                if current != observation or (
                        titles.stamp_bound(observation) and titles.stamp != stamp_used):
                    diagnostic("source_changed", path)
                    continue
            if (explicit_basis is not None and source_revision(explicit_basis[0], args.host) != explicit_basis[1]) or current_revision(path) != revision or (
                    latest_basis is not None and not basis_unchanged(latest_basis)):
                diagnostic("source_changed", path)
                continue
            claims = None
            if bulk_origin and claimed is not None and not claimed.failed:
                try:
                    # As exact detail checks before its output: the root still
                    # leads where discovery anchored it, or a claimant could
                    # have been hidden -- from this session and every later one.
                    witness_root_held(reader, anchors)
                    claims = origin_claims(records, claimed)
                except WitnessUnowned:
                    origin_blind = True
                except ValueError:
                    pass
            elif args.origin_evidence and not bulk_origin:
                # Joined to the records the read returned, before any line is
                # rebuilt around them.
                claims = origin_claims(records, claimed)
            human_claims = None
            if args.human_input_adjustments and human_claimed is not None and not human_claimed.failed:
                try:
                    witness_root_held(reader, anchors)
                    human_claims = origin_claims(records, human_claimed)
                except WitnessUnowned:
                    origin_blind = True
                except ValueError:
                    pass
            automated_claims = None
            if automated_claimed is not None and not automated_claimed.failed:
                try:
                    if args.host == "codex":
                        witness_root_held(reader, anchors)
                    automated_claims = origin_claims(records, automated_claimed)
                except WitnessUnowned:
                    origin_blind = True
                except ValueError:
                    pass
            if human_claims is not None:
                records = [record if claim is None else {**record, "human_input_adjustment": claim}
                           for record, claim in zip(records, human_claims)]
            if automated_claims is not None:
                records = [record if claim is None else {**record, "automated_input": claim}
                           for record, claim in zip(records, automated_claims)]
            if args.source_witness:
                if witness_segments(path, revision, history_groups.get(path)) != segments:
                    raise SourceChanged("a witnessed segment changed during the read")
                header["source_witness"] = {"contract": codex_witness.CONTRACT,
                                            "version": codex_witness.VERSION}
                records = list(witnessed(records, described, segments))
            if claims is not None:
                # The header says the mode ran, so a record without a claim
                # is one the reader declined to claim, not one never examined.
                header["origin_evidence"] = {"contract": codex_origin.CONTRACT,
                                             "version": codex_origin.VERSION}
                records = [record if claim is None else {**record, "origin_evidence": claim}
                           for record, claim in zip(records, claims)]
            if args.since is not None and header["mtime"] < args.since:
                continue
            # Validate the entire session before emitting its header. A bad
            # number or unsupported value cannot leave a partial session behind.
            if budget is not None:
                budget.check()
                if args.source_witness or args.origin_evidence:
                    # Last, before the first byte: the root still leads where
                    # the selection was anchored.
                    owned(witness_root_held, reader, witness_anchors)
                emit_detail(header, records, budget)
                detail_emitted = True
            else:
                lines = [encode(header)] + [encode(record) for record in records]
                if args.human_input_adjustments and human_claims is None:
                    notice("human_input_adjustments_withheld", path)
                if bulk_origin and claims is None:
                    # Every line is the one the export writes without the
                    # flag; this says so. The records are complete, so the
                    # exit status does not change.
                    notice(ORIGIN_WITHHELD, path)
                for line in lines:
                    print(line)
        except ContextRefusal as refused:
            diagnostic(refused.code, path)
        except DetailLimit as limit:
            diagnostic(limit.code, path)
        except DetailUnsupported:
            diagnostic("detail_prerequisite_unsupported", path)
        except SourceChanged:
            diagnostic("source_changed", path)
        except (WitnessUnowned, OriginUnowned) as refused:
            # Not provably this selection's own flat rollout (or, for origin
            # evidence, its own rollouts): nothing else from this request is
            # witnessed or reported.
            diagnostic(refused.code)
            return 2
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
                OverflowError, RuntimeError, argparse.ArgumentTypeError):
            diagnostic("session_unreadable", path)
    if budget is not None and not detail_emitted:
        # This mode's exit code is a statement about one session: 0 means that
        # session is on stdout, whole. Nothing above may leave an exact request
        # answered by silence and a healthy status.
        if not incomplete:
            diagnostic("session_unavailable")
        return 2
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
