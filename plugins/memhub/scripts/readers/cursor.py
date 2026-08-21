"""Cursor session reader: cursor-agent chat stores → Claude Code records.

Store layout (undocumented; pinned by inspection on cursor-agent 2026.08,
``meta.json.schemaVersion == 1`` — this reader REFUSES other versions loudly
rather than misparse):

    ~/.cursor/chats/<md5-of-cwd>/<session-uuid>/store.db   (sqlite)
    ~/.cursor/chats/<md5-of-cwd>/<session-uuid>/meta.json
        {schemaVersion, cwd, createdAtMs, updatedAtMs, hasConversation}

``store.db`` is a content-addressed blob store: ``blobs(id TEXT PK, data
BLOB)`` where ``id`` is the hex hash of the content, plus ``meta(key, value)``
whose session row carries ``latestRootBlobId``. The conversation is a hash
tree: the root is a protobuf node whose repeated field 1 holds 32-byte child
hashes; interior nodes point at more nodes; leaves are PLAIN JSON messages in
the Vercel AI SDK chat shape:

    {"role": "system"|"user"|"assistant"|"tool", "content": str | [blocks]}
    assistant blocks: {"type": "reasoning"|"text"|"tool-call", ...}
      tool-call: {toolCallId, toolName, args}
    tool blocks: {"type": "tool-result", toolCallId, toolName, result,
                  experimental_content: [{type: "text", text}]}

Mapping to Claude records mirrors the codex reader: reasoning → thinking
(signatures dropped — opaque), text → text, tool-call → tool_use,
tool-result → tool_result; the system prompt and Cursor's context injections
(``<user_info>``, ``<git_status>``, ``<timestamp>`` …) are noise. The real ask
arrives inside ``<user_query>…</user_query>`` — extract it when present.

Content-addressing is also the watermark story for live capture later: "the
set of blob ids already shipped" survives checkpoint restores (a new root
over mostly-old blobs) where a rowid watermark would lie.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
import uuid as _uuid
from pathlib import Path

HOST = "cursor"

_CHATS = Path.home() / ".cursor" / "chats"
_SCHEMA_VERSION = 1

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
# A user turn that STARTS with a well-formed <tag>…</tag> block is carrying
# Cursor context injections (<user_info>, <git_status>, <rules>,
# <agent_transcripts>, …). Tag names churn with Cursor versions, so strip
# leading blocks GENERICALLY rather than maintaining a name list; mid-text
# tags are left alone (they're the user's own content).
_LEADING_TAG_RE = re.compile(r"^<([A-Za-z_][\w-]*)(?:\s[^>]*)?>.*?</\1>\s*", re.S)


def _clean_user_text(text: str) -> str | None:
    """The user's real ask, or None when the turn is pure injected context."""
    t = (text or "").strip()
    if not t:
        return None
    m = _USER_QUERY_RE.search(t)
    if m:
        return m.group(1).strip() or None
    while True:
        stripped = _LEADING_TAG_RE.sub("", t, count=1).strip()
        if stripped == t:
            break
        t = stripped
    return t or None


def _text_of(content) -> str:
    """Join text pieces of a content value (string, or a list of
    ``{type: "text", text}`` blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _usage_of(message: dict) -> dict[str, int] | None:
    """Map persisted Cursor usage, when the host provides it.

    Cursor 2026.08's v1 chat store does not persist the CLI result's usage
    object, so real sessions currently return ``None``. Accept both the CLI's
    camelCase names and Claude-shaped snake_case names so usage begins flowing
    without another reader migration if Cursor adds it to message leaves.
    Never estimate from text: proprietary model tokenizers make that look
    precise while being wrong.
    """
    raw = message.get("usage")
    if not isinstance(raw, dict):
        raw = message.get("tokenCount")
    if not isinstance(raw, dict):
        return None

    aliases = {
        "input_tokens": ("inputTokens", "input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
        "cache_read_input_tokens": (
            "cacheReadTokens", "cache_read_tokens", "cache_read_input_tokens"
        ),
        "cache_creation_input_tokens": (
            "cacheWriteTokens", "cache_write_tokens",
            "cache_creation_input_tokens",
        ),
    }
    out: dict[str, int] = {}
    measured = False
    for target, names in aliases.items():
        value = next((raw[name] for name in names if name in raw), None)
        if value is None:
            out[target] = 0
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        out[target] = value
        measured = True
    return out if measured else None


def _read_meta_json(session_dir: Path) -> dict | None:
    p = session_dir / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_dirs() -> list[Path]:
    return [p.parent for p in _CHATS.glob("*/*/store.db")]


def list_sessions(limit: int = 20) -> list[dict]:
    """Most recent cursor sessions, newest first (by meta.json updatedAtMs)."""
    rows = []
    for d in _session_dirs():
        m = _read_meta_json(d) or {}
        rows.append({"id": d.name, "path": str(d / "store.db"),
                     "mtime": (m.get("updatedAtMs") or 0) / 1000.0,
                     "host": HOST, "cwd": m.get("cwd")})
    rows.sort(key=lambda s: s["mtime"], reverse=True)
    return rows[:limit]


def locate(ref: str) -> tuple[Path | None, str]:
    """Accept a store.db path, a session dir, ``latest``, or a session uuid."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p, ""
    if p.is_dir() and (p / "store.db").is_file():
        return p / "store.db", ""
    if "/" in ref and ref != "latest":
        return None, f"cursor store not found: {p}"
    dirs = _session_dirs()
    if not dirs:
        return None, f"no cursor sessions under {_CHATS}"
    if ref == "latest":
        newest = max(dirs, key=lambda d: (_read_meta_json(d) or {}).get("updatedAtMs", 0))
        return newest / "store.db", ""
    hits = [d for d in dirs if d.name == ref]
    if not hits:
        return None, f"no cursor session {ref!r} under {_CHATS}"
    if len(hits) > 1:
        # Same uuid under two workspace hashes would change which repo's
        # brain receives the import — refuse, never guess.
        return None, f"ambiguous session id {ref!r}: {len(hits)} matches — pass the path"
    return hits[0] / "store.db", ""


