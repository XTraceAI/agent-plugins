#!/usr/bin/env python3
"""`rulebook_verify.py` — the pre-filing check for a candidate rule.

What is pinned here:

* the three failure classes it exists to catch, each taken from a rule that
  was actually live and wrong: a matcher with no exemption that fires when a
  command merely quotes its trigger; a matcher that still fires on the
  complied-with form, so fixing the problem does not silence the rule; and a
  pattern the hook drops at load time, which makes an active rule that never
  fires on anybody's machine;
* that it verifies through the SHIPPED engine — `to_hook_rule` + `evaluate` —
  so a change to matching behaviour cannot pass here and fail live;
* that the generated self-mention cases are real commands, never a mangled
  literal that passes vacuously;
* that a rule with no `--fires` case is refused: nothing has shown it can fire.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "plugins", "memhub", "scripts")
VERIFY = os.path.join(SCRIPTS, "rulebook_verify.py")

sys.path.insert(0, SCRIPTS)
import rulebook_verify as V  # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("PASS  " if cond else "FAIL  ") + label + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def run(rule: dict, *args: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, VERIFY, "--rule", json.dumps(rule), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def main() -> int:
    bash = lambda **m: {"title": "t", "statement": "s", "delivery": "agent_hook",
                        "matcher": {"event": "bash", **m}}

    # --- the self-mention class -------------------------------------------
    rc, out = run(bash(command_rx="gh pr merge"), "--fires", "gh pr merge 7")
    check("a bash rule with no exemption fails on its own generated grep case",
          rc == 1 and "SILENT FAIL" in out, out)
    rc, out = run(bash(command_rx=r"gh\s+pr\s+merge\b",
                       command_not_rx=r"\bgrep\b|\brg\b|python3?\s+-c\b"),
                  "--fires", "gh pr merge 7")
    check("the same rule with command_not_rx passes", rc == 0 and "SILENT FAIL" not in out, out)

    # The generated case must be a command someone could type. A mangled
    # literal would pass every rule and prove nothing.
    check("generated self-mention cases use a readable literal",
          'grep -rn "gh pr merge" .' in out, out)
    check("_literal_of resolves escapes and whitespace classes",
          V._literal_of(r"gh\s+pr\s+merge\b") == "gh pr merge"
          and V._literal_of(r"git\s+push\b") == "git push")
    check("_literal_of declines to guess through real regex structure",
          V._literal_of(r"grep\s+-[a-zA-Z]*r") == "" and V._literal_of(r"(a|b)+") == "")
    rc, out = run(bash(command_rx=r"rm\s+-[a-z]*f"), "--fires", "rm -rf build")
    check("a pattern we cannot render as a literal generates no self-mention case",
          rc == 0 and "generated" not in out, out)

    # --- the complied-with class ------------------------------------------
    edit = {"title": "t", "statement": "s", "delivery": "agent_hook",
            "matcher": {"event": "edit", "path_rx": r"/src/.*\.py$", "content_rx": r"verify=False"}}
    rc, out = run(edit, "--fires", "/r/src/db.py::connect(url, verify=False)",
                  "--silent", "/r/src/db.py::connect(url)")
    check("an edit rule that stops on the fixed form passes", rc == 0, out)
    loose = {"title": "t", "statement": "s", "delivery": "agent_hook",
             "matcher": {"event": "edit", "path_rx": r"/src/.*\.py$", "content_rx": r"connect\("}}
    rc, out = run(loose, "--fires", "/r/src/db.py::connect(url, verify=False)",
                  "--silent", "/r/src/db.py::connect(url)")
    check("an edit rule that still fires after the fix is refused",
          rc == 1 and "SILENT FAIL" in out, out)

    # --- the silent-drop class --------------------------------------------
    rc, out = run(bash(command_rx="a" * 450), "--fires", "aaa")
    check("a pattern over the hook's length bound is reported as a LOAD failure",
          rc == 1 and "LOAD   FAIL" in out and "longer than" in out, out)
    rc, out = run(bash(command_rx="(a+)+$"), "--fires", "aaa")
    check("a catastrophically backtracking pattern is a LOAD failure",
          rc == 1 and "LOAD   FAIL" in out, out)
    rc, out = run(bash(command_rx="[unclosed"), "--fires", "x")
    check("a pattern that does not compile is a LOAD failure", rc == 1 and "LOAD   FAIL" in out, out)

    # --- it verifies through the shipped engine ---------------------------
    import rulebook_hook as H
    check("verification goes through the hook's own to_hook_rule + evaluate",
          V.H is H and hasattr(H, "evaluate") and hasattr(H, "to_hook_rule"))
    rc, out = run(bash(command_rx=r"python3?\s+-\s*<<", match_heredoc_body=True, body_rx="DROP TABLE"),
                  "--fires", "python3 - <<'PY'\nDROP TABLE users\nPY",
                  "--silent", "python3 - <<'PY'\nprint(1)\nPY", "--no-self-mention")
    check("heredoc body rules are evaluated the way the live hook evaluates them", rc == 0, out)

    # --- usability guards --------------------------------------------------
    rc, out = run(bash(command_rx="terraform apply"))
    check("a rule with no --fires case is refused", rc == 1 and "nothing proved" in out, out)
    session = {"title": "t", "statement": "s", "delivery": "session_context"}
    rc, out = run(session)
    check("a session_context rule needs no fire-test", rc == 0 and "nothing to fire-test" in out, out)
    p = subprocess.run([sys.executable, VERIFY, "--rule", "{not json"], capture_output=True, text=True)
    check("malformed JSON exits 2 with a plain message, never a traceback",
          p.returncode == 2 and "Traceback" not in p.stderr, p.stderr[:120])
    p = subprocess.run([sys.executable, VERIFY, "--help"], capture_output=True, text=True)
    check("--help works", p.returncode == 0 and "--fires" in p.stdout)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook verify checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
