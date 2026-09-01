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

import json
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


# Ceiling on the serialized size of one tool result before it is elided.
#
# Sized against what the MODEL was allowed to see. Claude Code spills any tool
# result over ~25k tokens (~100 KB) to a file and shows the model a one-line
# pointer, so nothing above that was ever part of the conversation — it is
# bytes the transcript carries that no one read. 200 KB is that ceiling with
# headroom for older clients that inlined more, and it sits far below the
# 3.5 MB upload slice and the server's 4 MiB request limit.
#
# Why this exists: an MCP ``search_memory`` reply of 5.26 MB (two HTML
# artifacts with screenshots inlined) was rejected by the client as too large
# — the model saw a 1.4 KB error — but the client stored the WHOLE reply in the
# record's ``mcpMeta``. One 5.27 MB transcript line can never fit a request the
# server accepts, and because the cursor rightly never advances past a failed
# send, every later turn of that session re-sent it, got a 413, and captured
# nothing. The session was silently uncapturable from that turn on.
MAX_TOOL_RESULT_BYTES = 200_000

# Record fields that are a second copy of a tool result rather than
# conversation: ``mcpMeta`` is the raw MCP response Claude Code keeps beside
# the ``tool_result`` block (and is where the 5 MB above lived), and
# ``toolUseResult`` is the client's structured mirror of the same content.
# Neither is read by the server. Both are dropped from an OVERSIZED record
# only — a record under the ceiling is shipped byte-for-byte as before.
_TOOL_RESULT_MIRRORS = ("mcpMeta", "toolUseResult")


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _elision_note(nbytes: int, tool_use_id) -> str:
    return (f"[memhub: tool result elided — {nbytes:,} bytes exceeded the "
            f"{MAX_TOOL_RESULT_BYTES:,}-byte capture limit; "
            f"tool_use_id={tool_use_id or '?'}]")


def elide_oversized_tool_results(
    records: list, max_bytes: int = MAX_TOOL_RESULT_BYTES,
) -> list:
    """``records`` with any tool result larger than ``max_bytes`` replaced by
    a note saying so.

    Only a record that is itself over the ceiling is touched, and within it
    only the tool-result payloads: the ``tool_result`` blocks whose content is
    over the ceiling become a one-line note that names the size and the
    ``tool_use_id`` (so the call stays linkable to its ``tool_use``), and the
    client's mirror copies of the result are dropped. Text blocks, the user's
    own prose, and every other field are left exactly as they were. The input
    list and its records are never mutated.

    A note rather than a silent drop, on purpose: a transcript where a tool
    call has no result reads as a call that never returned, and an extractor
    will happily build a fact on that. The note says what happened and how
    big it was, which is all anyone downstream can use.

    Applied on every upload path — per-turn, session-end, on-demand import,
    Codex, Cursor — for the same reason the slash-command filter is: a session
    must not be capturable or stuck depending on which path reached it.
    Never raises; on an unexpected failure the records are returned untouched,
    because capture stopping is worse than this pass not running.
    """
    try:
        return [_elide_record(r, max_bytes) for r in records]
    except Exception:  # noqa: BLE001 — never break capture over a size cap
        return records


def _elide_record(record, max_bytes: int):
    if not isinstance(record, dict) or _size(record) <= max_bytes:
        return record
    message = record.get("message")
    if not isinstance(message, dict):
        return record  # oversized, but not a message — nothing to elide
    content = message.get("content")
    if not isinstance(content, list):
        return record  # a bare-string message carries no tool result
    new_blocks = []
    changed = False
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "tool_result"
                and _size(block.get("content")) > max_bytes):
            nbytes = _size(block.get("content"))
            block = dict(block)
            block["content"] = _elision_note(nbytes, block.get("tool_use_id"))
            changed = True
        new_blocks.append(block)
    mirrors = [k for k in _TOOL_RESULT_MIRRORS if k in record]
    if not changed and not mirrors:
        return record
    out = {k: v for k, v in record.items() if k not in _TOOL_RESULT_MIRRORS}
    out["message"] = {**message, "content": new_blocks}
    return out
