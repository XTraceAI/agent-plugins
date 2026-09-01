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

# The ceiling above which a record is UNSENDABLE, not merely big: one record
# over the upload slice (3.5 MB, itself under the server's 4 MiB request
# limit) rides alone in a request the server always rejects, and because the
# cursor never advances past a failed send, it pins capture for the rest of
# the session. Below this line the module refuses to touch prose — a big
# record that CAN ship, ships whole. Above it that refusal protects nothing:
# the record cannot be sent, so leaving it intact loses it AND every record
# after it. Kept just under the slice so a trimmed record still fits one.
HARD_MAX_RECORD_BYTES = 3_400_000

# What survives of an oversized text when the record is unsendable: enough to
# read what the turn was about, not the pasted payload that made it huge.
_HARD_TRIM_KEEP_CHARS = 64_000


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _elision_note(nbytes: int, tool_use_id=None) -> str:
    """One line saying what was cut and how big it was — truthful for any
    block type, so a trimmed text block is not labeled a tool result."""
    ref = f"; tool_use_id={tool_use_id}" if tool_use_id else ""
    return (f"[memhub: elided — {nbytes:,} bytes exceeded the "
            f"{MAX_TOOL_RESULT_BYTES:,}-byte capture limit{ref}]")


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
    # Mirrors go first, whatever shape the message content takes: they are
    # where the 5 MB lived, and a bare-string message with a giant mirror
    # beside it must not slip past the block handling below untouched.
    out = {k: v for k, v in record.items() if k not in _TOOL_RESULT_MIRRORS}
    content = message.get("content")
    if not isinstance(content, list):
        if isinstance(content, str) and _size(out) > HARD_MAX_RECORD_BYTES:
            # Unsendable prose: keep the head, say what happened. Below the
            # hard ceiling a bare-string message is never touched.
            out["message"] = {**message, "content":
                              content[:_HARD_TRIM_KEEP_CHARS] + "\n"
                              + _elision_note(_size(content), None)}
            return out
        # A bare-string message carries no tool result; only the mirrors
        # (if any) came off.
        return out if len(out) != len(record) else record
    blocks = list(content)
    out["message"] = {**message, "content": blocks}
    # Largest tool result first, until the record fits — not just the blocks
    # that are over the ceiling on their own. A parallel-tool turn lands as ONE
    # user record carrying several results, and five 150 KB results are as
    # unsendable as one 750 KB one. Only tool results are ever touched: if the
    # record is still over after every one of them is a note, the bulk is the
    # user's own prose or the client's metadata, and it is shipped as-is —
    # dropping a user message is the asymmetric failure this module refuses.
    results = sorted(
        (i for i, b in enumerate(blocks)
         if isinstance(b, dict) and b.get("type") == "tool_result"),
        key=lambda i: _size(blocks[i].get("content")), reverse=True,
    )
    # Running total, not re-serialized per block: swapping one block for its
    # note changes the record's size by exactly the difference between the two
    # (the delimiters around it are unchanged), so the arithmetic is exact and
    # the 5 MB record is serialized once instead of once per result.
    total = _size(out)
    for i in results:
        if total <= max_bytes:
            break
        block = blocks[i]
        note = {**block, "content": _elision_note(
            _size(block.get("content")), block.get("tool_use_id"))}
        saved = _size(block) - _size(note)
        if saved <= 0:
            # The note would be no smaller than the result it replaces, and
            # every remaining result is smaller still: the bulk is elsewhere
            # (prose), and swapping notes in would only grow the record.
            break
        total -= saved
        blocks[i] = note
    if total > HARD_MAX_RECORD_BYTES:
        # Still unsendable: the bulk is outside the tool results — pasted
        # prose, a giant tool_use input. From here every byte kept costs the
        # whole rest of the session, so trim ANY block, largest first, until
        # one upload slice can carry the record.
        for i in sorted(range(len(blocks)), key=lambda i: _size(blocks[i]),
                        reverse=True):
            if total <= HARD_MAX_RECORD_BYTES:
                break
            trimmed = _hard_trim_block(blocks[i])
            total += _size(trimmed) - _size(blocks[i])
            blocks[i] = trimmed
    if blocks == content and len(out) == len(record):
        return record  # nothing elided and no mirror to drop: untouched
    return out


def _hard_trim_block(block):
    """``block`` reduced enough to ship, whatever type it is.

    Only reached for a record over ``HARD_MAX_RECORD_BYTES`` — the tier where
    the alternative to trimming is losing the record and the session tail
    behind it. Text keeps its head; a tool result becomes the standard note;
    a tool call keeps its name with its input summarized; anything else is
    replaced by a note naming its size.
    """
    if isinstance(block, dict):
        if isinstance(block.get("text"), str):
            if len(block["text"]) <= _HARD_TRIM_KEEP_CHARS:
                return block
            return {**block, "text":
                    block["text"][:_HARD_TRIM_KEEP_CHARS] + "\n"
                    + _elision_note(_size(block["text"]), None)}
        if block.get("type") == "tool_result":
            return {**block, "content": _elision_note(
                _size(block.get("content")), block.get("tool_use_id"))}
        if "input" in block:
            return {**block, "input": {"elided": _elision_note(
                _size(block.get("input")), block.get("id"))}}
    return {"type": "text", "text": _elision_note(_size(block), None)}
