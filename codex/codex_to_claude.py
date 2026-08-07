#!/usr/bin/env python3
"""Transform an OpenAI Codex *rollout* transcript into the Claude Code record
shape that MemHub's ``import_conversation`` auto-detects as a coding-agent
transcript — the tool-aware **agentic** ingestion path (facts + episodes + the
session gist).

Why re-shape instead of adding a Codex detector server-side: the agentic path
keys off *structure* ("records with a nested ``message`` and tool-call /
tool-result blocks"), not the ``source_platform`` tag (which the schema says is
ignored on that path). So a faithful client-side transform gets the full
agentic extraction with **no backend change**.

Codex rollout envelope (one JSON object per line)::

    {"timestamp": ..., "type": <t>, "payload": {...}}

The conversation lives in the ``response_item`` stream (the OpenAI Responses
API items actually exchanged with the model — this is what carries tool I/O in
order). The parallel ``event_msg`` stream is UI telemetry and is intentionally
ignored: it duplicates the text without the tool-call structure the agentic
detector needs, and mixing the two would double every user/assistant turn.

Mapping (order preserved — gpt-5.x emits a ``reasoning`` item *before* its
``function_call`` and the loop must keep that order)::

    response_item message role=user   -> user  text
    response_item message role=assistant -> assistant text block
    response_item reasoning           -> assistant thinking block (summary only;
                                         encrypted_content is opaque, dropped)
    response_item function_call       -> assistant tool_use block
    response_item custom_tool_call    -> assistant tool_use block (apply_patch …)
    response_item function_call_output-> user tool_result block
    response_item custom_tool_call_output -> user tool_result block
    (role=developer / system prompt injections are skipped as noise)
"""
from __future__ import annotations

import json
import re
from typing import Any

# The user's real ask is wrapped by the Codex VSCode extension under this
# heading, after an "# Context from my IDE setup:" preamble.
_IDE_REQUEST_RE = re.compile(r"##\s*My request(?: for Codex)?:\s*\n", re.I)


def clean_user_text(text: str) -> str | None:
    """Strip Codex context injections, returning the real user ask — or None
    when the message is pure injected context.

    Codex prepends several non-user "user" turns: the ``# AGENTS.md
    instructions`` block, an ``<environment_context>`` metadata blob, and (VSCode
    extension) an ``# Context from my IDE setup:`` preamble that wraps the real
    request under a ``## My request for Codex:`` heading. Plain CLI turns pass
    through untouched."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("# AGENTS.md instructions") or t.startswith("<environment_context>"):
        return None
    if t.startswith("# Context from my IDE setup:"):
        m = _IDE_REQUEST_RE.search(t)
        req = t[m.end():].strip() if m else ""
        return req or None  # a context-only refresh has no ask → drop
    return t


def load_rollout(path) -> list[dict]:
    """Parse a Codex rollout .jsonl tolerantly (skip malformed lines, e.g. a
    truncated final line from an interrupted write).

    Explicit utf-8 for the same reason as the Claude transcript reader: rollouts
    are UTF-8, a bare read_text() decodes with the OS locale codec, and one
    em-dash then kills the whole import on a cp950/cp1252 box."""
    from pathlib import Path
    records: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _text_of(content: Any) -> str:
    """Join the text pieces of a Responses-API content value (a list of
    ``{type: input_text|output_text|text|summary_text, text}`` blocks, or a
    bare string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _tool_input(payload: dict) -> dict:
    """Normalise a Codex tool call's arguments to a dict.

    ``function_call.arguments`` is a JSON string; ``custom_tool_call.input``
    (apply_patch etc.) is a raw string. Parse JSON when possible, else wrap the
    raw text so nothing is lost."""
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {"input": v}
        except json.JSONDecodeError:
            return {"input": raw}
    return {}


def _session_meta(rollout: list[dict]) -> dict:
    for r in rollout:
        if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict):
            return r["payload"]
    return {}


