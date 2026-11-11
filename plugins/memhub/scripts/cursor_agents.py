"""Cursor sub-agents: find the agents a Cursor session spawned and describe
each as an ``agent_capture.Agent`` linked to its root session.

Two Cursor surfaces spawn agents, and they leave different evidence:

**cursor-agent (the CLI).** Each sub-agent is a sibling session directory,
``~/.cursor/chats/<md5(cwd)>/<child>/store.db``, with **no** ``meta.json``.
Its store's meta row carries ``subagentInfo{parentAgentId,
rootParentAgentId, toolCallId, typeName}``; ``toolCallId`` is the spawning
call's id in the spawner's store, byte for byte (an 80-character
``call-…\\nfc_…`` with an embedded newline). The spawner's tool result ends
``Agent ID: <child>``. The CLI fires no subagentStart/subagentStop; children
fire their own tool hooks under their own id, and only the root gets a
``sessionEnd`` (proved on cursor-agent 2026.10.14 and 2026.10.16).

**The Cursor IDE (3.25).** A sub-agent's transcript is NESTED under its
parent's: ``~/.cursor/projects/<ws>/agent-transcripts/<parent>/subagents/
<child>.jsonl``. ``subagentStop`` fires under the PARENT with
``child_conversation_id``, ``subagent_id``/``tool_call_id`` (the parent's
Task call id), ``status`` and ``subagent_type``; ``agent_transcript_path`` is
null. ``state.vscdb`` holds ``composerData:<child>.subagentInfo
{parentComposerId, rootParentConversationId, toolCallId, subagentTypeName}``.
Children fire no stop/afterAgentResponse/sessionEnd; their tool hooks carry
``parent_tool_call_id``. The parent's JSONL carries no tool ids at all.

What every agent gets (``agent_capture`` sends it):

  agent_id          the child's conversation uuid
  agent_type        CLI ``typeName``; IDE ``subagent_type`` / ``subagentTypeName``
  description       the spawning Task's ``description``
  tool_use_id       the spawning call id, byte-identical, only where the
                    parent's stored records carry that same id or the host
                    named it directly (see ``tool_use_id`` below); never
                    normalised at the newline
  spawn_depth       the length of the parent chain (1 for a direct child)
  parent_agent_id   the spawner, when it is itself an agent
  status            ``turn_ended`` in the child's JSONL (success -> done;
                    error "User aborted request" -> stopped; other errors ->
                    failed), else the background notification / subagentStop
                    status, else running while the root is live, stopped once
                    it has ended
  result_preview    the child's last assistant text, else the spawner's
                    ``<response>`` block (CLI) or ``final_summary`` (IDE)
  revision          a digest of the child's canonical records

``tool_use_id``: on the CLI a direct child's id is sent only when the root's
records came from its store (``root_source == "store"``), because a root read
from its JSONL carries made-up ``cursor-call-N`` ids; a grandchild's spawner
is always read from its store, so its id is always sent. On the IDE the id
is sent when ``subagentStop.tool_call_id`` or ``state.vscdb`` named it.

SQLite is never opened in place for writing: CLI stores are COPIED (with
their -wal/-shm) into a temporary directory first, and ``state.vscdb`` is
opened read-only (``mode=ro``, falling back to ``immutable=1``).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_capture  # noqa: E402
from readers import cursor as cursor_reader  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
AGENT_ID_IN_RESULT = re.compile(r"Agent ID: ([0-9a-fA-F-]{36})")
RESPONSE_BLOCK = re.compile(r"<response>\n?(.*?)\n?</response>", re.S)
TASK_BLOCK = re.compile(r"<task>(.*?)</task>", re.S)
TASK_FIELD = re.compile(r"^(\w+):[ \t]*(.*)$", re.M)
ABORTED = "User aborted request"
# Cursor IDE writes an agent's hidden reasoning as a literal "[REDACTED]"
# paragraph inside its text; it says nothing about the result.
REDACTED_PARAGRAPH = re.compile(r"(?:^|\n+)\[REDACTED\](?=\n|$)")
# Bounds on what discovery reads: a session that spawned more than this many
# agents is captured up to the bound, oldest first, and says so in the log.
MAX_AGENTS = 64
MAX_DEPTH = 32


def records_revision(records: list[dict]) -> str:
    """A digest of canonical records — what changes when an agent's content
    does. The same digest ``cursor_flush`` keeps for a transcript source."""
    encoded = json.dumps(records, ensure_ascii=True, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _state_db() -> Path:
    """Cursor IDE's global ``state.vscdb`` for this platform."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Cursor"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Cursor"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Cursor"
    return base / "User" / "globalStorage" / "state.vscdb"


