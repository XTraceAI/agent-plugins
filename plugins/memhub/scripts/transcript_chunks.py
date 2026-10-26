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


def slices(records: list, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> list[list]:
    """``records`` split into consecutive disjoint runs under ``chunk_bytes``.

    A single record larger than the budget still goes through alone: splitting
    inside a record would corrupt it, and one oversized payload that the server
    may reject beats silently dropping the record.
    """
    if chunk_bytes <= 0:
        return [list(records)] if records else []
    out: list[list] = []
    cur: list = []
    size = 0
    for rec in records:
        b = len(json.dumps(rec, separators=(",", ":")))
        if cur and size + b > chunk_bytes:
            out.append(cur)
            cur, size = [], 0
        cur.append(rec)
        size += b
    if cur:
        out.append(cur)
    return out
