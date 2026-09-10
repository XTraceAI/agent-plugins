#!/usr/bin/env python3
"""PostToolUse hook: ONE additional context for a call that addressed GitHub.

Two PostToolUse groups that each return `hookSpecificOutput.additionalContext`
do not both reach the model — the earlier registration's context survives and
the later one is dropped, silently, on both sides. `gh pr create` was the one
command where both PR-lane hooks fired, so the instruction that lost was
exactly the link instruction for the one call that links unconditionally,
while the babysit instruction that won sent the model into a loop where it
never revisited the question.

So the PR lane has ONE registration, and this is it. Each lane stays a pure
function in its own module — `pr_link_trigger.context_for` and
`pr_babysit_trigger.context_for` — which is what a future Claude-side
dispatcher (the Edit family has the same collision) would call too.

The Codex half of the plugin already worked this way: `codex_hook_bridge.py`
folds its jobs into one document with the same `"\n\n"` join, so the two hosts
compose context identically.

Order is load-bearing: the LINK instruction comes first, so the model reads it
before the babysit instruction hands it a loop.

    python3 pr_post_context.py [--host claude|codex|cursor]   # default: claude
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported defensively, because these are MODULE-SCOPE imports and therefore
# outside _lane's guard. `pr_link_trigger` pulls in pr_link -> pr_provenance and
# touches Path.home() while importing, so a half-written file during a plugin
# upgrade, or an unset HOME, used to make this hook exit 1 with a traceback and
# lose BOTH instructions — where the old two-registration layout would still
# have emitted the babysit one from its own process. A hook never fails the call
# it follows, so a lane that cannot even be imported is simply a lane that has
# nothing to say.
try:
    import pr_link_trigger  # noqa: E402
except BaseException as _exc:  # noqa: BLE001
    pr_link_trigger = None
    print(f"[memhub-pr-context] link lane unavailable: {_exc!r}", file=sys.stderr)
try:
    import pr_babysit_trigger  # noqa: E402
except BaseException as _exc:  # noqa: BLE001
    pr_babysit_trigger = None
    print(f"[memhub-pr-context] babysit lane unavailable: {_exc!r}", file=sys.stderr)

# The matcher the babysit hook carried before the merge, replicated exactly.
# Claude Code matchers are UNANCHORED regexes, so `"Bash"` also selected
# `BashOutput`; keeping the same shape here keeps the babysit lane's trigger
# surface byte-for-byte what it was, rather than quietly widening it to the
# GitHub MCP tools this merged registration now also matches.
_BASH = re.compile("Bash")

# host, owner, repo, number — the identity of a pull request, compared instead
# of the URL text. The backend answers with the owner LOWERCASED
# (`XTraceAI` -> `xtraceai`), so a case-sensitive comparison read gh's own URL
# as a different pull request and suppressed the babysit lane on every repo
# whose owner has a capital in it. GitHub owners and repo names are
# case-insensitive; the number is the identity.
_PR_IDENTITY = re.compile(r"(?i)https?://([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/(\d+)")


def _pr_identities(text: str) -> set[tuple[str, str, str, str]]:
    return {(host.lower(), owner.lower(), repo.lower(), number)
            for host, owner, repo, number in _PR_IDENTITY.findall(text)}


def _lane(job: Callable[[], str | None]) -> str | None:
    """Run one lane. A lane that raises loses its own voice, not the other's."""
    try:
        return job()
    except BaseException as exc:  # noqa: BLE001 — a hook never fails its call
        print(f"[memhub-pr-context] {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def contexts_for(payload: object, *, host: str = "claude") -> list[str]:
    """Every instruction this call earned, in the order the model should read."""
    if not isinstance(payload, dict):
        return []
    tool_name = payload.get("tool_name")
    tool_name = tool_name if isinstance(tool_name, str) else ""

    link = babysit = None
    if pr_link_trigger is not None:
        link = _lane(lambda: pr_link_trigger.context_for(payload, host=host))
    if pr_babysit_trigger is not None and _BASH.search(tool_name):
        babysit = _lane(lambda: pr_babysit_trigger.context_for(payload))

    # The lanes find the pull request by DIFFERENT rules: the link lane reads
    # gh's report structurally (a URL alone on its line, or a `key:\tvalue`
    # field) precisely so a pull-request BODY citing some other PR cannot
    # choose the target, while the babysit lane takes the first URL anywhere in
    # stdout. Separately that was two hooks disagreeing; merged it would be ONE
    # instruction saying "link PR A" and "babysit PR B" — aiming a loop that
    # fixes findings and pushes at a pull request the session never opened. If
    # they disagree, the hardened extractor wins and the babysit half is
    # dropped, because arming the wrong loop is worse than arming none.
    if link and babysit:
        armed = _pr_identities(babysit)
        if armed and not (armed & _pr_identities(link)):
            print("[memhub-pr-context] lanes named different pull requests; "
                  "babysit suppressed", file=sys.stderr)
            babysit = None

    return [part for part in (link, babysit) if part]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    host = "claude"
    if pr_link_trigger is not None:
        host = _lane(lambda: pr_link_trigger.host_from_argv(argv)) or "claude"
    parts = contexts_for(payload, host=host)
    if not parts:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(parts),
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:  # noqa: BLE001 — a hook never fails the call it follows
        rc = 0
    sys.exit(rc or 0)