STATE_DB = _state_db()


# ── shared helpers ───────────────────────────────────────────────────────────

def turn_status(path: Path | None) -> str | None:
    """The agent status a child's hook JSONL ends with: its LAST
    ``turn_ended`` record, mapped; None when it has none yet."""
    if path is None:
        return None
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"turn_ended"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "turn_ended":
                    last = row
    except OSError:
        return None
    if last is None:
        return None
    status = str(last.get("status") or "").lower()
    if status == "success":
        return "done"
    if status == "error":
        return "stopped" if ABORTED in str(last.get("error") or "") else "failed"
    return agent_capture.normalise_status(status)


def _notification_status(word) -> str | None:
    word = str(word or "").strip().lower()
    return "done" if word == "success" else agent_capture.normalise_status(word)


def last_assistant_text(records: list[dict]) -> str | None:
    """The last assistant text block of canonical records."""
    for record in reversed(records):
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = REDACTED_PARAGRAPH.sub("", block.get("text") or "").strip()
                if text:
                    return text
    return None


def _final_summary(records: list[dict]) -> str | None:
    """The IDE's ``UpdateCurrentStep.final_summary``, the agent's own short
    account of what it did — the preview when it wrote no closing text."""
    for record in reversed(records):
        for block in (record.get("message") or {}).get("content") or []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "UpdateCurrentStep"):
                summary = (block.get("input") or {}).get("final_summary")
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()
    return None


def _first_ask(records: list[dict]) -> str | None:
    """The child's task: its first real user turn (after the banner)."""
    for record in records[1:]:
        content = (record.get("message") or {}).get("content")
        if record.get("type") == "user" and isinstance(content, str) and not record.get("isMeta"):
            return content
    return None


def _task_calls(records: list[dict]) -> list[tuple[str, dict]]:
    """(tool_use id, input) of every Task call in canonical records."""
    calls = []
    for record in records:
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "Task"):
                calls.append((block.get("id"), block.get("input") or {}))
    return calls


def _results(records: list[dict]) -> dict[str, str]:
    out = {}
    for record in records:
        content = (record.get("message") or {}).get("content")
        if record.get("type") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content")
                    out[block.get("tool_use_id")] = text if isinstance(text, str) else json.dumps(text)
    return out


def _notifications(records: list[dict]) -> dict[str, dict]:
    """Background completions the spawner was told about, by agent id."""
    out = {}
    for record in records:
        content = (record.get("message") or {}).get("content")
        if record.get("isMeta") and isinstance(content, str):
            for task in TASK_BLOCK.findall(content):
                fields = dict(TASK_FIELD.findall(task))
                if fields.get("kind") == "subagent" and fields.get("task_id"):
                    out[fields["task_id"]] = fields
    return out


def _settled(status: str | None, root_ended: bool) -> str:
    """No terminal evidence yet: running while the root is live, stopped once
    the root has ended (an agent cannot outlive its cursor-agent process)."""
    return status or ("stopped" if root_ended else "running")


# ── the CLI: sibling stores with subagentInfo ────────────────────────────────

def _chats_dir(uuid: str) -> Path | None:
    hits = [p.parent for p in cursor_reader._CHATS.glob(f"*/{uuid}/store.db")]
    return hits[0] if len(hits) == 1 else None


