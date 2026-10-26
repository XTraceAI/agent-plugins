"""Per-host session readers — the normalization boundary for multi-host capture.

Each supported host stores agent sessions in its own place and shape (Claude
Code: ``~/.claude/projects/<cwd>/<id>.jsonl``; Codex:
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``). Everything downstream of
this package — flush, redaction, chunking, import — speaks exactly one shape:
the Claude Code record shape, because MemHub's agentic ingestion detects it by
*structure*, not by a platform tag. All host knowledge is quarantined here, so
a host changing its (mostly undocumented) storage breaks one module with its
own fixtures, never the pipeline.

Every reader module exposes the same surface:

    HOST: str                                  # registry key, conv-id prefix
    list_sessions(limit) -> list[dict]         # {id, path, mtime, cwd, host}
    locate(ref) -> (Path | None, err: str)     # ref = path | bare id | "latest"
    to_canonical(path) -> (records, meta)      # Claude-shaped records +
                                               # {session_id, cwd, title, ...}

``conversation_id`` for non-Claude hosts is namespaced ``<host>-<session-id>``
(established by the Codex importer) so the server-side watermark folds
re-imports forward per host instead of colliding across hosts.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from . import claude, codex, cursor

READERS = {m.HOST: m for m in (claude, codex, cursor)}


def reader_for(host: str):
    """The reader module for ``host``, or None."""
    return READERS.get(host)


def sniff(ref: str) -> str | None:
    """Best-effort host detection from how a session is addressed.

    Path shapes are unambiguous (rollout filename / .claude location). Bare ids
    and ``latest`` have no shape, so return None; capture.py may safely probe a
    bare id across readers, but requires an explicit host for ``latest`` or a
    cross-host collision.
    """
    if not ref or ref == "latest":
        return None
    p = Path(ref).expanduser()
    name = p.name
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return codex.HOST
    parts = p.parts
    if name == "store.db" or ".cursor" in parts:
        return cursor.HOST
    if ".codex" in parts:
        return codex.HOST
    if ".claude" in parts:
        return claude.HOST
    if name.endswith(".jsonl") and p.is_file():
        # An explicit transcript path outside both homes: claude-shaped files
        # are the native input format, so default to claude.
        return claude.HOST
    return None


def _parseable_timestamp(value) -> bool:
    """Would the server's ``_parse_ts`` read this? (ISO-8601, ``Z`` ok.)"""
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_canonical(records: list[dict]) -> list[str]:
    """Structural check that ``records`` will trip the agentic detector.

    Returns a list of problems (empty = valid). This is the contract every
    reader's ``to_canonical`` must satisfy; the cross-reader test asserts it,
    and a reader can call it defensively after transforming a format it is
    less sure about.
    """
    problems: list[str] = []
    if not records:
        return ["no records"]
    for i, r in enumerate(records):
        msg = r.get("message")
        if r.get("type") not in ("user", "assistant"):
            problems.append(f"record {i}: type={r.get('type')!r}")
            continue
        # The agentic parser SKIPS records without a uuid (replay-dedup key) —
        # verified against the backend: uuid-less records import as NOTHING,
        # silently. ``timestamp`` is different: the server dates turns from it
        # when present and stores NULL event_date ("unmeasured") when absent —
        # a legitimate state per the claude_parts contract (ENG-675b). So a
        # MISSING timestamp is not a problem (fabricating one at read time is
        # the actual bug — it collapses a session onto flush-adjacent clocks),
        # but a PRESENT-yet-unparseable one is: the server would silently null
        # it, which always means a reader formatting regression.
        if not r.get("uuid"):
            problems.append(f"record {i}: missing uuid (server skips it)")
        ts = r.get("timestamp")
        if ts is not None and not _parseable_timestamp(ts):
            problems.append(
                f"record {i}: unparseable timestamp {ts!r} "
                f"(server would null event_date)")
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            problems.append(f"record {i}: missing message/role")
            continue
        content = msg.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            problems.append(f"record {i}: content is {type(content).__name__}")
            continue
        for b in content:
            if not isinstance(b, dict) or "type" not in b:
                problems.append(f"record {i}: malformed content block")
                break
            if b["type"] == "tool_use" and not b.get("id"):
                problems.append(f"record {i}: tool_use without id")
            if b["type"] == "tool_result" and not b.get("tool_use_id"):
                problems.append(f"record {i}: tool_result without tool_use_id")
    return problems
