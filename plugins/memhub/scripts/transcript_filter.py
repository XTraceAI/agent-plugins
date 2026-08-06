#!/usr/bin/env python3
"""Records that must never leave the machine as conversation content.

Claude Code writes slash commands and their output into the transcript as
ordinary USER records — a real ``type: "user"`` carrying a real ``message`` —
so no type-based filter excludes them. Shipped as-is, ``/model`` and its "Set
model to Sonnet 5" reply are extracted as things the USER SAID.

Measured over 205 local sessions: **687 of 5,839 text-carrying user records,
11.8%**. (Against ALL user records it is only 1.8%, but that denominator is
dominated by tool results, which are also typed ``user`` and carry no prose —
the 11.8% is the share of what actually reads as conversation.)

Shared by both upload paths deliberately. ``flush_turn`` ships deltas per turn
and ``import_session`` ships whole transcripts; a filter on only one of them
would mean the same session is clean or dirty depending on which path captured
it, which is worse than either answer applied consistently.

Stdlib only, and no side effects on import — ``flush_turn`` runs inside a Stop
hook where an import-time failure would surface as a traceback in the user's
session.
"""
from __future__ import annotations

import re

# The wrappers the client emits around a slash command: the invocation, the
# echo of its name and args, and the captured output of a local command.
_COMMAND_WRAPPER_TAGS = (
    "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-stderr", "local-command-caveat",
)

# Anchored at the START of the message, not searched. These are emitted by the
# client rather than typed, so the shape is fixed — while a loose search would
# strip a message that merely QUOTES one of these tags, which is exactly what a
# conversation ABOUT this filter looks like.
_OPENS_WITH_WRAPPER = re.compile(
    r"\s*<(?:" + "|".join(_COMMAND_WRAPPER_TAGS) + r")>",
)
_WRAPPER_ELEMENT = re.compile(
    r"<(" + "|".join(_COMMAND_WRAPPER_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)


def record_text(record: dict) -> str:
    """The record's plain text, whichever shape its content takes.

    Claude Code writes ``content`` as a bare string for a typed message and as
    a block list once anything structured is attached, so both are read.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def is_command_wrapper(record: object) -> bool:
    """True for a record that is ONLY slash-command bookkeeping.

    All-or-nothing, deliberately. Every one of the 687 measured cases was a
    wrapper alone in its own record — the client writes the invocation, the
    echo, and the output as separate records — so dropping the whole record
    loses nothing real.

    If a wrapper ever DOES arrive beside real prose, the record is KEPT and
    both are shipped, because the two failure modes are not symmetric: an
    unfiltered wrapper is noise in a search result, while a dropped user
    message is content that no later flush will re-send. This is the same
    reasoning that keeps ``attachment`` out of the inert-record set.
    """
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    text = record_text(record)
    if not _OPENS_WITH_WRAPPER.match(text):
        return False
    return not _WRAPPER_ELEMENT.sub("", text).strip()


def drop_command_wrappers(records: list) -> list:
    """``records`` minus the slash-command bookkeeping."""
    return [r for r in records if not is_command_wrapper(r)]
