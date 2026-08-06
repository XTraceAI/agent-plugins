#!/usr/bin/env python3
"""What to call a captured session.

Claude Code names a session by writing its own generated title into the
transcript as an ``ai-title`` record, and the capture paths simply forward it.
That works for an interactive session and for nothing else:

* **Headless runs never get one.** A session started through the SDK or
  ``claude -p`` (``entrypoint: "sdk-cli"``) emits no ``ai-title`` at all —
  title generation is a UI feature. Measured locally: 168 of 178 ``cli``
  sessions carry one, **0 of 2 ``sdk-cli`` and 0 of 5 ``claude-desktop``
  ones do**. Every headless capture therefore lands unnamed, and a sessions
  list reads as a wall of untitled rows even though the messages arrived.
* **A renamed session keeps reporting its old name.** When the user renames
  a session the client writes a ``custom-title`` record — and goes on
  emitting the STALE ``ai-title`` beside it, on nearly every turn. Measured
  on a renamed session: 130 ``ai-title`` records all reading "Test updated
  brain MCP feature" interleaved with 47 ``custom-title`` records reading
  "better manifest and idex", with an ``ai-title`` frequently LAST. So
  "whichever came last" resolves to the name the user replaced.

Hence the two rules here. Precedence is by TYPE, never by position: an
explicit rename outranks a generated title no matter which record the client
wrote most recently. And when neither exists, the session's first real prompt
is better than nothing — a headless run is usually named by what it was asked
to do.

Stdlib only, no import-time side effects: ``flush_turn`` imports this inside a
Stop hook, where an import-time failure surfaces as a traceback in the user's
session.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcript_filter import is_command_wrapper, record_text  # noqa: E402

# Client-written blocks that are not prose the user typed. Stripped before a
# prompt becomes a title so a message that merely CARRIES one still titles the
# session by what the person actually said.
_STRIP_TAGS = (
    "system-reminder",
    "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-stderr", "local-command-caveat",
)
_STRIP_ELEMENT = re.compile(
    r"<(" + "|".join(_STRIP_TAGS) + r")>.*?</\1>", re.DOTALL,
)

# Long enough to stay a recognisable sentence, short enough to read as a title
# in a sessions list. Claude's own titles run 3-6 words; a prompt-derived one
# is a fallback and may be a full sentence, so it gets more room than that.
_MAX_LEN = 80


def _last_of(records: list, record_type: str, field: str) -> str | None:
    """The last non-empty ``field`` among ``record_type`` records."""
    found = None
    for record in records:
        if not isinstance(record, dict) or record.get("type") != record_type:
            continue
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            found = value.strip()
    return found


def custom_title(records: list) -> str | None:
    """The name the USER gave this session, if they renamed it."""
    return _last_of(records, "custom-title", "customTitle")


def generated_title(records: list) -> str | None:
    """The name CLAUDE CODE generated for this session, if it did.

    Regenerated as the session develops, so the last one in a batch wins —
    but only among ``ai-title`` records. A rename always outranks this.
    """
    return _last_of(records, "ai-title", "aiTitle")


def _is_tool_result(record: dict) -> bool:
    """True for a tool result, which Claude Code types as a USER record."""
    if record.get("toolUseResult") is not None:
        return True
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def prompt_title(records: list, limit: int = _MAX_LEN) -> str | None:
    """A title derived from the first thing the user actually asked for.

    Last resort, and only ever used when the client wrote no title record at
    all — which in practice means a headless run. It is skipped for anything
    the user did not type: tool results and sidechains (both typed ``user``),
    slash-command bookkeeping, and ``isMeta`` records, which is how the client
    writes its own output — a ``/context`` dump would otherwise title the
    session "## Context Usage **Model:** …".
    """
    for record in records:
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        if record.get("isSidechain") or record.get("isMeta"):
            continue
        if _is_tool_result(record) or is_command_wrapper(record):
            continue
        text = _STRIP_ELEMENT.sub(" ", record_text(record))
        text = re.sub(r"\s+", " ", text).strip()
        # A leftover unclosed tag means the record is client bookkeeping whose
        # shape we do not recognise — keep looking rather than title the
        # session with markup.
        if not text or text.startswith("<"):
            continue
        if len(text) <= limit:
            return text
        # Cut on a word boundary when there is one near the end, so the title
        # does not break mid-word.
        head = text[:limit - 1]
        space = head.rfind(" ")
        if space >= limit // 2:
            head = head[:space]
        return head.rstrip(" ,.;:—-") + "…"
    return None
