"""Codex sub-agents: the threads a person's Codex thread spawned, found on disk
and described for ``agent_capture`` (one shape for Codex CLI and the Codex
desktop app, which share ``$CODEX_HOME`` and the rollout format).

MemHub-Backend docs/specs/agent-sessions-spec.md defines the wire; the
agent-capture parity plan §4.3 holds the evidence each rule below rests on.

Discovery
---------
Multi-agent v2 (Codex CLI 0.154, desktop app-server 0.162/0.163):

* The root rollout logs each spawn as ``event_msg item_completed`` whose
  ``item.type == "SubAgentActivity"`` and ``kind == "started"``, carrying
  ``id`` (the ``spawn_agent`` call id) and ``agent_thread_id``. Other kinds
  (``interacted``, ``completed``, ``interrupted``) refer to a thread already
  started; a child's own rollout logs them about its parent too, so only
  ``started`` names a thread this rollout spawned.
* The child's rollout (``sessions/`` or ``archived_sessions/``, ``.jsonl`` or
  ``.jsonl.zst``) must confirm the link: its header's
  ``source.subagent.thread_spawn.parent_thread_id`` is the spawning thread.
  An unconfirmed child is skipped, never guessed.
* A child's own ``started`` activity spawns grandchildren; they are walked
  the same way (depth >= 2 is designed, not yet seen live). Every agent is
  linked to the ROOT conversation; ``parent_agent_id`` carries the chain.
* A /review thread (``source.subagent == "review"`` with a top-level
  ``parent_thread_id``) is logged in no parent activity. It is found by its
  header among the plain rollouts written since the root began, and is
  attached as an agent of type ``review`` with nothing stripped (D-E).
* Guardian reviews, memory consolidation and ``subagent`` threads that name
  no parent are never agents of anyone and stay uncaptured (D-E).
* Multi-agent v1 (``collab_agent_spawn_end``) is reachable on some models but
  has never been seen on disk. A root that spawns without any v2 activity is
  logged as ``unknown multi-agent shape`` and its agents are skipped (D-F).

Fork copy
---------
A child spawned with ``fork_turns`` begins with a copy of its parent's
history: rows with ``ordinal < subagent_history_start_ordinal`` (and a second
``session_meta``, the parent's). They are dropped, the child's own header
kept, so nothing is counted twice. The rule runs only on a fork
(``codex_history.is_fork``): a /review thread sets the same ordinal to its
own length while copying nothing.

Fields
------
``agent_id`` the child's thread uuid (never ``agent_path``, which the backend
refuses); ``agent_type`` its ``agent_role``, else ``default`` (``review`` for
/review); ``description`` the path leaf and nickname (``alpha (Feynman)``) —
Codex encrypts the task text, so there is nothing richer to send;
``tool_use_id`` the spawn call id; ``spawn_depth`` the header's depth;
``revision`` the child's decompressed byte size.

Status comes from the child's LAST lifecycle event after stripping (the copied
prefix holds the parent's own ``task_started``/``turn_aborted``):
``task_complete`` with an ``error`` -> failed; ``task_complete`` -> done;
``turn_aborted`` -> stopped; ``task_started`` (a turn in progress) or nothing
-> running while the child or the root was written to within
``LIVE_WINDOW_S``, else stopped. A parent activity ``interrupted`` makes any
child that did not finish stopped.

The preview is the newest non-empty ``task_complete.last_agent_message``, else
the parent's last FINAL_ANSWER envelope from that child with its headers
stripped. ``agent_capture.import_arguments`` redacts and clips it.

Stdlib only. Reads, never writes: nothing here touches plugin state.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_capture  # noqa: E402
from readers import codex as codex_reader  # noqa: E402
from readers import codex_history  # noqa: E402

DEFAULT_ROLE = "default"            # codex-rs core/src/agent/role.rs DEFAULT_ROLE_NAME
REVIEW_TYPE = "review"
MAX_DEPTH = 8
#: A child with no finished turn is "running" while it, or the root, was
#: written to this recently; older than that it was cut off (an interrupted
#: child fires no SubagentStop and may never write a terminal event).
LIVE_WINDOW_S = 15 * 60
#: Bounds on the /review header scan.
REVIEW_SCAN_MAX = 64
#: Codex's own thread kinds that are nobody's agent (D-E).
NOT_AGENTS = ("guardian_review", "memory_consolidation")
_LIFECYCLE = ("task_started", "task_complete", "turn_aborted")
UNKNOWN_SHAPE = "unknown multi-agent shape"


@dataclass
class Discovery:
    """What ``discover`` found. ``agents`` are ``agent_capture.Agent`` with a
    ``load`` that returns canonical records; ``skipped`` says why each other
    candidate was left out; ``unknown_shape`` is set for a v1 spawn (D-F)."""
    root_id: str | None = None
    agents: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    unknown_shape: bool = False


def _payload(row) -> dict:
    p = row.get("payload") if isinstance(row, dict) else None
    return p if isinstance(p, dict) else {}


def _header(rows) -> dict:
    return _payload(rows[0]) if rows and rows[0].get("type") == "session_meta" else {}


def spawned(rows) -> list[dict]:
    """The threads this rollout spawned, in spawn order:
    ``{thread_id, call_id, agent_path, kinds}``."""
    seen: dict[str, dict] = {}
    for row in rows:
        p = _payload(row)
        if row.get("type") != "event_msg" or p.get("type") != "item_completed":
            continue
        item = p.get("item")
        if not isinstance(item, dict) or item.get("type") != "SubAgentActivity":
            continue
        tid = item.get("agent_thread_id")
        if not isinstance(tid, str) or not tid:
            continue
        entry = seen.setdefault(tid, {"thread_id": tid, "call_id": None,
                                      "agent_path": item.get("agent_path"), "kinds": []})
        kind = str(item.get("kind") or "").lower()
        entry["kinds"].append(kind)
        if kind == "started" and isinstance(item.get("id"), str) and entry["call_id"] is None:
            entry["call_id"] = item["id"]
    return [e for e in seen.values() if "started" in e["kinds"]]


def v1_spawn_seen(rows) -> bool:
    """A multi-agent v1 spawn: its end event, or a spawn_agent call with no v2
    activity to show for it."""
    calls = False
    for row in rows:
        p = _payload(row)
        if row.get("type") == "event_msg" and p.get("type") == "collab_agent_spawn_end":
            return True
        if (row.get("type") == "response_item" and p.get("type") == "function_call"
                and p.get("name") == "spawn_agent"):
            calls = True
    return calls and not spawned(rows)


def strip_fork(rows) -> tuple[list, int]:
    """A child's own rows: the copied parent prefix dropped, the child's
    header kept. Non-fork threads come back unchanged."""
    if not rows or not codex_history.is_fork(rows):
        return rows, 0
    start = _header(rows).get("subagent_history_start_ordinal")
    if type(start) is not int or start <= 0:
        return rows, 0
    kept = [rows[0]]
    for index, row in enumerate(rows[1:], start=1):
        ordinal = row.get("ordinal")
        if (ordinal if type(ordinal) is int else index) >= start:
            kept.append(row)
    return kept, len(rows) - len(kept)


def status_of(rows, *, live: bool, parent_kinds=()) -> str:
    """running | done | failed | stopped from a child's own (stripped) rows."""
    last = None
    for row in rows:
        p = _payload(row)
        if row.get("type") == "event_msg" and p.get("type") in _LIFECYCLE:
            last = p
    if last is not None and last.get("type") == "task_complete":
        return "failed" if last.get("error") else "done"
    if "interrupted" in parent_kinds:
        return "stopped"
    if last is not None and last.get("type") == "turn_aborted":
        return "stopped"
    return "running" if live else "stopped"


