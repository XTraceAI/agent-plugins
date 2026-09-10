"""OpenAI Codex session reader: rollout transcripts → Claude Code records.

Moved from ``codex/codex_to_claude.py`` + the locate half of
``codex/import_codex_session.py`` (both remain as thin shims for one release).

Transforms an OpenAI Codex *rollout* transcript into the Claude Code record
shape that MemHub's ``import_conversation`` auto-detects as a coding-agent
transcript — the tool-aware **agentic** ingestion path (facts + episodes + the
session gist).

Why re-shape instead of adding a Codex detector server-side: the agentic path
keys off *structure* ("records with a nested ``message`` and tool-call /
tool-result blocks"), while ``source_platform`` records provenance. A faithful
client-side transform gets the full agentic extraction and the import still
stores the real ``codex`` platform.

Codex rollout envelope (one JSON object per line)::

    {"timestamp": ..., "type": <t>, "payload": {...}}

The conversation lives in the ``response_item`` stream (the OpenAI Responses
API items actually exchanged with the model — this is what carries tool I/O in
order). The parallel ``event_msg`` stream is ignored for CONTENT because it
duplicates text without tool-call structure, but its cumulative ``token_count``
snapshots are the rollout's authoritative usage source.

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
    event_msg token_count              -> usage on the latest assistant record
    (role=developer / system prompt injections are skipped as noise)
"""
from __future__ import annotations

import glob
import json
import re
import sys
import uuid as _uuid
from collections import deque
from pathlib import Path
from typing import Any

# The readers are imported both as a package and, by some callers, with
# ``scripts/`` already on the path; pin the parent either way so the shared
# title rules resolve. ``session_title`` is stdlib-only with no import-time
# side effects, which is what makes it safe on the hook path. Guarded so
# importing the package does not keep prepending the same entry.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from session_title import normalize_title  # noqa: E402

HOST = "codex"

# Not $CODEX_HOME: this module has never read that variable, and
# `sessions_root()` is a containment boundary for payload-supplied
# paths, so widening it is a separate change with its own review.
_CODEX_DIR = Path.home() / ".codex"
_SESSIONS = _CODEX_DIR / "sessions"
# Sidecar index of every thread Codex has named: one
# {"id", "thread_name", "updated_at"} object per line. Strictly more complete
# than the rollouts — a Codex Desktop session can be named here while its own
# rollout carries no ``thread_name_updated`` record at all — so it is the
# fallback when the rollout has none. See ``_sidecar_thread_name``.
_SESSION_INDEX = _CODEX_DIR / "session_index.jsonl"
# The index grows with every session the user has ever run and is read on a
# hook path, so the scan is bounded rather than trusting the file to be small.
# The byte bound is the real one (the read is seeked to the tail); the line cap
# is the belt-and-braces second limit. 10k rows of this shape is ~1.4 MB, so
# 4 MB comfortably holds the window even if Codex widens the record.
_INDEX_MAX_LINES = 10_000
_INDEX_TAIL_BYTES = 4 * 1024 * 1024
# Matches the cap the send sites already apply, so `meta["title"]` cannot come
# out longer on one capture path than another. The pre-change reader bounded
# every title unconditionally (`[:150]`); the verbatim lane keeps that promise.
_MAX_NAME = 200


def sessions_root() -> Path:
    """The rollout store this reader will ever touch — capture callers use
    it as a containment boundary for payload-supplied paths."""
    return _SESSIONS

# The user's real ask is wrapped by the Codex VSCode extension under this
# heading, after an "# Context from my IDE setup:" preamble.
_IDE_REQUEST_RE = re.compile(r"##\s*My request(?: for Codex)?:\s*\n", re.I)

# Codex Desktop can prepend these app-owned blocks as a Responses-API ``user``
# item before the person's prompt. Strip a leading sequence rather than dropping
# the whole item so a future host version can append the real ask to the same
# item without losing it.
_APP_CONTEXT_BLOCK_RE = re.compile(
    r"\A\s*<(recommended_plugins|environment_context)>.*?</\1>\s*",
    re.S,
)
_AGENTS_XML_BLOCK_RE = re.compile(
    r"\A\s*# AGENTS\.md instructions[^\n]*\n+\s*"
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*",
    re.S,
)