def _title(rollout: list[dict]) -> str | None:
    """Best-effort title: the final ``task_complete`` summary, else the first
    real user message's first line."""
    last_complete = None
    first_user = None
    for r in rollout:
        pl = r.get("payload")
        if not isinstance(pl, dict):
            continue
        if r.get("type") == "event_msg" and pl.get("type") == "task_complete":
            msg = pl.get("last_agent_message")
            if isinstance(msg, str) and msg.strip():
                last_complete = msg
        if (first_user is None and r.get("type") == "response_item"
                and pl.get("type") == "message" and pl.get("role") == "user"):
            txt = clean_user_text(_text_of(pl.get("content")))
            if txt:
                first_user = txt
    # Prefer the user's opening request (topic-like) over the closing summary.
    src = first_user or last_complete
    if not src:
        return None
    line = src.strip().splitlines()[0]
    return line[:150]


def rollout_to_claude_records(rollout: list[dict]) -> tuple[list[dict], dict]:
    """Return ``(claude_records, meta)``.

    ``meta`` = ``{session_id, cwd, model, originator, cli_version, title}``.
    ``claude_records`` are Claude-Code-shaped and carry ``cwd`` (so
    ``import_session._namespace_from_records`` can resolve the repo) plus a
    leading provenance banner (the agentic path always tags ``claude``, so
    origin would otherwise be lost)."""
    sm = _session_meta(rollout)
    cwd = sm.get("cwd") if isinstance(sm.get("cwd"), str) else None
    model = None
    for r in rollout:
        pl = r.get("payload")
        if isinstance(pl, dict) and r.get("type") == "turn_context" and pl.get("model"):
            model = pl["model"]
            break
    meta = {
        "session_id": sm.get("id"),
        "cwd": cwd,
        "model": model,
        "originator": sm.get("originator"),
        "cli_version": sm.get("cli_version"),
        "title": _title(rollout),
    }

    def rec(record: dict) -> dict:
        if cwd:
            record["cwd"] = cwd
        return record

    def user(content) -> dict:
        return rec({"type": "user", "message": {"role": "user", "content": content}})

    def assistant(block) -> dict:
        return rec({"type": "assistant",
                    "message": {"role": "assistant", "content": [block]}})

    out: list[dict] = []

    # Provenance banner: origin is otherwise lost (source_platform forced to
    # "claude" on the agentic path). Kept terse and bracketed as metadata.
    banner = "[Imported from OpenAI Codex"
    if model:
        banner += f" · model {model}"
    if sm.get("id"):
        banner += f" · session {sm['id']}"
    if cwd:
        banner += f" · cwd {cwd}"
    banner += "]"
    out.append(user(banner))

    for idx, r in enumerate(rollout):
        if r.get("type") != "response_item":
            continue
        pl = r.get("payload")
        if not isinstance(pl, dict):
            continue
        pt = pl.get("type")

        if pt == "message":
            role = pl.get("role")
            if role == "developer":
                continue  # sandbox/permissions system injection — noise
            text = _text_of(pl.get("content")).strip()
            if not text:
                continue
            if role == "user":
                ask = clean_user_text(text)
                if ask:  # drop AGENTS.md / environment_context / IDE-context noise
                    out.append(user(ask))
            elif role == "assistant":
                out.append(assistant({"type": "text", "text": text}))

        elif pt == "reasoning":
            summary = _text_of(pl.get("summary")).strip()
            if summary:
                out.append(assistant({"type": "thinking", "thinking": summary}))

        elif pt in ("function_call", "custom_tool_call"):
            # Real Codex tool calls always carry call_id; synthesize a unique,
            # non-None id if a malformed record omits it (the matching output
            # carries the same call_id, so pairing still holds).
            call_id = pl.get("call_id") or pl.get("id") or f"codex-call-{idx}"
            out.append(assistant({
                "type": "tool_use",
                "id": call_id,
                "name": pl.get("name") or "tool",
                "input": _tool_input(pl),
            }))

        elif pt in ("function_call_output", "custom_tool_call_output"):
            # An id-less output is inherently unpairable (its call_id is the only
            # link, and parallel calls make positional guessing wrong). Give it a
            # UNIQUE id so it orphans cleanly rather than mispairing to — or
            # duplicate-linking — an unrelated call. Never happens for real Codex.
            call_id = pl.get("call_id") or pl.get("id") or f"codex-out-{idx}"
            output = pl.get("output")
            if not isinstance(output, str):
                output = json.dumps(output) if output is not None else ""
            out.append(user([{
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": output,
            }]))

    return out, meta
