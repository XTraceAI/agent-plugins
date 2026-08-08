"""Every test function in this directory must actually run.

This exists because one did not. `test_only_a_server_round_trip_retracts_a_
failure` was written in #53, described in that PR as the lock protecting the
retraction rule, and never registered in its file's hand-maintained tuple — so
it never executed once. The suite stayed green the whole time, just over less,
which is the failure mode that makes a registration list dangerous: it does not
break, it silently shrinks.

Two runner styles are legitimate here:

* discovery from ``globals()`` — structurally cannot drop a test;
* an explicit list — fine, provided the list actually names everything.

So this checks the second kind, and lets the first kind alone. It is a static
read of each file rather than an import, because importing a suite would run
its module-level fixtures (several redirect ``$HOME`` and mutate caches).

Run: python3 registration_test.py  (stdlib only).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SELF = Path(__file__).name

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def main() -> int:
    files = sorted(p for p in TESTS.glob("*_test.py") if p.name != SELF)
    check("found the suites", len(files) > 5, True)

    for path in files:
        source = path.read_text(encoding="utf-8")
        defined = set(re.findall(r"^def (test_\w+)", source, re.M))
        if not defined:
            continue

        # Everything from `if __name__` onward is the runner.
        runner = source.split("if __name__")[-1]
        if "globals()" in runner:
            print(f"  ok  {path.name} (discovers from globals)")
            continue

        named = set(re.findall(r"(test_\w+)", runner))
        orphans = sorted(defined - named)
        check(f"{path.name} runs every test it defines", orphans, [])

    _check_every_failure_leaves_a_trace()
    return 1 if failures else 0


def _check_every_failure_leaves_a_trace() -> None:
    """Every way the backstop can fail must record a breadcrumb.

    `flush_session._send` returns False on each failure, and this hook is async
    fire-and-forget — its stdout goes nowhere — so a `return False` with no
    breadcrumb is an invisible failure in the path whose whole job is catching
    what per-turn capture missed.

    That gap appeared twice: first the transport exceptions were silent, then
    once those were fixed the isError and unrecognized-reply paths still were.
    A guarantee with holes is not one, so it is checked structurally rather
    than remembered.
    """
    print("\nbackstop failure paths")
    source = (Path(__file__).resolve().parents[1] / "plugins" / "memhub"
              / "scripts" / "flush_session.py").read_text(encoding="utf-8")
    body = source.split("async def _send(")[-1].split("\ndef ")[0]

    # Walk the function's lines; every `return False` must have a _breadcrumb
    # call somewhere in the handful of lines before it.
    lines = body.splitlines()
    unguarded = []
    for i, line in enumerate(lines):
        if line.strip() != "return False":
            continue
        window = "\n".join(lines[max(0, i - 8):i])
        if "_breadcrumb(" not in window:
            unguarded.append(i)
    check("every `return False` in _send breadcrumbs first", unguarded, [])
    # And that the scan found the returns at all — a test that silently
    # inspects nothing is worse than no test.
    check("found the failure exits",
          len([l for l in lines if l.strip() == "return False"]) >= 3, True)


if __name__ == "__main__":
    print("test registration")
    code = main()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        print("\nA test that is defined but never registered is invisible: the "
              "suite passes over less code than it appears to.")
    else:
        print("\nevery defined test is reachable")
    sys.exit(code)
