"""Claude Code session reader — the identity end of the reader contract.

Claude transcripts ARE the canonical shape, so ``to_canonical`` is a tolerant
parse plus meta extraction, no transform. The value of having a reader at all
is uniformity: ``capture.py list/import`` addresses Claude sessions the same
way as any other host's, and the cross-reader contract test pins the claude
output as the reference the other readers must match.

Location facts (mirrors the import-session skill):
- Sessions live DIRECTLY under ``~/.claude/projects/<munged-cwd>/`` — ``.jsonl``
  files in subdirectories (``subagents/``, workflow dirs) are subagent
  transcripts, not sessions, and are never listed here.
- Large tool outputs spill into sidecar files; this reader ships the main
  transcript only (v1 policy: main trajectory only).
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

HOST = "claude"

_PROJECTS = Path.home() / ".claude" / "projects"


def _session_files() -> list[Path]:
    return [Path(f) for f in glob.glob(str(_PROJECTS / "*" / "*.jsonl"))]


def load(path) -> list[dict]:
    """Tolerant JSONL parse (skip malformed lines — e.g. a truncated final
    line from a crash mid-write). Explicit utf-8: a bare read_text() decodes
    with the OS locale codec and one em-dash kills the import on a cp950 box."""
    records: list[dict] = []
    for encoded in Path(path).read_bytes().splitlines():
        line = encoded.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _meta_of(path: Path, records: list[dict]) -> dict:
    cwd = None
    for r in records:
        if isinstance(r.get("cwd"), str):
            cwd = r["cwd"]
            break
    return {"session_id": path.stem, "cwd": cwd, "title": None, "host": HOST}


# The current directory is what `capture.py current` matches on, so the LAST
# record that names one wins: a session can `cd`, and where it ended is where
# it is running. Bounded on both ends — a transcript can exceed a gigabyte.
_CWD_TAIL_BYTES = 256 * 1024
_CWD_MAX_RECORDS = 200


def session_cwd(path) -> str | None:
    """The directory this session is running in, or None.

    Read from the top-level ``cwd`` key on transcript records, NOT from
    ``f.parent.name``: that is the encoded project directory, which collapses
    ``/`` to ``-`` and cannot be compared against a real path.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as handle:
            if size > _CWD_TAIL_BYTES:
                handle.seek(size - _CWD_TAIL_BYTES)
                handle.readline()          # drop a line the seek cut in half
            tail = handle.read()
    except OSError:
        return None
    for encoded in reversed(tail.splitlines()[-_CWD_MAX_RECORDS:]):
        line = encoded.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = record.get("cwd") if isinstance(record, dict) else None
        if isinstance(cwd, str) and cwd:
            return cwd
    if size <= _CWD_TAIL_BYTES:
        return None
    # A tail of nothing but huge tool results: the first record carries a cwd
    # too, and a session that never moved is the common case.
    try:
        with p.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(_CWD_MAX_RECORDS), handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def list_sessions(limit: int = 20) -> list[dict]:
    """Most recent sessions across every project, newest first."""
    files = sorted(_session_files(), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        out.append({"id": f.stem, "path": str(f),
                    "mtime": f.stat().st_mtime, "host": HOST,
                    "cwd": f.parent.name})
    return out


def locate(ref: str) -> tuple[Path | None, str]:
    """Accept a transcript path, ``latest``, or a bare session id."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p, ""
    if "/" in ref and ref != "latest":
        return None, f"transcript not found: {p}"
    files = _session_files()
    if not files:
        return None, f"no Claude sessions under {_PROJECTS}"
    if ref == "latest":
        return max(files, key=lambda f: f.stat().st_mtime), ""
    sid = ref.removesuffix(".jsonl")
    hits = [f for f in files if f.stem == sid]
    if not hits:
        return None, f"no Claude session {sid!r} under {_PROJECTS}"
    if len(hits) > 1:
        # One session id appearing under two project dirs would make the
        # choice change which repo's brain receives the import — refuse.
        return None, f"ambiguous session id {sid!r}: {len(hits)} matches — pass the path"
    return hits[0], ""


def to_canonical(path) -> tuple[list[dict], dict]:
    """Identity transform: parse and return, with meta."""
    p = Path(path)
    records = load(p)
    return records, _meta_of(p, records)