def _copy_store(session_dir: Path, tmp: Path) -> Path:
    """Copy one store (+ its -wal/-shm) and, for a root, its meta.json into
    ``tmp`` under the same ``<hash>/<uuid>`` layout, so a live store is read
    from a snapshot and never opened in place."""
    dest = tmp / session_dir.parent.name / session_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("store.db", "store.db-wal", "store.db-shm", "meta.json"):
        source = session_dir / name
        if source.is_file():
            shutil.copy2(source, dest / name)
    return dest / "store.db"


def cli_child_root(uuid: str) -> tuple[bool, str | None]:
    """(is a cursor-agent sub-agent, its root uuid or None). A store dir with
    no meta.json is a sub-agent even when its meta row cannot be read yet
    (the CLI holds it open): it is then never captured on its own, only with
    its root."""
    session_dir = _chats_dir(uuid)
    if session_dir is None or (session_dir / "meta.json").exists():
        return False, None
    with tempfile.TemporaryDirectory(prefix="memhub-cursor-") as tmp:
        try:
            meta = cursor_reader.store_meta(_copy_store(session_dir, Path(tmp)))
        except OSError:
            meta = {}
    info = meta.get("subagentInfo") if isinstance(meta, dict) else None
    root = info.get("rootParentAgentId") if isinstance(info, dict) else None
    return True, root if isinstance(root, str) and UUID_RE.match(root) else None


def _cli_transcript(uuid: str) -> Path | None:
    hits = list(cursor_reader._PROJECTS.glob(f"*/agent-transcripts/{uuid}/{uuid}.jsonl"))
    return hits[0] if len(hits) == 1 else None


def cli_agents(root: str, *, root_source: str | None, root_ended: bool,
               log=lambda _m: None) -> list[agent_capture.Agent]:
    """The cursor-agent sub-agents of ``root`` (any depth), oldest first."""
    root_dir = _chats_dir(root)
    if root_dir is None or not (root_dir / "meta.json").exists():
        return []
    candidates = [d for d in root_dir.parent.iterdir()
                  if d.name != root and UUID_RE.match(d.name)
                  and (d / "store.db").is_file() and not (d / "meta.json").exists()]
    if not candidates:
        return []
    agents: list[agent_capture.Agent] = []
    with tempfile.TemporaryDirectory(prefix="memhub-cursor-") as tmpname:
        tmp = Path(tmpname)
        stores = {root: _copy_store(root_dir, tmp)}
        tree: dict[str, dict] = {}
        created: dict[str, float] = {}
        for d in candidates:
            # The workspace holds every root's children: one read-only look
            # at the meta row (as the reader opens any store) skips another
            # root's child without copying it, so a long history does not
            # cost a copy of every past sub-agent on each turn. A row that
            # cannot be read in place (held open) is decided on the copy.
            peek = cursor_reader.store_meta(d / "store.db").get("subagentInfo")
            if isinstance(peek, dict) and peek.get("rootParentAgentId") != root:
                continue
            try:
                db = _copy_store(d, tmp)
            except OSError as e:
                log(f"agents: could not copy sub-agent store {d.name[:8]} ({e})")
                continue
            meta = cursor_reader.store_meta(db)
            info = meta.get("subagentInfo")
            if isinstance(info, dict) and info.get("rootParentAgentId") == root:
                tree[d.name], stores[d.name] = info, db
                created[d.name] = meta.get("createdAt") or 0
        records: dict[str, list[dict]] = {}

        def canonical(sid: str) -> list[dict]:
            if sid not in records:
                try:
                    records[sid] = cursor_reader.to_canonical(stores[sid])[0]
                except Exception as e:  # noqa: BLE001 — one bad store skips one agent
                    log(f"agents: sub-agent {sid[:8]} unreadable ({e})")
                    records[sid] = []
            return records[sid]

        def depth(sid: str) -> int | None:
            n, cur, seen = 0, sid, set()
            while cur != root:
                if cur in seen or cur not in tree or n >= MAX_DEPTH:
                    return None
                seen.add(cur)
                cur = tree[cur].get("parentAgentId")
                n += 1
            return n

        ordered = sorted(tree, key=lambda sid: created[sid])
        if len(ordered) > MAX_AGENTS:
            log(f"agents: {len(ordered)} sub-agents; capturing the first {MAX_AGENTS}")
            ordered = ordered[:MAX_AGENTS]
        for sid in ordered:
            info = tree[sid]
            child = canonical(sid)
            if not child:
                continue
            spawner = info.get("parentAgentId")
            spawner_records = canonical(spawner) if spawner in stores else []
            call_id = info.get("toolCallId")
            args = next((inp for cid, inp in _task_calls(spawner_records) if cid == call_id), {})
            result = _results(spawner_records).get(call_id) or ""
            if result and sid not in AGENT_ID_IN_RESULT.findall(result):
                log(f"agents: {sid[:8]}'s spawn result names another agent")
            note = _notifications(spawner_records).get(sid) or {}
            status = (turn_status(_cli_transcript(sid))
                      or _notification_status(note.get("status"))
                      or ("done" if result and not args.get("run_in_background")
                          and AGENT_ID_IN_RESULT.search(result) else None))
            preview = last_assistant_text(child)
            if not preview and result:
                match = RESPONSE_BLOCK.search(result)
                preview = match.group(1) if match else None
            anchored = spawner != root or root_source == "store"
            agents.append(agent_capture.Agent(
                agent_id=sid, revision=records_revision(child),
                agent_type=info.get("typeName") if isinstance(info.get("typeName"), str) else None,
                description=args.get("description") if isinstance(args.get("description"), str) else None,
                tool_use_id=call_id if anchored and isinstance(call_id, str) else None,
                spawn_depth=depth(sid),
                parent_agent_id=spawner if spawner != root else None,
                status=_settled(status, root_ended), result_preview=preview,
                load=lambda recs=child: recs, source={"surface": "cli"}))
    return agents


