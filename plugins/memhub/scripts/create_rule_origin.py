#!/usr/bin/env python3
"""Stamp MemHub ``create_rule`` with the session it is filed from (stdlib only).

**The failure.** A rule is almost always filed from inside a session — the
person's ``/memhub:create-rule``, ``/memhub:start-rulebook``, a nomination —
but MemHub learned which session only for a ``session_draft``, from the
harness's ``state`` stamp. Every other filing arrived with no session at all:
an MCP call carries no session header, and the agent does not know its own
Claude Code session id. So a reviewer could not open the conversation a rule
came out of (ENG-1193, docs/specs/rule-origin-session.md).

**Why here and not in the skills.** The hook's stdin names the session, and
the transcript it points at numbers the turn; the model knows neither. So this
``PreToolUse`` hook copies ``session_id`` (and the turn in progress) into the
call's arguments as ``origin_session_id`` / ``origin_turn`` through
``updatedInput``. The model never types them, so they cannot be wrong or
forgotten, and anything it did type for them is overwritten: stdin is the
ground truth. No skill text changes.

What it stamps, and what it leaves alone:

* the tool is MemHub's ``create_rule`` — matched on the name's suffix AND on
  its required ``title`` and ``statement`` arguments, so an unrelated server's
  ``create_rule`` is never rewritten;
* a ``session_draft`` is left untouched: the server derives its origin from
  ``state.session_id`` / ``state.turn`` and refuses a supplied value that
  disagrees, so stamping it could only cause a refusal;
* the session id must be one the server accepts (``_SESSION_ID``, the same
  pattern as ``harness_stop.py``'s moment ref), or nothing is stamped;
* ``origin_turn`` is the last turn ``harness_extract.turns_from_transcript``
  reads from the transcript, numbered exactly as ``stamp_state`` numbers
  ``state.turn`` — one numbering for every rule. In that numbering a slash
  command (``/memhub:create-rule …``, recorded as ``<command-message>``) opens
  no turn, so a rule filed from one carries the last prompt the person TYPED
  before it, usually the one the rule is about. A session whose only prompt is
  the slash command has no such turn. ``origin_turn`` is omitted there, and
  whenever the transcript can't be read, and a model-typed one is removed.

**Subagents.** A call made inside a subagent carries a top-level ``agent_id``.
Its ``session_id`` is still stamped and its turn is not: the transcript and
the turn count there need not be the mother session's. No raw subagent hook
payload is checked into this repository, so which session id a subagent's
PreToolUse stdin carries is settled from recorded live observations, not a
fixture: ``rulebook_hook.agent_id_of`` ("verified live 2026-09-07": ``agent_id``
and ``agent_type`` at the top level, ``transcript_path`` the PARENT session's
file), the payload ``tests/rulebook_hook_test.py`` builds from that probe, and
``skills/create-rule/references/live-test.md`` (a sub-agent's ledger row
carries this, the parent's, ``session_id`` and the sub-agent's own
``agent_id``). Each says the parent's id. Either way the behaviour is the
same — stamp the session, omit the turn — and the harness fork files
``session_draft``, which is skipped above.

**Never approves.** The output has no ``permissionDecision``, so the call
goes through the normal permission flow with the stamped input.

**Fails OPEN.** Any unexpected payload or error prints nothing and exits 0,
so ``create_rule`` runs unchanged: a broken stamp must never take the tool
away.

Run the self-test:  python3 tests/create_rule_origin_test.py  (from the repo root)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# `mcp__<server>__create_rule`, whatever the server segment is called.
_CREATE_RULE = re.compile(r"^mcp__.+__create_rule$")
# What MemHub (and harness_stop.py's moment ref) accepts as a session id.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def is_memhub_create_rule(payload: object) -> bool:
    """True for MemHub's create_rule tool, from any server that exposes it."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("tool_name")
    if not isinstance(name, str) or not _CREATE_RULE.match(name):
        return False
    args = payload.get("tool_input")
    return isinstance(args, dict) and "title" in args and "statement" in args


def origin_turn(payload: dict) -> int | None:
    """The last person-typed turn, as ``stamp_state`` numbers it; None inside
    a subagent, without a readable transcript, or on any error."""
    if str(payload.get("agent_id") or "").strip():
        return None
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not os.path.isfile(transcript):
        return None
    try:
        import harness_extract  # noqa: PLC0415 — stdlib-only, beside this file
        turns = harness_extract.turns_from_transcript(transcript)
    except Exception:  # noqa: BLE001 — an unread turn still stamps the session
        return None
    n = turns[-1].get("n") if turns else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def decide(payload: object) -> dict | None:
    """The hook's stdout document, or None to let the call go ahead unchanged."""
    if not is_memhub_create_rule(payload):
        return None
    tool_input = payload["tool_input"]
    if tool_input.get("source") == "session_draft":
        return None
    session = payload.get("session_id")
    session = session.strip() if isinstance(session, str) else ""
    if not _SESSION_ID.match(session):
        return None
    # `updatedInput` replaces the whole input, so every original field is kept.
    updated = dict(tool_input)
    updated["origin_session_id"] = session
    turn = origin_turn(payload)
    if turn is None:
        updated.pop("origin_turn", None)
    else:
        updated["origin_turn"] = turn
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": updated}}


def main() -> int:
    try:
        # Bytes, decoded as UTF-8 whatever the locale: the input is echoed back
        # as `updatedInput`, so a cp1252 read would store mojibake in the rule.
        stream = getattr(sys.stdin, "buffer", None)
        raw = (stream.read().decode("utf-8") if stream is not None
               else sys.stdin.read())
        out = decide(json.loads(raw) if raw.strip() else {})
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 — fail open, see module docstring
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
