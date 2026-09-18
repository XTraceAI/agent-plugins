#!/usr/bin/env python3
"""The runner fails a suite that writes into $HOME (stdlib only).

Two suites once appended synthetic breadcrumbs to the developer's real
``~/.config/memhub-plugin`` on every local run, and the output was later read
as a live production failure (ENG-1034). ``run_all.run_suite`` now runs each
suite under a throwaway HOME and fails one that writes there; this pins that
the check actually trips, and that it never lets the write reach the real
home directory.

Run: python3 tests/run_all_sandbox_test.py
"""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

# This suite's own HOME is redirected first, so even a regression in the
# runner under test cannot reach the developer's real home directory.
_TMP_HOME = tempfile.mkdtemp(prefix="run-all-sandbox-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_all  # noqa: E402

LEAK = ".config/memhub-plugin/x/log"

CLEAN = 'print("ok")\n'
LEAKY = (
    "from pathlib import Path\n"
    f"target = Path.home() / {LEAK!r}\n"
    "target.parent.mkdir(parents=True, exist_ok=True)\n"
    "target.write_text('synthetic breadcrumb\\n')\n"
    "print('ok')\n"
)
BROKEN = "raise SystemExit(3)\n"


def _suites(*named: tuple[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="run-all-suites-"))
    atexit.register(shutil.rmtree, str(root), ignore_errors=True)
    for name, body in named:
        (root / name).write_text(body, encoding="utf-8")
    return root


def test_clean_suite_passes():
    root = _suites(("clean_test.py", CLEAN))
    ok, output, written = run_all.run_suite(root / "clean_test.py")
    assert ok, output
    assert written == [], written
    print("PASS test_clean_suite_passes")


def test_suite_writing_into_home_fails_and_names_the_file():
    root = _suites(("leaky_test.py", LEAKY))
    ok, output, written = run_all.run_suite(root / "leaky_test.py")
    # exit 0 is not enough: the write alone fails the suite
    assert "ok" in output, output
    assert not ok
    assert written == [LEAK], written
    # ...and the write went to the throwaway HOME, never this process's own
    assert not (Path(_TMP_HOME) / LEAK).exists()
    print("PASS test_suite_writing_into_home_fails_and_names_the_file")


def test_failing_suite_that_writes_nothing_still_fails():
    root = _suites(("broken_test.py", BROKEN))
    ok, _output, written = run_all.run_suite(root / "broken_test.py")
    assert not ok
    assert written == [], written
    print("PASS test_failing_suite_that_writes_nothing_still_fails")


def test_child_writes_no_bytecode():
    # macOS's Command Line Tools python3 points sys.pycache_prefix under
    # $HOME/Library/Caches: without this, importing anything would write .pyc
    # files into the throwaway HOME and fail every suite on that interpreter.
    root = _suites(("probe_test.py",
                    "import os, sys\n"
                    "print('dont_write', os.environ.get('PYTHONDONTWRITEBYTECODE'),"
                    " sys.dont_write_bytecode)\n"))
    ok, output, written = run_all.run_suite(root / "probe_test.py")
    assert ok, output
    assert "dont_write 1 True" in output, output
    assert written == [], written
    print("PASS test_child_writes_no_bytecode")


def test_main_reports_the_leaking_suite():
    root = _suites(("clean_test.py", CLEAN), ("leaky_test.py", LEAKY))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_all.main(root)
    text = out.getvalue()
    assert code == 1, text
    assert "PASS  clean_test.py" in text, text
    assert "FAIL  leaky_test.py" in text, text
    assert "leaky_test.py wrote into $HOME" in text, text
    assert LEAK in text, text
    assert "1 of 2 suites FAILED: leaky_test.py" in text, text
    print("PASS test_main_reports_the_leaking_suite")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