# Codex rollout files are named rollout-<ISO-timestamp>-<uuid>.jsonl.
_ROLLOUT_UUID_RE = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def clean_user_text(text: str) -> str | None:
    """Strip Codex context injections, returning the real user ask — or None
    when the message is pure injected context.

    Codex prepends several non-user "user" turns: the ``# AGENTS.md
    instructions`` block, app-owned ``<recommended_plugins>`` and
    ``<environment_context>`` metadata blobs, and (VSCode extension) an
    ``# Context from my IDE setup:`` preamble that wraps the real request under
    a ``## My request for Codex:`` heading. Plain CLI turns pass through
    untouched."""
    t = (text or "").strip()
    if not t:
        return None
    while True:
        for pattern in (_APP_CONTEXT_BLOCK_RE, _AGENTS_XML_BLOCK_RE):
            match = pattern.match(t)
            if match:
                t = t[match.end():].strip()
                break
        else:
            break
    if not t:
        return None
    # Older CLI rollouts can carry an unstructured AGENTS.md blob with no
    # closing delimiter. There is no safe boundary at which a user ask could be
    # recovered, so preserve the established behavior and drop that item.
    if t.startswith("# AGENTS.md instructions"):
        return None
    if t.startswith("# Context from my IDE setup:"):
        m = _IDE_REQUEST_RE.search(t)
        req = t[m.end():].strip() if m else ""
        return req or None  # a context-only refresh has no ask → drop
    return t


def load_rollout(path, *, strict_utf8: bool = False, strict_json: bool = False) -> list[dict]:
    """Parse a Codex rollout .jsonl tolerantly (skip malformed lines, e.g. a
    truncated final line from an interrupted write).

    Explicit utf-8 for the same reason as the Claude transcript reader: rollouts
    are UTF-8, a bare read_text() decodes with the OS locale codec, and one
    em-dash then kills the whole import on a cp950/cp1252 box."""
    records: list[dict] = []
    errors = "strict" if strict_utf8 else "replace"
    for raw in Path(path).read_text(encoding="utf-8", errors=errors).splitlines(keepends=True):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if strict_json and raw.endswith(("\n", "\r")):
                raise
            continue
        # The return type says list[dict] and every consumer walks these with
        # ``r.get(...)``. A line holding a bare JSON scalar (``null``, a number,
        # a quoted string) parses fine and used to land in the list anyway, so
        # one such line raised AttributeError deep in the transform — inside a
        # Stop hook, where that surfaces as a traceback in the user's session.
        # Dropped here, once, rather than guarded at every walk.
        if isinstance(record, dict):
            records.append(record)
        elif strict_json:
            raise ValueError("Codex rollout row is not an object")
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
        # Non-dict guard: see _rollout_thread_name. This is the FIRST thing
        # to touch a rollout, so an unguarded .get() here crashes the whole
        # Stop hook before any of the later guards can matter.
        if not isinstance(r, dict):
            continue
        if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict):
            return r["payload"]
    return {}


_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


