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

Exits non-zero if any suite fails, printing that suite's tail.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def main() -> int:
    suites = sorted(TESTS.glob("*_test.py"))
    if not suites:
        print("no test suites found", file=sys.stderr)
        return 1

    failed: list[str] = []
    for suite in suites:
        result = subprocess.run([sys.executable, str(suite)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        ok = result.returncode == 0
        if not ok:
            failed.append(suite.name)
            print(f"\n{'=' * 60}\n{suite.name}\n{'=' * 60}")
            print((result.stdout + result.stderr)[-2000:])
        print(f"{'PASS' if ok else 'FAIL'}  {suite.name}")

    print()
    if failed:
        print(f"{len(failed)} of {len(suites)} suites FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(suites)} suites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
