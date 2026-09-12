"""Self-test for cloud-session detection and the advice it changes.

Getting this wrong is not loud: a cloud session told to run /memhub:login
tries to open a browser that does not exist, and a laptop told to edit an
environment's variables has no such environment. Both are advice that cannot
work, which spends the user's trust proving it.

Run: python3 cloud_session_test.py  (stdlib only).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cloud_session as cs  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def test_detection_reads_only_the_documented_variable() -> None:
    print("detection")
    check("unset -> local", cs.is_cloud_session({}), False)
    check("true -> cloud", cs.is_cloud_session({"CLAUDE_CODE_REMOTE": "true"}), True)
    check("case and whitespace tolerated",
          cs.is_cloud_session({"CLAUDE_CODE_REMOTE": " True "}), True)
    # Anything else is not the documented value. "1" is the tempting one — a
    # heuristic that accepted it would also accept a stray shell export.
    for value in ("false", "", "1", "yes", "remote"):
        check(f"{value!r} -> local", cs.is_cloud_session({"CLAUDE_CODE_REMOTE": value}), False)
    # Other remote-looking variables do not count on their own: a local
    # container or a developer's own tooling may set any of them.
    check("other CLAUDE_CODE_REMOTE_* variables do not count",
          cs.is_cloud_session({"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_x",
                               "CLAUDE_CODE_ENTRYPOINT": "remote_desktop"}), False)


def test_the_fixes_name_what_the_user_must_touch() -> None:
    print("fix wording")
    host = "api.memhub.xtrace.ai"
    fix = cs.token_fix(host)
    check("token fix names the variable", "MEMHUB_TOKEN" in fix, True)
    check("token fix names the key shape", "mhk_" in fix, True)
    check("token fix names where to mint", "--cloud-key" in fix, True)
    check("token fix names the host", host in fix, True)
    check("token fix does not send them to a browser login",
          "Run /memhub:login to authenticate" in fix, False)
    egress = cs.egress_fix(host)
    check("egress fix names the host", host in egress, True)
    check("egress fix names the setting", "network access" in egress, True)


if __name__ == "__main__":
    for test in (test_detection_reads_only_the_documented_variable,
                 test_the_fixes_name_what_the_user_must_touch):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall cloud_session checks passed")
