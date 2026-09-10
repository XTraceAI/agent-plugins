"""Cursor session reader: native stores/transcripts -> Claude Code records.

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

Cursor IDE 3.17 also writes hook transcripts at
``~/.cursor/projects/<project>/agent-transcripts/<uuid>/<uuid>.jsonl``. Those
JSONL records preserve user/assistant text and tool calls but omit tool
results, model, cwd, and usage. Hook payload metadata fills the latter fields
during live capture; manual imports remain valid with nullable metadata.

**Timestamps are real or absent — never synthesized.** The server persists
``timestamp`` as the row's ``event_date``, whose contract (MemHub's
claude_parts, ENG-675b) is "the turn's clock at its source; NULL means
unmeasured". A record therefore gets a ``timestamp`` only from a clock the
artifact actually carries for it: a user turn's embedded ``<timestamp>`` tag,
a store leaf's ancestor checkpoint clock, or meta.json's ``createdAtMs`` on
the synthetic banner. Session-level fallbacks (``updatedAtMs``, file ctime)
used to be stamped on every otherwise-undated record; those are flush-adjacent
wall clocks, so a whole session collapsed onto one or two ``event_date``
values — measured on production as 44 turns sharing a single instant. Live
capture fills the gaps with per-record first-seen clocks instead
(``cursor_flush._stamp_records``); backfills honestly leave them NULL.
"""
from __future__ import annotations

import datetime
import json
import hashlib
import re
import sqlite3
import sys
import uuid as _uuid
from pathlib import Path
from .strict_json import loads as load_json

# See the same note in ``readers/codex.py`` — shared title rules, stdlib only.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from session_title import normalize_title  # noqa: E402

HOST = "cursor"

_CHATS = Path.home() / ".cursor" / "chats"
_PROJECTS = Path.home() / ".cursor" / "projects"
_SCHEMA_VERSION = 1

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
# A user turn that STARTS with a well-formed <tag>…</tag> block is carrying
# Cursor context injections (<user_info>, <git_status>, <rules>,
# <agent_transcripts>, …). Tag names churn with Cursor versions, so strip
# leading blocks GENERICALLY rather than maintaining a name list; mid-text
# tags are left alone (they're the user's own content).
_LEADING_TAG_RE = re.compile(r"^<([A-Za-z_][\w-]*)(?:\s[^>]*)?>.*?</\1>\s*", re.S)
_TIMESTAMP_RE = re.compile(
    r"<timestamp>([A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4}, "
    r"\d{1,2}:\d{2} [AP]M) \(UTC([+-]\d{1,2})(?::(\d{2}))?\)</timestamp>")


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


def normalize_usage(raw) -> dict[str, int] | None:
    """Normalize exact native counters, or return ``None`` when unmeasured.

    Accept store/CLI camelCase, hook snake_case, and canonical names. Missing
    buckets become measured zero only when at least one exact counter exists.
    A malformed present value invalidates the whole sample; partially trusting
    one bucket would make the aggregate look more complete than it is.
    """
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


def _usage_of(message: dict) -> dict[str, int] | None:
    """Map persisted Cursor usage, when the host provides it."""
    raw = message.get("usage")
    if not isinstance(raw, dict):
        raw = message.get("tokenCount")
    return normalize_usage(raw)


def _read_meta_json(session_dir: Path, *, strict_json=False) -> dict | None:
    p = session_dir / "meta.json"
    try:
        value = load_json(p.read_text(encoding="utf-8"), strict=strict_json)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        if strict_json:
            raise
        return None


def _session_dirs() -> list[Path]:
    return [p.parent for p in _CHATS.glob("*/*/store.db")]


def _transcript_paths() -> list[Path]:
    return list(_PROJECTS.glob("*/agent-transcripts/*/*.jsonl"))


