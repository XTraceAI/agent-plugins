#!/usr/bin/env python3
"""Slicing a transcript into sendable payloads.

Run: python3 plugins/memhub/scripts/transcript_chunks_test.py

The property that matters is DISJOINTNESS: each slice is its own incremental
import against one conversation, so a record appearing in two slices would be
extracted twice, and a record in none would be lost with nothing to notice it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from transcript_chunks import DEFAULT_CHUNK_BYTES, slices  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(name)


def rec(i: int, pad: int = 0) -> dict:
    return {"type": "user", "uuid": f"u{i}", "text": "x" * pad}


def sizeof(record) -> int:
    return len(json.dumps(record, separators=(",", ":")))


# ── the invariants ────────────────────────────────────────────────────

records = [rec(i, 100) for i in range(50)]
out = slices(records, 1000)

check("every record appears", [r for s in out for r in s] == records)
check("slices are disjoint",
      sum(len(s) for s in out) == len(records))
check("order is preserved",
      [r["uuid"] for s in out for r in s] == [r["uuid"] for r in records])
check("it actually split", len(out) > 1)
check("no slice exceeds the budget once it holds more than one record",
      all(sum(sizeof(r) for r in s) <= 1000 for s in out if len(s) > 1))

check("an empty transcript yields no slices", slices([], 1000) == [])
check("a small transcript stays whole",
      slices(records, 10_000_000) == [records])

# Splitting inside a record would corrupt it, so an oversized record rides
# alone rather than being dropped or cut.
big = rec(99, 5000)
out = slices([rec(1, 10), big, rec(2, 10)], 500)
check("an oversized record goes through alone", [big] in out)
check("an oversized record does not swallow its neighbours",
      [r for s in out for r in s] == [rec(1, 10), big, rec(2, 10)])

# A non-positive budget must not loop forever or return nothing.
check("a zero budget degrades to one slice",
      slices(records, 0) == [records])
check("a zero budget on empty stays empty", slices(0 * [1], 0) == [])

check("the default budget is a real size", DEFAULT_CHUNK_BYTES > 100_000)


# ── against the real transcripts on this machine ──────────────────────

root = Path.home() / ".claude" / "projects"
real = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_size,
              reverse=True)[:5] if root.is_dir() else []
if real:
    split_count = 0
    for path in real:
        records = []
        for line in open(path, "rb"):
            try:
                records.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        if not records:
            continue
        out = slices(records)
        if len(out) > 1:
            split_count += 1
        check(f"{path.name[:8]}: every record survives slicing",
              [r for s in out for r in s] == records)
        check(f"{path.name[:8]}: slices stay under budget",
              all(sum(sizeof(r) for r in s) <= DEFAULT_CHUNK_BYTES
                  for s in out if len(s) > 1))
        print(f"  {path.stat().st_size / 1e6:6.1f} MB -> {len(out)} slice(s)")
    # The whole reason this module exists: the biggest local sessions do not
    # fit one payload. If none of them split, the budget is not being applied.
    check("the largest real sessions do need splitting", split_count > 0)
else:
    print("real transcripts: none found, skipped")


print(f"{'FAIL' if FAILURES else 'PASS'}: transcript_chunks")
for f in FAILURES:
    print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