# Plausible ms-epoch window for node clocks (2017..2096). Field numbers churn
# with cursor versions, so a checkpoint node's timestamp is recognized by
# RANGE, not by field number — any varint in this window is a wall clock.
_MS_EPOCH_MIN = 1_500_000_000_000
_MS_EPOCH_MAX = 4_000_000_000_000


def _parse_node(data: bytes) -> tuple[list[str], int | None]:
    """(ordered child blob ids, node timestamp ms) of a protobuf tree node.

    Minimal TLV walk (stdlib only — no protobuf dependency): field 1
    length-delimited (tag 0x0A) with len 32 is a child hash; a varint in the
    ms-epoch window is the checkpoint's wall clock (observed as field 26 on
    cursor-agent 2026.08); everything else is skipped by wire type. Malformed
    input just yields what parsed."""
    out: list[str] = []
    ts: int | None = None
    i, n = 0, len(data)

    def varint(j: int) -> tuple[int, int]:
        v, shift = 0, 0
        while j < n:
            b = data[j]
            v |= (b & 0x7F) << shift
            j += 1
            if not b & 0x80:
                break
            shift += 7
        return v, j

    while i < n:
        tag, i = varint(i)
        wire = tag & 7
        if wire == 2:                      # length-delimited
            ln, i = varint(i)
            if tag >> 3 == 1 and ln == 32 and i + 32 <= n:
                out.append(data[i:i + 32].hex())
            i += ln
        elif wire == 0:                    # varint
            v, i = varint(i)
            if ts is None and _MS_EPOCH_MIN <= v <= _MS_EPOCH_MAX:
                ts = v
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:                              # unknown wire type — stop safely
            break
    return out, ts


