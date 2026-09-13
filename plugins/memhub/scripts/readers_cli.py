#!/usr/bin/env python3
"""Read-only native session headers and canonical JSONL for local consumers."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import datetime
import io
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

from readers import reader_for, validate_canonical
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
    """One file as a revision entry: name, size, mtime and identity."""
    return (str(item), observed.st_size, observed.st_mtime_ns, observed.st_ino, observed.st_dev)


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
def regular_source(path: Path, expect=None):
    """Open one source observation without following or blocking on special files.

    ``expect`` is the identity an earlier observation of this run recorded for
    the same name; a different file there is a source change, not new input.
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
        with os.fdopen(descriptor, "rb") as handle:
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


def cursor_selection(path: Path, *, select_saved=False, anchors=None):
    """Never restore index-derived pins onto a different representation.

    Returns ``(source, state, observation)``: the saved state this call
    validated and parsed (``{}`` when the file was absent) and the state
    file's identity as seen by the very read that made this selection, in
    source_revision()'s shape (``None`` when absent). The caller binds the
    selection to its revision baseline through that observation and applies
    the state without any path being reopened afterwards.
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
            saved_text = handle.read().decode("utf-8")
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
            stores = safe_paths(cursor_reader._CHATS, ("*", sid, "store.db"), lambda error: None)
        else:
            # An explicit path is an intentional selection, aliases included;
            # only a second distinct store of the same session is ambiguous.
            stores = list({item.resolve(strict=True): item
                           for item in cursor_reader._CHATS.glob(f"*/{sid}/store.db")
                           if item.is_file()}.values())
        saved = stores[0] if len(stores) == 1 else None
    elif kind == "transcript":
        root = anchors.get(cursor_reader._PROJECTS) if anchors else None
        saved, _ = _valid_transcript_path(state.get("transcript_path"), sid, root=root)
    else:
        saved = None
    if saved is not None:
        saved = saved.resolve(strict=True)
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "cursor"), required=True)
    parser.add_argument("--since", type=since_instant)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--session", type=session_selector,
                        help="Select one native session ID, latest, or an explicit path")
    args = parser.parse_args(argv)
    reader = reader_for(args.host)
    explicit_path = False
    latest = None
    latest_seen = None
    latest_basis = None
    incomplete = False
    cursor_counts = Counter()

    def diagnostic(code: str, path=None):
        nonlocal incomplete
        incomplete = True
        # Static codes keep parser errors and transcript excerpts out of diagnostics.
        print(json.dumps({"type": "diagnostic", "host": args.host,
                          "code": code, "path": str(path) if path else None}), file=sys.stderr)

    try:
        path_syntax = bool(args.session and any(mark in args.session for mark in ("/", "\\")))
        explicit_path = args.session is not None and args.session != "latest" and path_syntax
        # Where each configured root leads, recorded once before any lookup:
        # discovery rechecks and transcript classification both use it.
        anchors = root_anchors(reader, lambda error: diagnostic("discovery_incomplete"))
        if explicit_path:
            path, error = reader.locate(args.session)
            if error or path is None:
                diagnostic("session_unavailable")
                return 2
            sessions = [{"path": str(path)}]
        else:
            options = {"include_representations": True} if args.host == "cursor" else {}
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
                    if path.name != "store.db" and not (
                            _UUID_RE.fullmatch(path.stem) and path.stem == path.parent.name):
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
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RuntimeError):
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
    prepared = []
    counts = Counter()
    for session in sessions:
        path = Path(session["path"])
        try:
            # An explicit path is the caller's own alias to follow; a
            # discovered row must still be the regular file discovery saw,
            # and that identity anchors every later read of it.
            if explicit_path:
                path, anchor = path.resolve(strict=True), None
            else:
                path, anchor = discovered_path(reader, path, anchors)
            if args.host == "cursor":
                from cursor_flush import _UUID_RE
                select_saved = not args.session or args.session == "latest" or bool(_UUID_RE.fullmatch(args.session))
                path, _, seen = cursor_selection(path, select_saved=select_saved, anchors=anchors)
                if not explicit_path:
                    anchor = discovered_source(reader, path, discovered, anchors)
                    if anchor is None:
                        raise ValueError("saved Cursor source was excluded by discovery")
            revision = source_revision(path, args.host, expect=anchor)
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
                with regular_source(path, revision_identity(revision, path)) as (handle, _):
                    source_header = reader._session_header(handle)
                sid = native_text(source_header.get("payload", {}).get("id"), required=True)
                native = None
            else:
                meta_text = None
                if path.name == "store.db":
                    meta = path.parent / "meta.json"
                    with regular_source(meta, revision_identity(revision, meta)) as (handle, _):
                        meta_text = handle.read().decode("utf-8")
                native = reader.session_metadata(path, meta_text=meta_text,
                                                 projects_root=anchors.get(reader._PROJECTS))
                sid = native_text(native.get("session_id"), required=True)
            # A readable identity still collides when another header field is bad.
            counts[f"{reader.HOST}-{sid}"] += 1
            if args.host == "codex" and args.session and not explicit_path:
                # Identity must participate in ambiguity checks, but an unrelated
                # header must not make an otherwise healthy selection incomplete.
                if args.session == "latest":
                    if path != latest:
                        continue
                elif sid != args.session.removesuffix(".jsonl"):
                    continue
            if native is None:
                native = reader._metadata_from_header(source_header)
            header = header_for(reader, path, mtime, native)
            prepared.append((path, revision, header))
        except SourceChanged:
            diagnostic("source_changed", path)
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                OverflowError, RuntimeError, argparse.ArgumentTypeError):
            diagnostic("session_unreadable", path)
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
    titles = (TitleIndex(reader, {item[2]["native_session_id"] for item in prepared})
              if args.host == "codex" and not args.metadata_only else None)
    for path, revision, header in prepared:
        if counts[header["conversation_id"]] > 1:
            diagnostic("discovery_incomplete", path)
            continue
        try:
            if titles is None and args.since is not None and header["mtime"] < args.since:
                if source_revision(path, args.host) != revision:
                    diagnostic("source_changed", path)
                continue
            records = []
            used_title = []
            if not args.metadata_only:
                with source_snapshot(path, args.host, revision) as snapshot:
                    def fallback_title(sid):
                        observation = titles.get(sid)
                        # Remember the index stamp too: an undated row keeps
                        # its tuple across unrelated index changes, but its
                        # effective mtime came from that stamp.
                        used_title.append((observation, titles.stamp))
                        header["mtime"] = max(header["mtime"], titles.mtime(observation))
                        return observation[0]
                    options = {"title_index": fallback_title} if titles is not None else {}
                    records, native = reader.to_canonical(snapshot, strict=True, **options)
                if native.get("session_id") != header["native_session_id"]:
                    raise ValueError("native identity changed during read")
                if args.host == "cursor":
                    from cursor_flush import apply_session_state
                    # Revalidate against the state covered by this revision and
                    # apply the state that validation parsed; an absent file is
                    # an explicit empty state, so no path is reopened here. The
                    # state read must be the one the baseline observed.
                    _, saved_state, seen = cursor_selection(path, anchors=anchors)
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
            if source_revision(path, args.host) != revision or (
                    latest_basis is not None and not basis_unchanged(latest_basis)):
                diagnostic("source_changed", path)
                continue
            if args.since is not None and header["mtime"] < args.since:
                continue
            # Validate the entire session before emitting its header. A bad
            # number or unsupported value cannot leave a partial session behind.
            lines = [encode(header)] + [encode(record) for record in records]
            for line in lines:
                print(line)
        except SourceChanged:
            diagnostic("source_changed", path)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
                OverflowError, RuntimeError, argparse.ArgumentTypeError):
            diagnostic("session_unreadable", path)
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