def _usage_total(value: Any) -> dict[str, int] | None:
    """A validated Codex cumulative-usage snapshot, or ``None``.

    Booleans are ints in Python but not token counts. Missing cache fields are
    zero for older rollouts; missing input/output fields make the snapshot
    unusable rather than turning an incomplete event into measured zeroes.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for key in _USAGE_KEYS:
        raw = value.get(key, 0 if "cache" in key else None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        out[key] = raw
    return out


def _usage_delta(current: dict[str, int], previous: dict[str, int] | None
                 ) -> dict[str, int] | None:
    """Map a cumulative Codex snapshot to one Claude-shaped usage delta.

    Codex may re-emit an unchanged ``last_token_usage`` on a rate-limit-only
    update, so the cumulative counters — not ``last_token_usage`` — are the
    dedup boundary. Codex ``input_tokens`` includes cache reads and writes;
    MemHub stores those separately, therefore ``input_tokens`` below is only
    the fresh remainder. This makes MemHub's four-field sum equal Codex's raw
    input + output total instead of double-counting the prompt cache.
    """
    before = previous or {key: 0 for key in _USAGE_KEYS}
    if any(current[key] < before[key] for key in _USAGE_KEYS):
        return None
    delta = {key: current[key] - before[key] for key in _USAGE_KEYS}
    if not any(delta.values()):
        return None
    fresh = max(
        0,
        delta["input_tokens"]
        - delta["cached_input_tokens"]
        - delta["cache_write_input_tokens"],
    )
    return {
        "input_tokens": fresh,
        "output_tokens": delta["output_tokens"],
        "cache_read_input_tokens": delta["cached_input_tokens"],
        "cache_creation_input_tokens": delta["cache_write_input_tokens"],
    }


def _merge_usage(record: dict, usage: dict[str, int]) -> None:
    message = record.get("message")
    if not isinstance(message, dict):
        return
    target = message.setdefault("usage", {})
    if not isinstance(target, dict):
        target = {}
        message["usage"] = target
    for key, value in usage.items():
        target[key] = target.get(key, 0) + value


def _one_line(name: str) -> str | None:
    """A host-generated name, made safe to carry without being reshaped.

    "Verbatim" is about not *rewriting* Codex's name — spacing and length are
    its own — but two invariants the old ``splitlines()[0][:150]`` provided for
    free still have to hold. A title is one line: a ``\\r`` in it would let a
    rollout overwrite the line ``import_session`` prints to the terminal. And
    it is bounded: without a cap an absurd name reaches ``--title`` as argv and
    the manual-import path dies with E2BIG before it can send anything.

    Real names run 18-31 characters, so neither limit is reached in practice.
    """
    first = name.strip().splitlines()
    return first[0].strip()[:_MAX_NAME] if first and first[0].strip() else None


def _rollout_thread_name(rollout: list[dict]) -> str | None:
    """The name CODEX gave this thread, as recorded in the rollout itself.

    Codex names a substantive thread and shows that name in its own UI, writing
    it as ``event_msg``/``thread_name_updated`` around line 8-10 — right after
    the first turn's ``task_complete``, so it is already there on the first
    ``Stop`` flush rather than only at session end.

    The record type is ``_updated``, so it can repeat; the LAST one wins, which
    is both how a regenerated name resolves and how a rename does. Same rule
    ``session_title.generated_title`` applies to Claude's ``ai-title``.
    """
    found = None
    for r in rollout:
        # ``load_rollout`` appends whatever ``json.loads`` returns, so a line
        # holding a bare scalar (``null``, a number) arrives as a non-dict.
        # Titling runs inside a Stop hook; an AttributeError here is a
        # traceback in the user's session.
        if not isinstance(r, dict):
            continue
        pl = r.get("payload")
        if not isinstance(pl, dict) or r.get("type") != "event_msg":
            continue
        if pl.get("type") != "thread_name_updated":
            continue
        name = pl.get("thread_name")
        if isinstance(name, str) and name.strip():
            found = _one_line(name) or found
    return found


def _sidecar_thread_name(session_id: str | None, *, strict_utf8=False, strict_json=False) -> str | None:
    """The name Codex gave this thread, from the ``session_index.jsonl``
    sidecar — for the sessions whose rollout does not carry one.

    Measured: a Codex Desktop session was named "Add memhub claude plugin" in
    the index while its rollout held no ``thread_name_updated`` record at all.
    Without this lookup a Desktop session falls through to the prompt fallback
    and MemHub disagrees with the name Codex is displaying.

    Degrades to ``None`` on absolutely anything — a missing file is the normal
    case on a fresh machine or a Codex build that writes no index, and this
    runs inside a ``Stop`` hook where a raised exception is a user-visible
    traceback.
    """
    if not session_id:
        return None
    try:
        found = None
        # The TAIL, not the head. The index is append-ordered — oldest session
        # first — so the row for the session being flushed is always among the
        # newest. Bounding from the front would make the lane silently die on a
        # long-time user's index and send every Codex Desktop session back to
        # the prompt fallback, which is the bug this lane exists to fix.
        #
        # SEEK to the tail rather than reading forward and keeping the last N
        # lines: that bounds memory but not the READ, and the read is what has
        # to be bounded — this runs on every flush of an unnamed rollout. A
        # byte bound also contains a pathological index (a single unterminated
        # line) that a line count cannot. Opened binary so the offset is in
        # bytes; a seek can land mid-character and mid-record, hence
        # errors="replace" and dropping the first partial line.
        with open(_SESSION_INDEX, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - _INDEX_TAIL_BYTES)
            fh.seek(start)
            blob = fh.read()
        if start:
            # Drop the incomplete byte prefix before strict UTF-8 decoding;
            # the seek may have landed inside a multi-byte character.
            blob = blob.partition(b"\n")[2]
        lines = blob.decode("utf-8", errors="strict" if strict_utf8 else "replace").splitlines(keepends=True)
        for raw in deque(lines, maxlen=_INDEX_MAX_LINES):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — a torn final line is normal
                if strict_json and raw.endswith(("\n", "\r")):
                    raise
                continue
            if not isinstance(row, dict):
                if strict_json:
                    raise ValueError("Codex title index row is not an object")
                continue
            if row.get("id") != session_id:
                continue
            name = row.get("thread_name")
            if isinstance(name, str) and name.strip():
                found = _one_line(name)
        return found
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — legacy capture remains best-effort
        if strict_utf8 or strict_json:
            raise
        return None


def _title(rollout: list[dict], session_id: str | None = None, *, strict_utf8=False, strict_json=False, title_index=None) -> str | None:
    """What Codex calls this session, else the best name we can derive.

    Precedence, and why: MemHub should show the title Codex's own UI shows.
    So the name Codex generated wins outright — from the rollout first (it is
    per-session and is the file we were handed), then from the global sidecar
    index. Only when Codex never named the thread do we derive one, and then
    the user's opening request beats the agent's closing summary because it
    reads as a topic rather than as a report.

    A host-generated name is passed through VERBATIM: it is already a title,
    and reshaping it would reintroduce the very disagreement this ladder
    exists to remove. Only the derived fallbacks are normalized.
    """
    thread_name = (_rollout_thread_name(rollout)
                   or (title_index.get(session_id) if title_index is not None else
                       _sidecar_thread_name(session_id, strict_utf8=strict_utf8, strict_json=strict_json)))
    if thread_name:
        return thread_name

    last_complete = None
    first_user = None
    for r in rollout:
        if not isinstance(r, dict):  # see _rollout_thread_name
            continue
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
    return normalize_title(first_user or last_complete)


def rollout_to_claude_records(rollout: list[dict], *, strict_utf8=False, strict_json=False, title_index=None) -> tuple[list[dict], dict]:
    """Return ``(claude_records, meta)``.

    ``meta`` = ``{session_id, cwd, model, originator, cli_version, title}``.
    ``claude_records`` are Claude-Code-shaped and carry ``cwd`` so
    ``import_session._namespace_from_records`` can resolve the repo. Platform,
    model, session, and cwd provenance live in structured metadata instead of a
    synthetic user turn, keeping titles and turn counts faithful."""
    sm = _session_meta(rollout)
    cwd = sm.get("cwd") if isinstance(sm.get("cwd"), str) else None
    model = None
    for r in rollout:
        if not isinstance(r, dict):  # see _rollout_thread_name
            continue
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
        "title": _title(rollout, sm.get("id"), strict_utf8=strict_utf8, strict_json=strict_json, title_index=title_index),
        "host": HOST,
    }

    out: list[dict] = []
    sid_key = sm.get("id") or "unknown"
    ts_holder = {"ts": None}
    # Reader versions through 0.27.4 emitted a provenance banner at identity
    # index 0. Keep a virtual slot for it so every real record retains its UUID
    # on incremental re-import even though the banner is no longer emitted.
    identity_index = 1

    def rec(record: dict) -> dict:
        nonlocal identity_index
        if cwd:
            record["cwd"] = cwd
        # The server's agentic parser SKIPS records without a ``uuid`` (it is
        # the per-record replay-dedup key) and lifts ``event_date`` from
        # ``timestamp`` — records missing them import as nothing, silently.
        # Deterministic uuid5 over (session, legacy output index) so a re-import
        # folds forward instead of duplicating. ``identity_index`` can reserve
        # slots for synthetic rows removed by newer readers.
        record["uuid"] = str(_uuid.uuid5(
            _uuid.NAMESPACE_URL, f"memhub:codex:{sid_key}:{identity_index}"))
        identity_index += 1
        if ts_holder["ts"]:
            record["timestamp"] = ts_holder["ts"]
        return record

    def reserve_legacy_identity() -> None:
        nonlocal identity_index
        identity_index += 1

    def user(content) -> dict:
        return rec({"type": "user", "message": {"role": "user", "content": content}})

    def recovered_user(content: str, source_index: int) -> dict:
        """A real ask recovered from a wrapper 0.27.4 dropped wholesale.

        It must not consume a legacy output index: doing so would shift every
        later record onto a new UUID during incremental re-import. A separate
        source-indexed namespace adds the missing ask while preserving all
        previously acknowledged identities.
        """
        record = {"type": "user", "message": {"role": "user", "content": content}}
        if cwd:
            record["cwd"] = cwd
        record["uuid"] = str(_uuid.uuid5(
            _uuid.NAMESPACE_URL,
            f"memhub:codex:{sid_key}:recovered-user:{source_index}",
        ))
        if ts_holder["ts"]:
            record["timestamp"] = ts_holder["ts"]
        return record

    def assistant(block) -> dict:
        message = {"role": "assistant", "content": [block]}
        if model:
            message["model"] = model
        return rec({"type": "assistant", "message": message})

    ts_holder["ts"] = next((r.get("timestamp") for r in rollout
                            if isinstance(r.get("timestamp"), str)), None)

    last_assistant: dict | None = None
    previous_usage_total: dict[str, int] | None = None

    def append_assistant(block: dict) -> None:
        nonlocal last_assistant
        last_assistant = assistant(block)
        out.append(last_assistant)

    def append_usage_only(usage: dict[str, int], event_idx: int) -> None:
        # A model request can consume tokens without yielding an emit-worthy
        # response item (empty assistant text, compaction, failed generation).
        # Preserve those counters immediately on an empty assistant turn. The
        # event index keeps its identity stable across incremental re-imports
        # and distinct from normal output rows and other usage-only events.
        record = assistant({"type": "text", "text": ""})
        record["uuid"] = str(_uuid.uuid5(
            _uuid.NAMESPACE_URL,
            f"memhub:codex:{sid_key}:usage-only:{event_idx}",
        ))
        _merge_usage(record, usage)
        out.append(record)

    for idx, r in enumerate(rollout):
        pl = r.get("payload")
        if (r.get("type") == "event_msg" and isinstance(pl, dict)
                and pl.get("type") == "token_count"):
            if isinstance(r.get("timestamp"), str):
                ts_holder["ts"] = r["timestamp"]
            info = pl.get("info")
            total = _usage_total(
                info.get("total_token_usage") if isinstance(info, dict) else None
            )
            if total is not None:
                usage = _usage_delta(total, previous_usage_total)
                # A regressing/malformed cumulative snapshot is ignored and
                # does not poison the baseline for later valid snapshots.
                if (previous_usage_total is None
                        or all(total[k] >= previous_usage_total[k]
                               for k in _USAGE_KEYS)):
                    previous_usage_total = total
                if usage:
                    if last_assistant is not None:
                        _merge_usage(last_assistant, usage)
                        # One cumulative delta belongs to one model request.
                        # Until another response item is emitted, a later
                        # advancing snapshot has no assistant output to own it
                        # and must become a usage-only record rather than pile
                        # onto this now-complete request.
                        last_assistant = None
                    else:
                        append_usage_only(usage, idx)
            continue
        if r.get("type") != "response_item":
            continue
        if isinstance(r.get("timestamp"), str):
            ts_holder["ts"] = r["timestamp"]
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
                    raw = text.lstrip()
                    if (raw.startswith("# AGENTS.md instructions")
                            or raw.startswith("<environment_context>")):
                        out.append(recovered_user(ask, idx))
                    else:
                        # A recommended_plugins-led item was already emitted by
                        # 0.27.4, so its cleaned ask must consume that SAME
                        # legacy slot. Moving it to recovered_user would add a
                        # duplicate ask beside the acknowledged wrapper row.
                        out.append(user(ask))
                elif text.lstrip().startswith("<recommended_plugins>"):
                    # 0.27.4 treated this app-owned preamble as a real user
                    # record. Reserve its former index so later real records
                    # keep the UUIDs already acknowledged by MemHub.
                    reserve_legacy_identity()
            elif role == "assistant":
                append_assistant({"type": "text", "text": text})

        elif pt == "reasoning":
            summary = _text_of(pl.get("summary")).strip()
            if summary:
                append_assistant({"type": "thinking", "thinking": summary})

        elif pt in ("function_call", "custom_tool_call"):
            # Real Codex tool calls always carry call_id; synthesize a unique,
            # non-None id if a malformed record omits it (the matching output
            # carries the same call_id, so pairing still holds).
            call_id = pl.get("call_id") or pl.get("id") or f"codex-call-{idx}"
            append_assistant({
                "type": "tool_use",
                "id": call_id,
                "name": pl.get("name") or "tool",
                "input": _tool_input(pl),
            })

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


def rollout_uuid(path) -> str | None:
    """The trailing session UUID of a rollout filename, or None if it doesn't
    match the ``rollout-<ts>-<uuid>`` pattern."""
    m = _ROLLOUT_UUID_RE.search(Path(path).stem)
    return m.group(1) if m else None


def _rollout_files(on_error=None) -> list[Path]:
    if on_error is not None:
        from .discovery import paths
        return paths(_SESSIONS, ("**", "rollout-*.jsonl"), on_error)
    return [Path(f) for f in glob.glob(str(_SESSIONS / "**" / "rollout-*.jsonl"),
                                       recursive=True)]


# `session_meta` is the rollout's first record in every layout observed; a few
# hundred lines of slack costs nothing and parsing the whole rollout to read
# one key costs a lot on a long session.
_META_MAX_RECORDS = 200


def session_metadata(path, *, strict_utf8: bool = True) -> dict:
    """Read native session identity without reading prompt-derived titles."""
    with Path(path).open("r", encoding="utf-8", errors="strict" if strict_utf8 else "replace") as handle:
        for _ in range(_META_MAX_RECORDS):
            line = handle.readline(1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 1024 * 1024:
                raise ValueError("session metadata probe exceeded its line bound")
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = _session_meta([record])
            if payload:
                git = payload.get("git")
                return {
                    "session_id": payload.get("id"),
                    "cwd": payload.get("cwd"),
                    "source_surface": payload.get("originator"),
                    "started_at": payload.get("timestamp") or record.get("timestamp"),
                    "git_branch": git.get("branch") if isinstance(git, dict) else None,
                }
    return {}


def session_cwd(path) -> str | None:
    """The native working directory, using the bounded session metadata read."""
    try:
        cwd = session_metadata(path, strict_utf8=False).get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    except (OSError, ValueError):
        return None


def list_sessions(limit: int | None = 20, *, on_error=None) -> list[dict]:
    """Most recent rollouts; discovery callers can receive access failures."""
    rows = []
    for path in _rollout_files(on_error):
        try:
            rows.append({"id": rollout_uuid(path) or path.stem, "path": str(path),
                         "mtime": path.stat().st_mtime, "host": HOST, "cwd": None})
        except OSError as error:
            if on_error is None:
                raise
            on_error(error)
    return sorted(rows, key=lambda row: row["mtime"], reverse=True)[:limit]


def locate(ref: str) -> tuple[Path | None, str]:
    """Accept a rollout path, ``latest``, or a bare Codex session id (UUID)."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p, ""
    if "/" in ref and ref != "latest":
        return None, f"rollout file not found: {p}"
    files = _rollout_files()
    if not files:
        return None, f"no Codex rollouts under {_SESSIONS}"
    if ref == "latest":
        return max(files, key=lambda f: f.stat().st_mtime), ""
    # Match the session UUID exactly — a partial/fragment id does NOT match (it
    # would risk selecting the wrong session and folding-forward the wrong
    # conversation's gist). Ambiguity is an error, never a largest-file guess.
    sid = ref.removesuffix(".jsonl")
    hits = [f for f in files if rollout_uuid(f) == sid]
    if not hits:
        return None, (f"no Codex rollout with session UUID {sid!r} under "
                      f"{_SESSIONS} (pass the full UUID or a rollout path)")
    if len(hits) > 1:
        return None, (f"ambiguous session id {sid!r}: {len(hits)} rollouts match — "
                      "pass the full session UUID or the rollout path")
    return hits[0], ""


def to_canonical(path, *, strict_utf8: bool = False, strict_json: bool = False, title_index=None) -> tuple[list[dict], dict]:
    """Normalize a rollout; an optional complete title index supports historical exports."""
    return rollout_to_claude_records(load_rollout(path, strict_utf8=strict_utf8, strict_json=strict_json),
                                    strict_utf8=strict_utf8, strict_json=strict_json, title_index=title_index)
