#!/usr/bin/env python3
"""Every file read/write in the shipped scripts must name its encoding.

Run: python3 plugins/memhub/scripts/encoding_test.py   (stdlib only)

Python decodes with the OS LOCALE codec when `encoding=` is omitted. On a
zh-TW Windows box that is cp950, and a Claude Code transcript — always UTF-8 —
carries em-dashes and ellipses in ordinary prose, so byte 0xe2 raises
UnicodeDecodeError. Reported from the field on v0.20.0: `import_session.py`
died on every run, and the same bare `open()` in `flush_session.py` meant the
commit/PR flush and the SessionEnd backstop had been failing silently on that
machine the whole time.

There is no user-side workaround worth having: Claude Code is the primary
caller and its permission classifier refuses both `PYTHONUTF8=1 uv run …` and
`python -X utf8`, so the encoding has to be pinned in the scripts.

This is a source lint, not a behavioural test, because the failure only
reproduces under a non-UTF-8 locale — which CI and every dev Mac here are not.
A grep-able invariant that holds on any machine beats a test that can only fail
on the one box we don't have.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "plugins" / "memhub" / "scripts", ROOT / "codex"]

FAILURES: list[str] = []


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _is_binary_open(call: ast.Call) -> bool:
    """`open(path, "rb")` needs no encoding — bytes are never decoded."""
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and "b" in mode


def check_file(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Path.read_text / Path.write_text — always text, always decodes.
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            if not _has_kw(node, "encoding"):
                FAILURES.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"bare .{func.attr}() — pass encoding=\"utf-8\""
                )
        # Builtin open() in text mode. `os.open` is an Attribute, not a Name,
        # so the fd-level lock helpers are correctly ignored here.
        elif isinstance(func, ast.Name) and func.id == "open":
            if not _is_binary_open(node) and not _has_kw(node, "encoding"):
                FAILURES.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"bare open() in text mode — pass encoding=\"utf-8\""
                )


def main() -> int:
    scanned = 0
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.py")):
            # Tests read their own fixtures and may deliberately write odd
            # bytes; the invariant is about what SHIPS and runs on user boxes.
            if path.name.endswith("_test.py"):
                continue
            check_file(path)
            scanned += 1

    if FAILURES:
        print(f"FAIL — {len(FAILURES)} unpinned encoding site(s) in {scanned} files:")
        for f in FAILURES:
            print(f"  {f}")
        print("\nThese crash on any non-UTF-8 default locale (cp950, cp1252, …).")
        return 1
    print(f"PASS — {scanned} shipped scripts pin their encodings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
