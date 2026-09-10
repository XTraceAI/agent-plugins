#!/usr/bin/env python3
"""Read-only native session headers and canonical JSONL for local consumers."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import datetime
import json
import math
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from readers import reader_for, validate_canonical


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


def source_revision(path: Path, host: str) -> tuple:
    paths = [path]
    if host == "codex":
        from readers.codex import _SESSION_INDEX
        paths.append(_SESSION_INDEX)  # Desktop title changes need a new revision.
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
            stat = item.stat()
            with item.open("rb"):
                pass
        except FileNotFoundError:
            if item == path:
                raise
            continue
        revision.append((str(item), stat.st_size, stat.st_mtime_ns, stat.st_ino))
    return tuple(revision)


def cursor_source(path: Path, *, select_saved=False) -> Path:
    """Never restore index-derived pins onto a different representation."""
    from cursor_flush import _read_state, _UUID_RE, _source_for
    sid = path.parent.name if path.name == "store.db" else path.stem
    if not _UUID_RE.fullmatch(sid):
        return path
    state = _read_state(sid, strict=True)
    kind = state.get("source_kind")
    if kind is None:
        if state.get("usage_events") or any(state.get("record_ts", {}).values()):
            raise ValueError("saved observations have no source provenance")
        return path
    if kind in {"store", "transcript"}:
        _, saved, error = _source_for(sid, {}, state)
        if saved is not None:
            saved = saved.resolve(strict=True)
            if select_saved or saved == path:
                return saved
    raise ValueError("saved observations belong to another source")


def header_for(reader, path: Path, mtime: float) -> dict:
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


@contextmanager
def source_snapshot(path: Path, host: str):
    if host != "cursor" or path.name != "store.db":
        yield path
        return
    # SQLite mode=ro may still create an SHM file. Copy stable source bytes to
    # a private snapshot so journal handling never writes in the native store.
    # The caller compares source revisions before and after the entire read.
    with tempfile.TemporaryDirectory(prefix="native-reader-") as temporary:
        directory = Path(temporary) / path.parent.name
        directory.mkdir(mode=0o700)
        for name in ("store.db", "store.db-wal", "store.db-journal", "meta.json"):
            source, target = path.parent / name, directory / name
            try:
                with source.open("rb") as handle:
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
    parser.add_argument("--session", help="Select one native session ID, latest, or an explicit path")
    args = parser.parse_args(argv)
    reader = reader_for(args.host)
    explicit_path = False
    latest = None
    incomplete = False

    def diagnostic(code: str, path=None):
        nonlocal incomplete
        incomplete = True
        # Static codes keep parser errors and transcript excerpts out of diagnostics.
        print(json.dumps({"type": "diagnostic", "host": args.host,
                          "code": code, "path": str(path) if path else None}), file=sys.stderr)

    try:
        explicit_path = (args.session is not None and args.session != "latest"
                         and ("/" in args.session or Path(args.session).expanduser().exists()))
        if explicit_path:
            path, error = reader.locate(args.session)
            if error or path is None:
                diagnostic("session_unavailable")
                return 2
            sessions = [{"path": str(path)}]
        else:
            sessions = reader.list_sessions(None, on_error=lambda error: diagnostic("discovery_incomplete"))
            if args.session == "latest":
                latest, error = reader.locate("latest")
                if error or latest is None:
                    diagnostic("session_unavailable")
                    return 2
                latest = latest.resolve(strict=True)
                if args.host == "cursor":
                    latest = cursor_source(latest, select_saved=True)
                    # Discovery prefers stores, whereas native latest may
                    # select a newer transcript of the same session. Replace
                    # that one representation without hiding duplicate stores.
                    def cursor_id(path):
                        path = Path(path)
                        return path.parent.name if path.name == "store.db" else path.stem
                    matches = [row for row in sessions
                               if cursor_id(row["path"]) == cursor_id(latest)]
                    if len(matches) == 1:
                        sessions[sessions.index(matches[0])] = {"path": str(latest)}
                if not any(Path(row["path"]).resolve() == latest for row in sessions):
                    sessions.append({"path": str(latest)})
    except (OSError, ValueError, TypeError, AttributeError):
        diagnostic("discovery_incomplete")
        return 2

    prepared = []
    for session in sessions:
        path = Path(session["path"])
        try:
            path = path.resolve(strict=True)
            if args.host == "cursor":
                from cursor_flush import _UUID_RE
                select_saved = not args.session or args.session == "latest" or bool(_UUID_RE.fullmatch(args.session))
                path = cursor_source(path, select_saved=select_saved)
            revision = source_revision(path, args.host)
            mtime = max(item[2] for item in revision) / 1_000_000_000
            if not math.isfinite(mtime):
                raise ValueError("invalid mtime")
            header = header_for(reader, path, mtime)
            prepared.append((path, revision, header))
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                OverflowError, RecursionError):
            diagnostic("session_unreadable", path)
    # Reject every candidate sharing an actual native identity before emitting
    # any of them. File names alone do not establish Codex session identity.
    counts = Counter(header["conversation_id"] for _, _, header in prepared)
    if args.session == "latest":
        # Preserve the native latest-selection rule, but only after all actual
        # identities have participated in ambiguity detection.
        try:
            prepared = [item for item in prepared if item[0] == latest]
            if not prepared:
                diagnostic("session_unavailable")
                return 2
        except (OSError, ValueError, TypeError, AttributeError):
            diagnostic("session_unavailable")
            return 2
    elif args.session and not explicit_path:
        native_id = args.session.removesuffix(".jsonl") if args.host == "codex" else args.session
        prepared = [item for item in prepared if item[2]["native_session_id"] == native_id]
        if not prepared:
            diagnostic("session_unavailable")
            return 2
    for path, revision, header in prepared:
        if counts[header["conversation_id"]] > 1:
            diagnostic("discovery_incomplete", path)
            continue
        try:
            if args.since is not None and header["mtime"] < args.since:
                if source_revision(path, args.host) != revision:
                    diagnostic("source_changed", path)
                continue
            records = []
            if not args.metadata_only:
                with source_snapshot(path, args.host) as snapshot:
                    records, native = reader.to_canonical(snapshot, strict_utf8=True, strict_json=True)
                if native.get("session_id") != header["native_session_id"]:
                    raise ValueError("native identity changed during read")
                if args.host == "cursor":
                    from cursor_flush import apply_session_state
                    cursor_source(path)  # Revalidate against the state covered by this revision.
                    apply_session_state(records, header["native_session_id"], strict=True)
                if records and validate_canonical(records):
                    raise ValueError("reader emitted invalid canonical records")
                header["title"] = native_text(native.get("title"))
            if source_revision(path, args.host) != revision:
                diagnostic("source_changed", path)
                continue
            # Validate the entire session before emitting its header. A bad
            # number or unsupported value cannot leave a partial session behind.
            lines = [encode(header)] + [encode(record) for record in records]
            for line in lines:
                print(line)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
                OverflowError, RecursionError):
            diagnostic("session_unreadable", path)
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