# ── the IDE: nested transcripts, subagentStop, state.vscdb ───────────────────

def composer_info(ids) -> dict[str, dict]:
    """``composerData:<id>.subagentInfo`` for each id that has one, read from
    ``state.vscdb`` without writing to it. {} when the database is absent or
    cannot be opened."""
    ids = [i for i in ids if isinstance(i, str) and UUID_RE.match(i)]
    if not ids or not STATE_DB.is_file():
        return {}
    con = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"{STATE_DB.resolve().as_uri()}?{query}",
                                  uri=True, timeout=2.0)
            con.execute("SELECT 1 FROM cursorDiskKV LIMIT 1")
            break
        except (sqlite3.Error, OSError, ValueError):
            if con is not None:
                con.close()
            con = None
    if con is None:
        return {}
    out = {}
    try:
        for cid in ids:
            try:
                row = con.execute("SELECT value FROM cursorDiskKV WHERE key = ?",
                                  (f"composerData:{cid}",)).fetchone()
            except sqlite3.Error:
                break
            if not row:
                continue
            try:
                value = row[0]
                data = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            info = data.get("subagentInfo") if isinstance(data, dict) else None
            if isinstance(info, dict):
                out[cid] = info
    finally:
        con.close()
    return out


def ide_child_root(uuid: str, payload: dict) -> tuple[bool, str | None]:
    """(is a Cursor IDE sub-agent, its root or None) for a hook payload. The
    IDE marks a sub-agent's own tool hooks with ``parent_tool_call_id``."""
    if not payload.get("parent_tool_call_id"):
        return False, None
    info = composer_info([uuid]).get(uuid) or {}
    root = info.get("rootParentConversationId") or info.get("parentComposerId")
    return True, root if isinstance(root, str) and UUID_RE.match(root) else None


def ide_children_dir(parent_transcript: Path) -> Path:
    return Path(parent_transcript).parent / "subagents"


def _ide_child_paths(parent_transcript: Path) -> list[Path]:
    """``<parent dir>/subagents/<uuid>.jsonl``, each resolving inside that
    directory — a symlink out of it is not a sub-agent transcript."""
    directory = ide_children_dir(parent_transcript)
    try:
        anchor = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl")):
        if not UUID_RE.match(path.stem):
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(anchor)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file():
            out.append(resolved)
    return out


