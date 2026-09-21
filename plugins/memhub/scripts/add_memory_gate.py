#!/usr/bin/env python3
"""Deny ``add_memory`` in a session this plugin is already capturing (stdlib only).

**The failure.** ``add_memory`` exists for MCP clients with no capture hooks: the
caller passes one turn — what the user said, what the assistant replied — and
the server stores it as a conversation. In a Claude Code session running this
plugin that is never needed, because the ``Stop`` hook already uploads every
turn verbatim. Agents call it anyway, to make a finding "findable later" in a
brain, and they fill ``user_message`` with a paraphrase they wrote themselves.
The server stores that as a second conversation, whose user turn the user never
typed, and Studio lists it beside the real session. Blind live sessions on an
unmodified build, asked only to put a finished answer into a new brain, did
this more often than not (the trials are in the PR that added this file).
``save_artifact`` is the tool for what they were after, and the agents that
did not call ``add_memory`` used it.

**Why here and not on the server.** The server cannot know whether a client
captures its own sessions; only this plugin knows its ``Stop`` hook is live. So
the rule is scoped to exactly that condition and nothing wider:

* the tool is MemHub's ``add_memory`` — matched on the name's suffix AND on its
  ``user_message`` argument, so it covers this plugin's own server and a
  claude.ai MemHub connector alike (the blind agents used the connector) without
  catching an unrelated server's ``add_memory``;
* per-turn capture is not switched off (``MEMHUB_TURN_FLUSH=0``), and this is
  not a harness authoring child (``MEMHUB_HARNESS_CHILD``), which the flush
  scripts never capture;
* the payload names a transcript, which is what the flush reads;
* the plugin holds a credential capture can authenticate with, judged by the
  same network-free check the capture-health banner uses — so this and that
  banner can never disagree about whether capture is running.

When any of those is false, capture is not happening and ``add_memory`` is the
only way to save the turn, so the call is allowed.

**Fails OPEN.** Any unexpected error allows the call: a broken guard must not
take a tool away from a session.

Run the self-test:  python3 tests/add_memory_gate_test.py  (from the repo root)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

# `mcp__<server>__add_memory`, whatever the server segment is called.
_ADD_MEMORY = re.compile(r"^mcp__.+__add_memory$")
_CAPTURE_OFF = {"0", "off", "false"}
# `_token_problem` verdicts under which the flush still authenticates today:
# none at all, a key close to expiry, and an OAuth token that works but cannot
# renew. The other verdicts ("never", "key_expired", "unrenewable") mean the
# flush has no working credential, so nothing is being captured.
_CREDENTIAL_WORKS = {None, "key_expiring", "no_refresh"}

DENY_REASON = (
    "MemHub is already capturing this Claude Code session: the plugin uploads "
    "every turn verbatim. add_memory here would store a second conversation "
    "whose user message the user never typed, and teammates would see it as a "
    "separate session. To keep findings where a brain's readers will find "
    "them, save them with save_artifact (pass agent_brain_id). Do not retry "
    "add_memory in this session."
)
USER_LINE = ("MemHub: blocked add_memory. This session is already captured, "
             "so findings belong in save_artifact.")


def is_memhub_add_memory(payload: object) -> bool:
    """True for MemHub's add_memory tool, from any server that exposes it."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("tool_name")
    if not isinstance(name, str) or not _ADD_MEMORY.match(name):
        return False
    args = payload.get("tool_input")
    return isinstance(args, dict) and "user_message" in args


def capture_is_active(payload: dict,
                      environ: Mapping[str, str] | None = None) -> bool:
    """True when this plugin's per-turn capture is running for this session."""
    env = os.environ if environ is None else environ
    if env.get("MEMHUB_TURN_FLUSH", "").strip().lower() in _CAPTURE_OFF:
        return False
    import transcript_filter  # noqa: PLC0415 — stdlib-only, beside this file
    if transcript_filter.is_harness_child(env):
        return False  # both flush scripts skip the harness's forked copy
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript.strip():
        return False
    import capture_health  # noqa: PLC0415 — stdlib-only, beside this file
    host = capture_health._env_host()
    if not host:
        return False
    return capture_health._token_problem(host) in _CREDENTIAL_WORKS


def decide(payload: object,
           environ: Mapping[str, str] | None = None) -> dict | None:
    """The hook's stdout document, or None to allow the call silently."""
    if not is_memhub_add_memory(payload) or not capture_is_active(payload, environ):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        },
        "systemMessage": USER_LINE,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        out = decide(json.loads(raw) if raw.strip() else {})
    except Exception:  # noqa: BLE001 — fail open, see module docstring
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