def session_cwd(path) -> str | None:
    """The directory this session is running in, from the session dir's
    ``meta.json`` — the same key ``list_sessions`` already surfaces. A
    transcript-only session (no ``store.db``) has no meta.json and no cwd."""
    p = Path(path)
    meta = _read_meta_json(p if p.is_dir() else p.parent) or {}
    cwd = meta.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def list_sessions(limit: int | None = 20, *, on_error=None) -> list[dict]:
    """Most recent Cursor sessions, preferring the richer store per UUID."""
    rows: list[dict] = []
    store_ids: set[str] = set()
    if on_error is None:
        stores, transcripts = _session_dirs(), _transcript_paths()
    else:
        from .discovery import paths
        # Either layout may legitimately be absent; both absent is incomplete.
        roots = []
        for root in (_CHATS, _PROJECTS):
            try:
                root.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                on_error(error)
            else:
                roots.append(root)
        if not roots:
            on_error(FileNotFoundError("Cursor session stores are unavailable"))
        stores = [path.parent for path in paths(_CHATS, ("*", "*", "store.db"), on_error)] if _CHATS in roots else []
        transcripts = paths(_PROJECTS, ("*", "agent-transcripts", "*", "*.jsonl"), on_error) if _PROJECTS in roots else []
    for d in stores:
        if on_error is not None:
            # Discovery uses filesystem observations. Bad session metadata is
            # diagnosed per session by the CLI, without hiding healthy peers.
            try:
                mtime = (d / "store.db").stat().st_mtime
            except OSError as error:
                on_error(error)
                continue
            store_ids.add(d.name)
            rows.append({"id": d.name, "path": str(d / "store.db"),
                         "mtime": mtime, "host": HOST, "cwd": None})
            continue
        m = _read_meta_json(d) or {}
        store_ids.add(d.name)
        rows.append({"id": d.name, "path": str(d / "store.db"),
                     "mtime": (m.get("updatedAtMs") or 0) / 1000.0,
                     "host": HOST, "cwd": m.get("cwd")})
    for p in transcripts:
        if p.stem in store_ids:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError as error:
            if on_error is not None:
                on_error(error)
            continue
        rows.append({"id": p.stem, "path": str(p), "mtime": mtime,
                     "host": HOST, "cwd": None})
    rows.sort(key=lambda s: s["mtime"], reverse=True)
    return rows[:limit]


def locate(ref: str) -> tuple[Path | None, str]:
    """Accept a native path, session directory, ``latest``, or session UUID."""
    p = Path(ref).expanduser()
    if p.is_file():
        return p, ""
    if p.is_dir() and (p / "store.db").is_file():
        return p / "store.db", ""
    if p.is_dir() and (p / f"{p.name}.jsonl").is_file():
        return p / f"{p.name}.jsonl", ""
    if "/" in ref and ref != "latest":
        return None, f"cursor session not found: {p}"
    dirs = _session_dirs()
    transcripts = _transcript_paths()
    if not dirs and not transcripts:
        return None, f"no cursor sessions under {_CHATS} or {_PROJECTS}"
    if ref == "latest":
        candidates = [
            ((m.get("updatedAtMs") or 0) / 1000.0, d / "store.db")
            for d in dirs for m in [(_read_meta_json(d) or {})]
        ]
        for transcript in transcripts:
            try:
                candidates.append((transcript.stat().st_mtime, transcript))
            except OSError:
                continue
        return max(candidates, key=lambda item: item[0])[1], ""
    hits = [d for d in dirs if d.name == ref]
    if len(hits) > 1:
        # Same uuid under two workspace hashes would change which repo's
        # brain receives the import — refuse, never guess.
        return None, f"ambiguous session id {ref!r}: {len(hits)} matches — pass the path"
    if hits:
        return hits[0] / "store.db", ""
    transcript_hits = [path for path in transcripts if path.stem == ref]
    if len(transcript_hits) > 1:
        return None, (f"ambiguous session id {ref!r}: "
                      f"{len(transcript_hits)} transcripts — pass the path")
    if transcript_hits:
        return transcript_hits[0], ""
    return None, f"no cursor session {ref!r} under {_CHATS} or {_PROJECTS}"


# Plausible ms-epoch window for node clocks (2017..2096). Field numbers churn
# with cursor versions, so a checkpoint node's timestamp is recognized by
# RANGE, not by field number — any varint in this window is a wall clock.
_MS_EPOCH_MIN = 1_500_000_000_000
_MS_EPOCH_MAX = 4_000_000_000_000