def _load_messages(db_path: Path) -> list[tuple[dict, int | None]]:
    """Walk the hash tree from latestRootBlobId; return ordered JSON leaves
    paired with their nearest ancestor node's wall clock (ms epoch, or None).
    Checkpoint nodes are timestamped; their leaves inherit that clock, which
    is what turns "the whole session is one instant" into a real timeline."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        blobs = {row[0]: row[1] for row in con.execute("SELECT id, data FROM blobs")}
        root = None
        for (value,) in con.execute("SELECT value FROM meta"):
            try:
                m = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(m, dict) and m.get("latestRootBlobId"):
                root = m["latestRootBlobId"]
                break
    finally:
        con.close()

    messages: list[tuple[dict, int | None]] = []
    seen: set[str] = set()

    def walk(blob_id: str, inherited_ts: int | None) -> None:
        if blob_id in seen or blob_id not in blobs:
            return
        seen.add(blob_id)
        data = blobs[blob_id]
        if isinstance(data, str):
            data = data.encode("utf-8")
        if data[:1] == b"{":
            try:
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                return
            if isinstance(msg, dict) and msg.get("role"):
                messages.append((msg, inherited_ts))
            return
        children, node_ts = _parse_node(data)
        for child in children:
            walk(child, node_ts or inherited_ts)

    if root:
        walk(root, None)
    if not messages:
        # Fallback: no walkable root (interrupted write). Take JSON blobs in
        # insertion order — degraded but better than losing the session.
        for data in blobs.values():
            if isinstance(data, (bytes, bytearray)) and data[:1] == b"{":
                try:
                    msg = json.loads(bytes(data).decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("role"):
                    messages.append((msg, None))
    return messages


def to_canonical(path) -> tuple[list[dict], dict]:
    """Load a cursor store and transform it to Claude-shaped records."""
    db_path = Path(path)
    session_dir = db_path.parent
    mj = _read_meta_json(session_dir) or {}
    version = mj.get("schemaVersion")
    if version != _SCHEMA_VERSION:
        raise ValueError(
            f"cursor store {session_dir} has schemaVersion {version!r}; this "
            f"reader is pinned to {_SCHEMA_VERSION} — refusing to misparse. "
            "Update readers/cursor.py against the new format.")
    cwd = mj.get("cwd")
    session_id = session_dir.name

    def _iso(ms) -> str | None:
        try:
            return datetime.datetime.fromtimestamp(
                ms / 1000.0, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except (TypeError, ValueError, OSError):
            return None

    # Per-message clocks come from checkpoint NODES in the hash tree (leaves
    # carry none); meta.json's window is the fallback when a node had no
    # readable clock. The server lifts event_date from ``timestamp`` and
    # treats missing as undated.
    fallback_ts = _iso(mj.get("updatedAtMs")) or _iso(mj.get("createdAtMs"))
    ts_holder = {"ts": fallback_ts}

    def rec(record: dict) -> dict:
        if cwd:
            record["cwd"] = cwd
        # uuid is the server's per-record replay-dedup key — records without
        # one are SKIPPED by the agentic parser (imported as nothing).
        # Deterministic over (session, output index) so re-flushes fold.
        record["uuid"] = str(_uuid.uuid5(
            _uuid.NAMESPACE_URL, f"memhub:cursor:{session_id}:{len(out)}"))
        if ts_holder["ts"]:
            record["timestamp"] = ts_holder["ts"]
        return record

    def user(content) -> dict:
        return rec({"type": "user", "message": {"role": "user", "content": content}})

    def assistant(block, block_model: str | None = None) -> dict:
        message = {"role": "assistant", "content": [block]}
        if block_model or model:
            message["model"] = block_model or model
        return rec({"type": "assistant", "message": message})

    out: list[dict] = []
    dated_messages = _load_messages(db_path)

    def _model_of(obj) -> str | None:
        po = obj.get("providerOptions") if isinstance(obj, dict) else None
        return (po.get("cursor") or {}).get("modelName") if isinstance(po, dict) else None

    model = None
    for m, _ in dated_messages:
        # modelName appears at message level OR on individual content blocks
        model = _model_of(m) or model
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                model = _model_of(b) or model

    banner = "[Imported from Cursor"
    if model:
        banner += f" · model {model}"
    banner += f" · session {session_id}"
    if cwd:
        banner += f" · cwd {cwd}"
    banner += "]"
    ts_holder["ts"] = _iso(mj.get("createdAtMs")) or fallback_ts
    out.append(user(banner))

    title = None
    for msg, node_ts in dated_messages:
        ts_holder["ts"] = _iso(node_ts) or fallback_ts
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            continue  # Cursor's harness prompt — noise

        if role == "user":
            ask = _clean_user_text(_text_of(content))
            if ask:
                out.append(user(ask))
                if title is None:
                    title = ask.strip().splitlines()[0][:150]
            continue

        if role == "assistant":
            blocks = content if isinstance(content, list) else [
                {"type": "text", "text": content}]
            emitted: list[dict] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                block_model = _model_of(b) or _model_of(msg)
                if bt == "reasoning":
                    text = (b.get("text") or "").strip()
                    if text:  # signatures are opaque — text only
                        record = assistant(
                            {"type": "thinking", "thinking": text}, block_model
                        )
                        out.append(record)
                        emitted.append(record)
                elif bt == "text":
                    text = (b.get("text") or "").strip()
                    if text:
                        record = assistant({"type": "text", "text": text}, block_model)
                        out.append(record)
                        emitted.append(record)
                elif bt == "tool-call":
                    args = b.get("args")
                    record = assistant({
                        "type": "tool_use",
                        "id": b.get("toolCallId") or f"cursor-call-{len(out)}",
                        "name": b.get("toolName") or "tool",
                        "input": args if isinstance(args, dict) else {"input": args},
                    }, block_model)
                    out.append(record)
                    emitted.append(record)
            usage = _usage_of(msg)
            if emitted and usage:
                emitted[-1]["message"]["usage"] = usage
            continue

        if role == "tool":
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool-result":
                    continue
                result = b.get("result")
                if not isinstance(result, str):
                    result = _text_of(b.get("experimental_content")) or (
                        json.dumps(result) if result is not None else "")
                out.append(user([{
                    "type": "tool_result",
                    "tool_use_id": b.get("toolCallId") or f"cursor-out-{len(out)}",
                    "content": result,
                }]))

    meta = {"session_id": session_id, "cwd": cwd, "model": model,
            "title": title, "host": HOST}
    return out, meta
