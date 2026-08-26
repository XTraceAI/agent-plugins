#!/usr/bin/env python3
"""One-shot: migrate ledger/fires.jsonl from v1 (one row per tool call,
`rules: [...]`) to v2 (one row per rule per fire, spec §3.2). Idempotent —
refuses to run when ledger/schema_version already says 2. The original is
kept as fires.v1.jsonl. Stdlib only.

Usage: rulebook_ledger_migrate.py [--ledger ~/.claude/scripts/rulebook/ledger]
"""
import argparse
import json
import os
import sys
import uuid


def migrate(ledger_dir):
    os.makedirs(ledger_dir, exist_ok=True)      # first run: no ledger dir yet
    sv = os.path.join(ledger_dir, "schema_version")
    src = os.path.join(ledger_dir, "fires.jsonl")
    if os.path.exists(sv) and open(sv, encoding="utf-8").read().strip() == "2":
        return "already v2"
    if not os.path.exists(src):
        with open(sv, "w", encoding="utf-8") as f:
            f.write("2\n")
        return "no v1 ledger; stamped v2"
    rows, bad = [], 0
    with open(src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1                     # one corrupt line must not abort the migration
    out = []
    for r in rows:
        if "fire_id" in r:                       # already v2-shaped
            out.append(r)
            continue
        phase = r.get("mode", "pre")             # v1 `mode` was the hook phase
        for rid in r.get("rules", []):
            out.append({
                "fire_id": str(uuid.uuid4()), "rule_id": rid,
                "rule_version": "pilot-<=3", "session_id": r.get("session", ""),
                "agent_id": None, "repo": r.get("repo"), "branch": None,
                "tool": r.get("tool"), "hook_phase": phase, "mode": "advise",
                "dedup_key": None, "raw_matches_before_fire": None,
                "fired_at": r.get("ts"), "converted": None, "converted_at": None,
                "excerpt": r.get("excerpt", ""),
                "migrated_from": "v1",
            })
    # crash-safe order: the v2 file is fully written to a temp name BEFORE the
    # v1 file moves aside; an interruption anywhere leaves either the intact
    # v1 file or both files, never neither.
    tmp = src + ".v2.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    os.replace(src, os.path.join(ledger_dir, "fires.v1.jsonl"))
    os.replace(tmp, src)
    with open(sv, "w", encoding="utf-8") as f:
        f.write("2\n")
    return f"migrated {len(rows)} v1 rows -> {len(out)} v2 rows" + (f" ({bad} unparseable line(s) skipped)" if bad else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.expanduser("~/.claude/scripts/rulebook/ledger"))
    a = ap.parse_args()
    print(migrate(a.ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
