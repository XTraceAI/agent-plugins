#!/usr/bin/env python3
"""Splitting a transcript into payloads a single call can carry.

A whole-transcript upload is one `import_conversation` call, and real sessions
outgrow what one call can carry. Measured over 185 local transcripts: median
652 KB, but **74 exceed 1 MB, 25 exceed 5 MB, and the largest is 46 MB**. A
path that sends the file in one payload therefore works on most sessions and
fails on exactly the long ones — the sessions with the most worth keeping.

Slices are CONSECUTIVE and DISJOINT: each is its own incremental import
against the same conversation, so no record is extracted twice regardless of
how the server's watermark happens to be positioned when a slice lands.

Stdlib only, no import-time side effects — the capture hooks import this.
"""
from __future__ import annotations

import json

# Comfortably under a request-size ceiling while keeping slice count low, and
# the same figure ``import_session`` has defaulted to.
DEFAULT_CHUNK_BYTES = 3_500_000


def slices(records: list, chunk_bytes: int = DEFAULT_CHUNK_BYTES,
           max_slices: int | None = None) -> list[list]:
    """``records`` split into consecutive disjoint runs under ``chunk_bytes``.

    A single record larger than the budget still goes through alone: splitting
    inside a record would corrupt it, and one oversized payload that the server
    may reject beats silently dropping the record.

    ``max_slices`` stops after that many payloads and discards the rest, for a
    caller that will only send the first few. ``flush_turn`` sends exactly one
    slice per turn: without this it re-serialized the WHOLE pending backlog on
    every turn — 46 MB on the largest transcript measured here — only to throw
    all but the first slice away, inside a 60s budget. The result is still a
    PREFIX of ``records``, so a caller detects truncation by comparing lengths.
    """
    if chunk_bytes <= 0:
        return [list(records)] if records else []
    if max_slices is not None and max_slices <= 0:
        return []
    out: list[list] = []
    cur: list = []
    size = 0
    for rec in records:
        b = len(json.dumps(rec, separators=(",", ":")))
        if cur and size + b > chunk_bytes:
            out.append(cur)
            if max_slices is not None and len(out) >= max_slices:
                return out
            cur, size = [], 0
        cur.append(rec)
        size += b
    if cur:
        out.append(cur)
    return out