def ide_agents(root: str, parent_transcript: Path, roster: dict, *,
               cwd: str | None, root_ended: bool, enrich: bool = True,
               log=lambda _m: None) -> list[agent_capture.Agent]:
    """The Cursor IDE sub-agents under ``root``'s transcript directory.

    ``roster`` is what ``subagentStop`` recorded, ``{child: {tool_call_id,
    status, subagent_type, description}}``. A child the roster does not know
    (an older Cursor, a missed hook) is still found by the directory glob and
    linked through ``state.vscdb`` when ``enrich``."""
    paths = _ide_child_paths(parent_transcript)
    if not paths:
        return []
    if len(paths) > MAX_AGENTS:
        log(f"agents: {len(paths)} sub-agents; capturing the first {MAX_AGENTS}")
        paths = sorted(paths, key=lambda p: p.stat().st_mtime)[:MAX_AGENTS]
    try:
        parent_records = cursor_reader.to_canonical(parent_transcript, session_id=root)[0]
    except Exception:  # noqa: BLE001 — descriptions only
        parent_records = []
    by_prompt = {(inp.get("prompt") or "").strip(): inp for _cid, inp in _task_calls(parent_records)
                 if isinstance(inp.get("prompt"), str)}
    unknown = [p.stem for p in paths if not (roster.get(p.stem) or {}).get("tool_call_id")]
    vscdb = composer_info(unknown) if enrich and unknown else {}
    agents = []
    for path in paths:
        child = path.stem
        hook = roster.get(child) or {}
        info = vscdb.get(child) or {}
        linked_to = info.get("rootParentConversationId") or info.get("parentComposerId")
        if linked_to and linked_to != root:
            log(f"agents: {child[:8]} sits under {root[:8]} but state.vscdb links it elsewhere")
            continue
        try:
            records = cursor_reader.to_canonical(path, session_id=child, cwd=cwd)[0]
        except Exception as e:  # noqa: BLE001 — one bad transcript skips one agent
            log(f"agents: sub-agent {child[:8]} unreadable ({e})")
            continue
        if len(records) < 2:
            continue                      # only the banner: nothing written yet
        ask = (_first_ask(records) or "").strip()
        spawn = by_prompt.get(ask) or {}
        description = hook.get("description") or spawn.get("description")
        agent_type = (hook.get("subagent_type") or info.get("subagentTypeName")
                      or spawn.get("subagent_type"))
        tool_use_id = hook.get("tool_call_id") or info.get("toolCallId")
        status = (turn_status(path) or agent_capture.normalise_status(hook.get("status")))
        spawner = info.get("parentComposerId")
        agents.append(agent_capture.Agent(
            agent_id=child, revision=records_revision(records),
            agent_type=agent_type if isinstance(agent_type, str) else None,
            description=description if isinstance(description, str) else None,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
            # Nested IDE sub-agents are unobserved: depth 1 is known only for
            # a child its root spawned (or one nothing says otherwise of).
            spawn_depth=1 if spawner in (None, root) else None,
            parent_agent_id=spawner if isinstance(spawner, str) and spawner != root else None,
            status=_settled(status, root_ended),
            result_preview=last_assistant_text(records) or _final_summary(records),
            load=lambda recs=records: recs, source={"surface": "ide", "path": str(path)}))
    return agents


def roster_entry(payload: dict) -> tuple[str, dict] | None:
    """(child uuid, what to remember) from a ``subagentStop`` payload, or None
    when it names no child. Cursor 3.25 sends ``child_conversation_id``; its
    documented ``subagent_id`` is the spawning call id, not the child."""
    child = payload.get("child_conversation_id")
    if not isinstance(child, str) or not UUID_RE.match(child):
        return None
    entry = {}
    call = payload.get("tool_call_id") or payload.get("subagent_id")
    if agent_capture.tool_use_id_ok(call):
        entry["tool_call_id"] = call
    for key, cap in (("status", 40), ("subagent_type", 100), ("description", 1000)):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            entry[key] = value[:cap]
    return child, entry
