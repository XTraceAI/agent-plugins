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

import pr_babysit_trigger  # noqa: E402
import pr_link_trigger  # noqa: E402

# The matcher the babysit hook carried before the merge, replicated exactly.
# Claude Code matchers are UNANCHORED regexes, so `"Bash"` also selected
# `BashOutput`; keeping the same shape here keeps the babysit lane's trigger
# surface byte-for-byte what it was, rather than quietly widening it to the
# GitHub MCP tools this merged registration now also matches.
_BASH = re.compile("Bash")


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

    parts = [_lane(lambda: pr_link_trigger.context_for(payload, host=host))]
    if _BASH.search(tool_name):
        parts.append(_lane(lambda: pr_babysit_trigger.context_for(payload)))
    return [part for part in parts if part]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    parts = contexts_for(payload, host=pr_link_trigger.host_from_argv(argv))
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
