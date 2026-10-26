#!/usr/bin/env python3
"""PostToolUse hook: after a call that addressed GitHub and named exactly one
pull request, ask the backend one question and inject one instruction — link
this session, let the model judge, or mention that GitHub isn't connected.

Emits nothing at all unless every gate passes: the call touched GitHub, its
output carried exactly one PR URL, the server answered, and the org has the
feature on. Silence is the normal outcome and always the safe one; the user
can still run `/memhub:link-pr`.

Stateless by design. A PR↔session relationship is many-to-many, so a dedup
file keyed on the pull request is exactly what would stop a genuinely new
session from linking itself later. What keeps this quiet inside a
`/memhub:pr-babysit` loop is the last line of the injected text, which the
agent reads with its own conversation context in view.

    python3 pr_link_trigger.py [--host claude|codex|cursor]   # default: claude

The host is passed on argv rather than sniffed from the payload, because it
decides how the session id is namespaced (`pr_link.conversation_id_for`) and a
mis-namespaced id links nothing with no error anywhere.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_link  # noqa: E402


def _host(argv: list[str]) -> str:
    for index, arg in enumerate(argv):
        if arg == "--host" and index + 1 < len(argv):
            value = argv[index + 1].strip().lower()
            # An unknown host is treated as claude: a wrong prefix is worse
            # than no prefix, because the bare id at least matches a Claude
            # session and a bogus one matches nothing.
            return value if value in pr_link.HOSTS else "claude"
        if arg.startswith("--host="):
            value = arg.split("=", 1)[1].strip().lower()
            return value if value in pr_link.HOSTS else "claude"
    return "claude"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    context = pr_link.context_for_call(
        payload.get("tool_name"),
        payload.get("tool_input"),
        payload.get("tool_response"),
        session_id,
        host=_host(argv),
    )
    if not context:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:  # noqa: BLE001 — a hook never fails the call it follows
        rc = 0
    sys.exit(rc or 0)
