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
import uuid as _uuid
from pathlib import Path
from typing import Any

HOST = "codex"

_SESSIONS = Path.home() / ".codex" / "sessions"


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


def load_rollout(path) -> list[dict]:
    """Parse a Codex rollout .jsonl tolerantly (skip malformed lines, e.g. a
    truncated final line from an interrupted write).

    Explicit utf-8 for the same reason as the Claude transcript reader: rollouts
    are UTF-8, a bare read_text() decodes with the OS locale codec, and one
    em-dash then kills the whole import on a cp950/cp1252 box."""
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
    ``claude_records`` are Claude-Code-shaped and carry ``cwd`` so
    ``import_session._namespace_from_records`` can resolve the repo. Platform,
    model, session, and cwd provenance live in structured metadata instead of a
    synthetic user turn, keeping titles and turn counts faithful."""
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


def _rollout_files() -> list[Path]:
    return [Path(f) for f in glob.glob(str(_SESSIONS / "**" / "rollout-*.jsonl"),
                                       recursive=True)]


def list_sessions(limit: int = 20) -> list[dict]:
    """Most recent rollouts, newest first."""
    files = sorted(_rollout_files(), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"id": rollout_uuid(f) or f.stem, "path": str(f),
             "mtime": f.stat().st_mtime, "host": HOST, "cwd": None}
            for f in files[:limit]]


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


def to_canonical(path) -> tuple[list[dict], dict]:
    """Load a rollout and transform it to Claude-shaped records."""
    return rollout_to_claude_records(load_rollout(path))