def preview_of(rows, parent_rows=(), agent_path: str | None = None) -> str | None:
    for row in reversed(rows):
        p = _payload(row)
        if row.get("type") == "event_msg" and p.get("type") == "task_complete":
            msg = p.get("last_agent_message")
            if isinstance(msg, str) and msg.strip():
                return msg
    if agent_path:
        for row in reversed(list(parent_rows)):
            p = _payload(row)
            if (row.get("type") == "response_item" and p.get("type") == "agent_message"
                    and p.get("author") == agent_path):
                text = codex_reader.agent_envelope_text(p)
                if codex_reader.envelope_type(text) == "FINAL_ANSWER":
                    payload = codex_reader.envelope_payload(text)
                    if payload:
                        return payload
    return None


def _description(link: dict, fallback_path=None) -> str | None:
    if link["kind"] == "review":
        return "review"
    path = link.get("agent_path") or fallback_path or ""
    leaf = path.rsplit("/", 1)[-1] if isinstance(path, str) else ""
    nick = link.get("nickname")
    text = " ".join(x for x in (leaf, f"({nick})" if nick else "") if x)
    return text or None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _load_child(path: Path):
    """``load`` for one agent: canonical records of the stripped child."""
    def load() -> list:
        rows, _ = strip_fork(codex_reader.load_rollout(path))
        records, _meta = codex_reader.rollout_to_claude_records(rows)
        return records
    return load