def _parse_node(data: bytes, *, strict: bool = False) -> tuple[list[str], int | None]:
    """(ordered child blob ids, node timestamp ms) of a protobuf tree node.

    Minimal TLV walk (stdlib only — no protobuf dependency): field 1
    length-delimited (tag 0x0A) with len 32 is a child hash; a varint in the
    ms-epoch window is the checkpoint's wall clock (observed as field 26 on
    cursor-agent 2026.08); everything else is skipped by wire type. Malformed
    input just yields what parsed unless strict validation is requested."""
    out: list[str] = []
    ts: int | None = None
    i, n = 0, len(data)

    def varint(j: int) -> tuple[int, int]:
        v, shift = 0, 0
        while j < n:
            b = data[j]
            if strict and (shift > 63 or (shift == 63 and b > 1)):
                raise ValueError("invalid protobuf varint")
            v |= (b & 0x7F) << shift
            j += 1
            if not b & 0x80:
                return v, j
            shift += 7
        if strict:
            raise ValueError("truncated protobuf varint")
        return v, j

    while i < n:
        tag, i = varint(i)
        if strict and not 1 <= tag >> 3 <= (1 << 29) - 1:
            raise ValueError("invalid protobuf field")
        wire = tag & 7
        if wire == 2:                      # length-delimited
            ln, i = varint(i)
            if strict and (i + ln > n or (tag >> 3 == 1 and ln != 32)):
                raise ValueError("invalid protobuf child or field length")
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
            if strict:
                raise ValueError("unsupported protobuf wire type")
            break
        if strict and i > n:
            raise ValueError("truncated protobuf fixed field")
    return out, ts


def _validate_message(message):
    if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
        raise ValueError("Cursor source has no supported message role")
    content = message.get("content")
    if not isinstance(content, (str, list)) or (isinstance(content, list)
            and any(not isinstance(block, dict) for block in content)):
        raise ValueError("Cursor message has invalid content")
    role = message["role"]
    if role == "tool" and not isinstance(content, list):
        raise ValueError("Cursor tool message has invalid result blocks")
    if role not in ("assistant", "tool") or isinstance(content, str):
        return
    for block in content:
        kind = block.get("type")
        allowed = ("reasoning", "text", "tool-call", "tool_use") if role == "assistant" else ("tool-result",)
        if kind not in allowed:
            raise ValueError("Cursor message has unsupported content block")
        if kind in ("text", "reasoning") and block.get("text") is not None and not isinstance(block["text"], str):
            raise ValueError("Cursor message text must be a string")
        if kind in ("tool-call", "tool_use", "tool-result"):
            for key in ("toolCallId", "id", "toolName", "name"):
                if block.get(key) is not None and not isinstance(block[key], str):
                    raise ValueError("Cursor tool identity must be a string")


def _load_messages(db_path: Path, *, strict_utf8: bool = False, strict_json: bool = False) -> list[tuple[dict, int | None]]:
    """Walk the hash tree from latestRootBlobId; return ordered JSON leaves
    paired with their nearest ancestor node's wall clock (ms epoch, or None).
    Checkpoint nodes are timestamped; their leaves inherit that clock, which
    is what turns "the whole session is one instant" into a real timeline."""
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        blobs = {row[0]: row[1] for row in con.execute("SELECT id, data FROM blobs")}
        root = None
        for (value,) in con.execute("SELECT value FROM meta"):
            try:
                m = load_json(value, strict=strict_json)
            except (TypeError, json.JSONDecodeError):
                if strict_json:
                    raise
                continue
            if isinstance(m, dict) and m.get("latestRootBlobId"):
                root = m["latestRootBlobId"]
                break
    finally:
        con.close()

    messages: list[tuple[dict, int | None]] = []
    seen: set[str] = set()
    active: set[str] = set()

    def walk(blob_id: str, inherited_ts: int | None) -> None:
        if not isinstance(blob_id, str) or blob_id not in blobs:
            if strict_json:
                raise ValueError("Cursor tree references a missing blob")
            return
        if blob_id in active and strict_json:
            raise ValueError("Cursor tree contains a cycle")
        if blob_id in seen:
            return
        seen.add(blob_id)
        data = blobs[blob_id]
        if isinstance(data, str):
            data = data.encode("utf-8")
        if strict_json and hashlib.sha256(data).hexdigest() != blob_id:
            raise ValueError("Cursor blob content does not match its hash")
        if data[:1] == b"{":
            try:
                msg = load_json(data.decode("utf-8", errors="strict" if strict_utf8 else "replace"), strict=strict_json)
            except json.JSONDecodeError:
                if strict_json:
                    raise
                return
            if strict_json:
                _validate_message(msg)
            if isinstance(msg, dict) and msg.get("role"):
                messages.append((msg, inherited_ts))
            return
        children, node_ts = _parse_node(data, strict=strict_json)
        active.add(blob_id)
        try:
            for child in children:
                walk(child, node_ts or inherited_ts)
        finally:
            active.remove(blob_id)

    if root:
        walk(root, None)
    elif strict_json and blobs:
        raise ValueError("Cursor tree has no root")
    if not messages and not strict_json:
        # Fallback: no walkable root (interrupted write). Take JSON blobs in
        # insertion order — degraded but better than losing the session.
        for data in blobs.values():
            if isinstance(data, (bytes, bytearray)) and data[:1] == b"{":
                try:
                    msg = json.loads(bytes(data).decode("utf-8", errors="strict" if strict_utf8 else "replace"))
                except json.JSONDecodeError:
                    if strict_json:
                        raise
                    continue
                if isinstance(msg, dict) and msg.get("role"):
                    messages.append((msg, None))
    return messages


