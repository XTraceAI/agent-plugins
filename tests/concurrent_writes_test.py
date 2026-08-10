"""Concurrent writers must never publish a spliced file.

Every credential and cursor this plugin keeps is written by more than one
process now: the per-turn flush, the SessionEnd backstop (which does NOT take
the per-turn flock), and the PreToolUse directive check that fires on every
edit. A shared temp path makes "atomic" a misnomer — two writers open the same
temp with O_TRUNC, interleave, and rename a half-and-half document.

The failure is silent in the worst way. A reader that catches a spliced token
cache does not error; it decides there is no usable credential and skips, which
looks exactly like "not logged in". A spliced turnflush state means a corrupt
cursor.

These drive the real writers from real subprocesses, because the bug only
exists between processes — a threaded test would share a GIL and could pass
while the shipped code is broken.

Run: python3 tests/concurrent_writes_test.py  (stdlib only, no network).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"

WRITERS = 12
ROUNDS = 25

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


# Each subprocess hammers one writer; the parent then checks that every
# published document is complete and parseable.
_CHILD = r"""
import json, os, sys
sys.path.insert(0, {scripts!r})
os.environ["HOME"] = {home!r}
target, rounds, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]

if target == "state":
    import flush_turn as m
    from pathlib import Path
    m.STATE_DIR = Path({home!r}) / "turnflush"
    for i in range(rounds):
        m._save_state("sess", offset=i, writer=tag, padding="x" * 4000)
else:
    import pak
    from pathlib import Path
    pak.CACHE_DIR = Path({home!r})
    for i in range(rounds):
        pak.save("https://api.example.com/mcp-server/mcp",
                 {{"secret": "mhk_" + tag * 40, "label": tag, "n": i,
                   "padding": "y" * 4000}})
"""


def _run(target: str, home: Path) -> Path:
    child = _CHILD.format(scripts=str(SCRIPTS), home=str(home))
    procs = [
        subprocess.Popen([sys.executable, "-c", child, target, str(ROUNDS), f"w{i}"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(WRITERS)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        if p.returncode != 0:
            failures.append(f"{target} writer failed: {err.decode()[-300:]}")
    return home


def test_turnflush_state_survives_concurrent_writers():
    print("\nturnflush state under concurrent writers")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _run("state", home)
        published = home / "turnflush" / "sess.json"
        check("a document was published", published.exists(), True)
        try:
            state = json.loads(published.read_text(encoding="utf-8"))
            check("published document parses", isinstance(state, dict), True)
            # A spliced file would most likely fail to parse; if it somehow
            # parsed, the padding proves it came from one writer whole.
            check("padding is intact", len(state.get("padding", "")), 4000)
        except ValueError as exc:
            check(f"published document parses ({exc})", False, True)
        # No temp debris left behind.
        leftovers = sorted(p.name for p in (home / "turnflush").glob("*.tmp"))
        check("no temp files left", leftovers, [])


def test_key_cache_survives_concurrent_writers():
    print("\naccess-key cache under concurrent writers")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        _run("pak", home)
        published = home / "pak-api.example.com.json"
        check("a document was published", published.exists(), True)
        try:
            record = json.loads(published.read_text(encoding="utf-8"))
            check("published document parses", isinstance(record, dict), True)
            # The sharper invariant than any length: the secret is built from
            # the writer's own tag, so a document whose secret and label
            # disagree is one that was spliced from two writers.
            check("secret and label come from the SAME writer",
                  record.get("secret"), "mhk_" + record.get("label", "") * 40)
            check("padding is intact", len(record.get("padding", "")), 4000)
        except ValueError as exc:
            check(f"published document parses ({exc})", False, True)
        check("mode is still 0600",
              oct(published.stat().st_mode)[-3:], "600")
        leftovers = sorted(p.name for p in home.glob("*.tmp"))
        check("no temp files left", leftovers, [])


if __name__ == "__main__":
    for test in (test_turnflush_state_survives_concurrent_writers,
                 test_key_cache_survives_concurrent_writers):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall concurrent-write checks passed")
