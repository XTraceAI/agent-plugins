#!/usr/bin/env python3
"""The current turn, in order, for the rulebook's rule judge.

`rulebook_hook.py` asks the server whether a rule its pattern matched fits the
turn in hand (`POST /v1/team/rulebook/judge`, rule-judge-spec §3, §5). The
judge needs the turn as it happened: what the person asked, what the agent
said, each call it made and the head of what each call returned, oldest first.

`harness_extract.read_transcript` reads the same file but for another
question: it keeps one turn's calls and results in two separate lists and only
the agent's LAST text, so the order between them is gone. This reader keeps
the order and nothing else. It borrows that module's own statements of what a
person's message is (`_SYS_BLOCK`, `is_harness_text`, `_text_of`,
`STOP_FEEDBACK_PREFIX`), so the two cannot come to disagree about where a turn
starts or ends:

  * a turn starts at a `user` record that is not a tool result, not `isMeta`
    and not harness text (a loop wake-up, a task notification, a skill body);
    harness text does not start one, and what follows it stays in the turn
    already in progress;
  * a blocked Stop — an `isMeta` record opening with `Stop hook feedback:`, or
    a Stop hook's `additionalContext` attachment — ENDS the turn. What the
    agent does after it belongs to no turn, and `read_turn` then returns the
    empty turn.

What is kept, per record of the turn:

  person   the person's message, system blocks stripped        ≤ 2000 chars
  agent    each assistant text block                           ≤ 1500 chars
  call     each tool call, "<Tool>: <command | path | input>"  ≤  300 chars
  result   each tool result's head, newlines → " | "           ≤  300 chars

Dropped: thinking, system reminders (also where they ride inside a result),
`isMeta` records, attachments (hook-injected context), harness text. At most
`MAX_ENTRIES` entries, the LAST ones — the call being judged is at the end.

The file is walked once, from its end, and only the turn in progress is
parsed: a transcript grows to megabytes and this runs inside PreToolUse.
Past `MAX_BYTES` only the tail is read; a turn that starts before it is not
found, and that is the empty turn too.

Nothing here redacts. The caller passes `redact`, which is applied to every
text BEFORE it is cut to its cap — cut first and a credential straddling the
cut no longer has the shape a denylist recognises. Stdlib only; never raises.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

MAX_ENTRIES = 200             # the server refuses a longer `turn`
PERSON_CHARS = 2000           # the server's `person_request` bound
AGENT_CHARS = 1500
CALL_CHARS = 300
RESULT_CHARS = 300
MAX_BYTES = 16 * 1024 * 1024  # transcript larger than this: only its tail is read
_SLACK = 4                    # text handed to `redact` is this many caps long at most

EMPTY = {"turn_id": "", "person_request": "", "turn": []}


def _hx():
    """`harness_extract`, which owns what a person's message is."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import harness_extract  # noqa: PLC0415 — beside this file
    return harness_extract


def _cut(text, cap, redact):
    """`text` redacted, then cut to `cap`. The redactor sees a bounded slice —
    a 5 MB tool result is not run through a dozen regexes for 300 chars."""
    text = (text or "")[:cap * _SLACK]
    if redact is not None:
        try:
            text = redact(text) or ""
        except Exception:
            return ""          # a text that could not be redacted is not sent
    return text[:cap]


def _add(entries, kind, text, cap, redact):
    """Append one entry, unless nothing is left of its text: an empty result,
    or a text the redactor refused."""
    text = _cut(text, cap, redact)
    if text:
        entries.append({"kind": kind, "text": text})


def _call_text(name, tool_input):
    """`<Tool>: <what it addressed>` — the command, the path, else the input."""
    i = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        what = str(i.get("command", ""))
    else:
        what = str(i.get("file_path", "") or "")
        if not what:
            try:
                what = json.dumps(i, default=str) if i else ""
            except Exception:
                what = ""
    return f"{name}: {' '.join(what.split())}"


def _lines(path):
    """The transcript's lines, or its tail's. A cut first line is dropped."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_BYTES:
            f.seek(size - MAX_BYTES)
            f.readline()
        return f.read().splitlines()


def read_turn(path, redact=None):
    """`{"turn_id", "person_request", "turn": [{"kind", "text"}]}` for the last
    human turn of the transcript at `path`.

    `turn_id` is the uuid of the turn's human message ("" when the record has
    none). `EMPTY`'s shape with nothing in it when there is no path, the file
    cannot be read, no human message is found, or the turn was ended by a
    blocked Stop. Never raises."""
    if not path:
        return dict(EMPTY, turn=[])
    try:
        hx = _hx()
        lines = _lines(str(path))
    except Exception:
        return dict(EMPTY, turn=[])
    try:
        return _read(hx, lines, redact)
    except Exception:
        return dict(EMPTY, turn=[])


def _read(hx, lines, redact):
    groups = []        # one list of entries per record, newest record first
    kept = 0
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if kind == "attachment":
            att = rec.get("attachment")
            if (isinstance(att, dict) and att.get("type") == "hook_additional_context"
                    and att.get("hookEvent") == "Stop"):
                return dict(EMPTY, turn=[])       # the turn ended here
            continue                              # any other injected context
        content = (rec.get("message") or {}).get("content") \
            if isinstance(rec.get("message"), dict) else None
        if kind == "assistant" and isinstance(content, list):
            if kept >= MAX_ENTRIES:
                continue      # full; keep walking only to find the human message
            entries = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text") or "").strip():
                    text = hx._SYS_BLOCK.sub("", str(b["text"])).strip()
                    _add(entries, "agent", text, AGENT_CHARS, redact)
                elif b.get("type") == "tool_use":
                    _add(entries, "call", _call_text(str(b.get("name") or ""), b.get("input")),
                         CALL_CHARS, redact)
            groups.append(entries)
            kept += len(entries)
            continue
        if kind != "user":
            continue
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            if kept >= MAX_ENTRIES:
                continue
            entries = []
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                body = b.get("content")
                body = body if isinstance(body, str) else hx._text_of(body)
                body = hx._SYS_BLOCK.sub("", body or "").strip()
                _add(entries, "result", " | ".join(body[:RESULT_CHARS * _SLACK].splitlines()),
                     RESULT_CHARS, redact)
            groups.append(entries)
            kept += len(entries)
            continue
        text = hx._SYS_BLOCK.sub("", hx._text_of(content)).strip()
        if rec.get("isMeta"):
            if text.startswith(hx.STOP_FEEDBACK_PREFIX):
                return dict(EMPTY, turn=[])       # a blocked Stop: the turn ended here
            continue                              # a skill body, a command's expansion
        if hx.is_harness_text(text):
            continue                              # a wake-up; the turn began earlier
        person = _cut(text, PERSON_CHARS, redact)
        turn = [{"kind": "person", "text": person}] if person else []
        for entries in reversed(groups):
            turn.extend(entries)
        uid = rec.get("uuid")
        return {"turn_id": uid if isinstance(uid, str) else "",
                "person_request": person, "turn": turn[-MAX_ENTRIES:]}
    return dict(EMPTY, turn=[])