def _iso_ms(ms) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(
            ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OSError):
        return None


def _embedded_timestamp(text: str) -> str | None:
    match = _TIMESTAMP_RE.search(text or "")
    if not match:
        return None
    try:
        local = datetime.datetime.strptime(match.group(1),
                                           "%A, %b %d, %Y, %I:%M %p")
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        sign = -1 if hours < 0 else 1
        offset = datetime.timedelta(hours=hours, minutes=sign * minutes)
        aware = local.replace(tzinfo=datetime.timezone(offset))
        return aware.astimezone(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
    except (TypeError, ValueError):
        return None


def _model_of(obj) -> str | None:
    po = obj.get("providerOptions") if isinstance(obj, dict) else None
    return (po.get("cursor") or {}).get("modelName") if isinstance(po, dict) else None


def _canonicalize(dated_messages: list[tuple[dict, str | None]], *,
                  session_id: str, cwd: str | None, model_hint: str | None,
                  created_ts: str | None) -> tuple[list[dict], dict]:
    """Transform either native source after it has yielded ordered messages.

    Each message's ``ts`` is a clock the artifact carries FOR IT (or None —
    see the module docstring); no session-level value fills the gaps here.
    """
    ts_holder: dict = {"ts": None}
    out: list[dict] = []

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

    model = model_hint
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
    ts_holder["ts"] = created_ts
    out.append(user(banner))

    title = None
    for msg, message_ts in dated_messages:
        ts_holder["ts"] = message_ts
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            continue  # Cursor's harness prompt — noise

        if role == "user":
            ask = _clean_user_text(_text_of(content))
            if ask:
                out.append(user(ask))
                if title is None:
                    # Cursor exposes no host-generated name anywhere in its
                    # artifacts, so the first ask is all there is — but it gets
                    # the same shaping as every other derived title instead of
                    # a ragged mid-word cut.
                    title = normalize_title(ask)
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
                elif bt in ("tool-call", "tool_use"):
                    args = b.get("args") if bt == "tool-call" else b.get("input")
                    record = assistant({
                        "type": "tool_use",
                        "id": (b.get("toolCallId") or b.get("id") or
                               f"cursor-call-{len(out)}"),
                        "name": b.get("toolName") or b.get("name") or "tool",
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


_MAX_TRANSCRIPT_LINE_BYTES = 8 * 1024 * 1024


def _load_transcript(path: Path, *, strict_utf8: bool = False, strict_json: bool = False) -> list[tuple[dict, str | None]]:
    """Read Cursor hook JSONL, ignoring only an unfinished final line.

    A message's clock is its OWN embedded ``<timestamp>`` tag (user turns
    carry one inside Cursor's context injection) or None. It is deliberately
    NOT carried forward to the following assistant/tool messages and not
    defaulted from file times: both collapse a whole span of turns onto one
    repeated value, which is exactly the degenerate ``event_date`` shape this
    reader must never produce. Live capture dates the undated records by
    first-seen hook clock instead; backfills leave them unmeasured.
    """
    messages: list[tuple[dict, str | None]] = []
    with path.open("rb") as handle:
        line_no = 0
        while True:
            raw = handle.readline(_MAX_TRANSCRIPT_LINE_BYTES + 1)
            if not raw:
                break
            line_no += 1
            if len(raw) > _MAX_TRANSCRIPT_LINE_BYTES:
                raise ValueError(
                    f"cursor transcript {path} line {line_no} exceeds 8 MiB")
            terminated = raw.endswith((b"\n", b"\r"))
            try:
                # JSON-only validation permits replacement decoding. Leaving
                # both flags off retains legacy unfinished-byte-tail handling.
                errors = "replace" if strict_json and not strict_utf8 else "strict"
                entry = load_json(raw.decode("utf-8", errors=errors), strict=strict_json)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if not terminated and not (strict_utf8 and isinstance(exc, UnicodeDecodeError)):
                    # Cursor appends records. A hook can race the writer, so a
                    # genuinely unfinished tail is deferred to the next event.
                    # A complete final JSON object needs no trailing newline
                    # and is accepted by the same parse above.
                    # Strict consumers defer incomplete JSON, never bad UTF-8.
                    break
                raise ValueError(
                    f"cursor transcript {path} line {line_no} is invalid JSON") from exc
            if not isinstance(entry, dict):
                if strict_json:
                    raise ValueError("Cursor transcript row is not an object")
                continue
            if entry.get("role") not in (
                    "system", "user", "assistant", "tool"):
                continue
            body = entry.get("message")
            if not isinstance(body, dict):
                if strict_json:
                    raise ValueError("Cursor transcript message is not an object")
                continue
            message = dict(body)
            message["role"] = entry["role"]
            if strict_json:
                _validate_message(message)
            own_ts = (_embedded_timestamp(_text_of(message.get("content")))
                      if entry["role"] == "user" else None)
            messages.append((message, own_ts))
    return messages


def to_canonical(path, *, session_id: str | None = None,
                 cwd: str | None = None, model: str | None = None,
                 strict_utf8: bool = False, strict_json: bool = False
                 ) -> tuple[list[dict], dict]:
    """Load either a legacy ``store.db`` or current hook transcript."""
    source = Path(path)
    if source.name != "store.db":
        sid = session_id or source.stem
        messages = _load_transcript(source, strict_utf8=strict_utf8, strict_json=strict_json)
        # The banner's clock: the first embedded user-turn tag — the earliest
        # source-carried instant the transcript offers (None when it offers
        # none; the flush's first-seen stamp covers live sessions).
        created_ts = next((ts for _, ts in messages if ts), None)
        return _canonicalize(
            messages, session_id=sid, cwd=cwd, model_hint=model,
            created_ts=created_ts)

    session_dir = source.parent
    mj = _read_meta_json(session_dir, strict_json=strict_json) or {}
    version = mj.get("schemaVersion")
    if version != _SCHEMA_VERSION:
        raise ValueError(
            f"cursor store {session_dir} has schemaVersion {version!r}; this "
            f"reader is pinned to {_SCHEMA_VERSION} — refusing to misparse. "
            "Update readers/cursor.py against the new format.")
    store_cwd = mj.get("cwd")
    # A leaf's clock is its nearest ancestor checkpoint node's — per-checkpoint
    # real time, or None where no ancestor carried one. meta.json's
    # ``updatedAtMs`` is NOT a substitute: it moves with every write, so using
    # it dates the whole undated remainder at flush-adjacent time.
    messages = [(message, _iso_ms(node_ts))
                for message, node_ts in _load_messages(source, strict_utf8=strict_utf8, strict_json=strict_json)]
    return _canonicalize(
        messages, session_id=session_dir.name, cwd=store_cwd,
        model_hint=None, created_ts=_iso_ms(mj.get("createdAtMs")))


def session_metadata(path) -> dict:
    """Native identity and start; a first user message is not a session start."""
    source = Path(path)
    if source.name == "store.db":
        with (source.parent / "meta.json").open(encoding="utf-8") as handle:
            meta = json.load(handle)
        if not isinstance(meta, dict) or meta.get("schemaVersion") != _SCHEMA_VERSION:
            raise ValueError("unsupported Cursor store metadata")
        created = meta.get("createdAtMs")
        return {"session_id": source.parent.name, "cwd": meta.get("cwd"),
                "git_branch": meta.get("gitBranch"),
                "started_at": _iso_ms(created) if type(created) in (int, float) else None,
                "source_surface": meta.get("source_surface")}
    # This observed location identifies IDE transcripts. An arbitrary file does
    # not establish a CLI or IDE surface, and absent native start stays unknown.
    try:
        relative = source.resolve().relative_to(_PROJECTS.resolve())
        known_ide = len(relative.parts) == 4 and relative.parts[1] == "agent-transcripts"
    except ValueError:
        known_ide = False
    return {"session_id": source.stem, "cwd": None, "git_branch": None,
            "started_at": None, "source_surface": "cursor-ide" if known_ide else None}