def _started_at(meta: dict) -> float:
    """The root's native start time (header ``timestamp``), else 0."""
    import datetime  # noqa: PLC0415
    value = meta.get("timestamp")
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _review_children(root_id: str, started: float, index: dict, taken: set) -> list:
    """/review threads of ``root_id``: plain rollouts written since the root
    began whose header names it, newest first, at most ``REVIEW_SCAN_MAX``
    headers read. Compressed rollouts are not opened here — a review of a
    live root is a live, uncompressed file."""
    found = []
    scanned = 0
    candidates = sorted(((p, _mtime(p)) for sid, p in index.items()
                         if sid not in taken and sid != root_id
                         and not p.name.endswith(".zst")),
                        key=lambda item: item[1], reverse=True)
    for path, mtime in candidates:
        if mtime < started - 60 or scanned >= REVIEW_SCAN_MAX:
            break
        scanned += 1
        try:
            meta = codex_reader._session_meta([codex_reader._session_header(path, strict=False)])
        except (OSError, ValueError):
            continue
        link = codex_reader.spawn_link(meta)
        if link and link["kind"] == "review" and link["parent"] == root_id:
            found.append((path, meta, link))
    return found


def discover(root_path: Path, *, rows=None, now: float | None = None,
             log=lambda _m: None, index: dict | None = None) -> Discovery:
    """Every agent the root rollout at ``root_path`` spawned (``rows``: the
    root already loaded). Never raises for one bad child: it is skipped with
    a reason."""
    now = time.time() if now is None else now
    root_path = Path(root_path)
    rows = codex_reader.load_rollout(root_path) if rows is None else rows
    meta = _header(rows)
    root_id = meta.get("id") if isinstance(meta.get("id"), str) else None
    out = Discovery(root_id=root_id)
    if not root_id:
        return out
    first = spawned(rows)
    if not first and v1_spawn_seen(rows):
        out.unknown_shape = True
        log(f"codex-agents: {UNKNOWN_SHAPE} in codex-{root_id[:8]} "
            "(a spawn with no SubAgentActivity) — its agents are skipped")
    root_mtime = _mtime(root_path)
    lazy = {"index": index}

    def where(tid: str) -> Path | None:
        if lazy["index"] is None:
            lazy["index"] = codex_reader.rollout_index()
        return lazy["index"].get(tid)

    queue = [(root_id, None, rows, entry) for entry in first]
    seen = {root_id}
    while queue:
        parent_id, parent_agent, parent_rows, entry = queue.pop(0)
        tid = entry["thread_id"]
        if tid in seen:
            continue
        seen.add(tid)
        path = where(tid)
        if path is None:
            out.skipped.append((tid, "rollout not found"))
            continue
        try:
            child_rows = codex_reader.load_rollout(path)
        except (OSError, ValueError) as exc:
            out.skipped.append((tid, f"unreadable: {type(exc).__name__}"))
            continue
        child_meta = _header(child_rows)
        link = codex_reader.spawn_link(child_meta)
        if child_meta.get("thread_source") in NOT_AGENTS:
            out.skipped.append((tid, f"thread_source={child_meta.get('thread_source')}"))
            continue
        if not link or link["kind"] != "thread_spawn" or link["parent"] != parent_id:
            out.skipped.append((tid, f"header does not name {parent_id[:8]} as its parent"))
            continue
        depth = link["depth"] if link["depth"] is not None else (
            1 if parent_agent is None else None)
        if depth is not None and depth > MAX_DEPTH:
            out.skipped.append((tid, f"depth {depth} > {MAX_DEPTH}"))
            continue
        own, _dropped = strip_fork(child_rows)
        live = now - max(_mtime(path), root_mtime) < LIVE_WINDOW_S
        agent_path = link.get("agent_path") or entry.get("agent_path")
        try:
            revision = str(codex_reader.rollout_size(path))
        except (OSError, ValueError) as exc:
            out.skipped.append((tid, f"unsized: {type(exc).__name__}"))
            continue
        out.agents.append(agent_capture.Agent(
            agent_id=tid, revision=revision,
            agent_type=link.get("role") or DEFAULT_ROLE,
            description=_description(link, entry.get("agent_path")),
            tool_use_id=entry.get("call_id"),
            spawn_depth=depth,
            parent_agent_id=parent_agent,
            status=status_of(own, live=live, parent_kinds=entry.get("kinds") or ()),
            result_preview=preview_of(own, parent_rows, agent_path),
            load=_load_child(path),
            source={"path": str(path), "kind": "thread_spawn"}))
        queue.extend((tid, tid, child_rows, grand) for grand in spawned(child_rows))

    # A /review of a compressed (finished) root would itself be compressed by
    # now and is not looked for; a live root's reviews are plain files.
    reviews = []
    if not root_path.name.endswith(".zst"):
        if lazy["index"] is None:
            lazy["index"] = codex_reader.rollout_index()
        reviews = _review_children(root_id, _started_at(meta), lazy["index"], seen)
    for path, child_meta, link in reviews:
        tid = child_meta.get("id")
        if not isinstance(tid, str) or tid in seen:
            continue
        seen.add(tid)
        try:
            child_rows = codex_reader.load_rollout(path)
            revision = str(codex_reader.rollout_size(path))
        except (OSError, ValueError) as exc:
            out.skipped.append((tid, f"unreadable: {type(exc).__name__}"))
            continue
        live = now - max(_mtime(path), root_mtime) < LIVE_WINDOW_S
        out.agents.append(agent_capture.Agent(
            agent_id=tid, revision=revision, agent_type=REVIEW_TYPE,
            description=_description(link), tool_use_id=None, spawn_depth=1,
            parent_agent_id=None, status=status_of(child_rows, live=live),
            result_preview=preview_of(child_rows),
            load=_load_child(path), source={"path": str(path), "kind": "review"}))
    return out


