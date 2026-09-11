#!/usr/bin/env python3
"""Run every plugin self-test.

These live here rather than beside the code they test because the plugin
directory is COPIED VERBATIM into every user's install
(``~/.claude/plugins/cache/…/<version>/``). Colocated tests shipped ~176K of
test code to every machine that installed the plugin, which nothing there ever
runs. They stay in the repo — public, where CI and contributors need them —
just not inside the artifact.

Most suites are stdlib-only by design, so they can be run under a bare
``python3``. A few import the mcp SDK; run this the way CI does to cover
those too:

    uv run --with 'mcp<2' python tests/run_all.py

Each suite runs under its own throwaway ``$HOME``, and a suite that WRITES into
it fails. The capture scripts resolve their state and breadcrumb directories
from ``Path.home()`` at import time, so a suite that forgets to redirect HOME
itself appends synthetic breadcrumbs to the developer's real
``~/.config/memhub-plugin`` — which two suites did, for long enough that their
output was mistaken for a live production failure (ENG-1034). The throwaway
HOME keeps this runner hermetic; the write check is what catches the next
suite that would have leaked when run on its own.

Exits non-zero if any suite fails, printing that suite's tail.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows consoles default to a legacy codepage (cp1252) that cannot encode
# the suites' own output (arrows, em-dashes, replacement chars). Reconfigure
# OUR stdio rather than requiring every caller to remember PYTHONUTF8=1.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TESTS = Path(__file__).resolve().parent


def run_suite(suite: Path) -> tuple[bool, str, list[str]]:
    """Run one suite under a throwaway HOME: ``(ok, output, written)``.

    ``written`` lists every file the suite left in that HOME (POSIX paths
    relative to it). A non-empty list fails the suite even when it exited 0 —
    run directly, those writes would have landed in the real home directory.
    """
    home = tempfile.mkdtemp(prefix="plugin-suite-home-")
    try:
        # PYTHONUTF8 for the CHILD: a suite that prints "→" into a piped
        # stdout dies on Windows' locale codec inside its own process — no
        # amount of decode tolerance on OUR side can fix the child crashing.
        # HOME and USERPROFILE both: POSIX expanduser reads HOME, Windows
        # reads USERPROFILE and never consults HOME.
        # PYTHONDONTWRITEBYTECODE: macOS's Command Line Tools python3 sets
        # sys.pycache_prefix under $HOME/Library/Caches, so without it every
        # child writes stdlib bytecode into the throwaway HOME and fails.
        result = subprocess.run([sys.executable, str(suite)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONUTF8": "1",
                                     "PYTHONDONTWRITEBYTECODE": "1",
                                     "HOME": home, "USERPROFILE": home,
                                     "XDG_CONFIG_HOME": os.path.join(home, ".config")})
        written = sorted(p.relative_to(home).as_posix()
                         for p in Path(home).rglob("*") if p.is_file())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    ok = result.returncode == 0 and not written
    return ok, result.stdout + result.stderr, written


def main(tests_dir: Path = TESTS) -> int:
    suites = sorted(tests_dir.glob("*_test.py"))
    if not suites:
        print("no test suites found", file=sys.stderr)
        return 1

    failed: list[str] = []
    for suite in suites:
        ok, output, written = run_suite(suite)
        if not ok:
            failed.append(suite.name)
            print(f"\n{'=' * 60}\n{suite.name}\n{'=' * 60}")
            print(output[-2000:])
            if written:
                print(f"{suite.name} wrote into $HOME — a suite must redirect "
                      "HOME/USERPROFILE to its own temp dir before importing "
                      "the code under test (see tests/flush_session_test.py):")
                for path in written[:10]:
                    print(f"  {path}")
        print(f"{'PASS' if ok else 'FAIL'}  {suite.name}")

    print()
    if failed:
        print(f"{len(failed)} of {len(suites)} suites FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(suites)} suites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