def child_root(path: Path, payload_sid: str | None = None, *,
               index: dict | None = None) -> str | None:
    """The ROOT thread id a child rollout belongs to, walking ``parent`` links
    up through each ancestor's header (at most ``MAX_DEPTH``). With
    ``payload_sid``, only an ancestor equal to it is accepted — a hook payload
    naming session A with a transcript of a child of B is still refused.
    None for a rollout that is not a linked child."""
    seen = set()
    current = Path(path)
    for _ in range(MAX_DEPTH + 1):
        try:
            meta = codex_reader._session_meta(
                [codex_reader._session_header(current, strict=False)])
        except (OSError, ValueError):
            return None
        link = codex_reader.spawn_link(meta)
        if link is None:
            # ``current`` is a person's thread: the root, when we walked here.
            mid = meta.get("id")
            if current == Path(path) or not isinstance(mid, str):
                return None
            return mid if payload_sid in (None, mid) else None
        parent = link["parent"]
        if parent in seen:
            return None
        seen.add(parent)
        if payload_sid is not None and parent == payload_sid:
            return parent
        if index is None:
            index = codex_reader.rollout_index()
        nxt = index.get(parent)
        if nxt is None:
            return parent if payload_sid is None else None
        current = nxt
    return None


def plan_report(parent_conv_id: str, found: Discovery, details: bool = True) -> list[dict]:
    """The exact ``import_conversation`` agent arguments each found agent
    would be sent with (messages left out) — for dry runs and diagnostics."""
    return [agent_capture.import_arguments(parent_conv_id, a, details) for a in found.agents]


if __name__ == "__main__":  # a dry run: what would be sent, nothing is
    import json

    if len(sys.argv) < 2:
        print("usage: codex_agents.py <root session uuid | rollout path>", file=sys.stderr)
        raise SystemExit(2)
    target, err = codex_reader.locate(sys.argv[1])
    if target is None:
        print(err, file=sys.stderr)
        raise SystemExit(2)
    result = discover(target, log=lambda m: print(m, file=sys.stderr))
    rid = result.root_id or codex_reader.rollout_uuid(target) or "?"
    print(json.dumps({
        "root": f"codex-{rid}",
        "agents": [{"import_arguments": args,
                    "records": len(agent.load()),
                    "rollout": os.path.basename(agent.source["path"])}
                   for args, agent in zip(plan_report(f"codex-{rid}", result), result.agents)],
        "skipped": result.skipped,
        "unknown_shape": result.unknown_shape,
    }, indent=1))
