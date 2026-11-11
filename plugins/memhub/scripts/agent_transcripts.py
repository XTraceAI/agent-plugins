"""The agents a Claude Code session spawned, which of them to capture, and the
dynamic-workflow runs they belong to.

Claude Code writes every subagent, fork and background agent to its own
transcript beside the session's, and every agent a dynamic workflow (the
``Workflow`` tool) dispatches one level deeper, under its run:

    <projects>/<proj>/<session_id>.jsonl                                  the session
    <projects>/<proj>/<session_id>/subagents/agent-<id>.jsonl             one agent
    <projects>/<proj>/<session_id>/subagents/agent-<id>.meta.json         its sidecar
    <projects>/<proj>/<session_id>/subagents/workflows/<runId>/agent-<id>.jsonl (+ .meta.json)
    <projects>/<proj>/<session_id>/subagents/workflows/<runId>/journal.jsonl     started/result events
    <projects>/<proj>/<session_id>/workflows/<runId>.json                 the run record, once it ends
    <projects>/<proj>/<session_id>/workflows/scripts/<name>-<runId>.js    the run's script

Discovery reads exactly those two agent levels, never an unbounded walk.

The sidecar carries ``agentType``, ``description``, ``toolUseId`` (the id of
the ``Agent`` tool_use that spawned it, in the session's transcript or in
another agent's), ``parentAgentId`` and ``spawnDepth``. A workflow agent's
sidecar has ``agentType: "workflow-subagent"``, its label as ``description``,
``workflowPhase`` and no ``toolUseId``. MemHub-Backend captures each agent as
its own conversation linked to the session (spec: MemHub-Backend
docs/specs/agent-sessions-spec.md §4.1, §10.2).

**The MemHub harness fork is never captured.** The Stop hook's rule fork
(``harness_stop.py``) is the plugin's own machinery, not the person's work. It
is recognised by the launch prompt the plugin wrote — the spawning tool_use's
``input.prompt`` starts with ``FORK_MARK`` (or, from builds before 2026-09-28,
points at a ``memhub-plugin/harness/…fork-….prompt`` file) — never by
``description``, which the spawning agent chooses. A fork a plugin spawned
itself (sidecar ``pluginSteered``) is skipped the same way. Any agent whose
spawn chain reaches either is skipped too, and so is an agent whose spawning
tool_use cannot be found yet: the next flush looks again. A workflow run whose
every launching ``Workflow`` call sits only in skipped agents is skipped with
all of its agents. Only the plugin can do this; the server never sees the
prompt.

**Fork copies are stripped (exact-once).** An Agent-tool fork's transcript
opens with a ``fork-context-ref`` record and copies of its owner's spawning
response. Uploaded as they are, that response would count twice — once in the
owner, once in the fork. ``fork_copies`` / ``drop_fork_copies`` remove them
before status, result and upload.

**Workflow runs** (``workflow_runs``) are display metadata for the session:
name, status, phases and the agent roster, built from the run record when it
exists and from the journal, script and sidecars while a run is in progress or
was killed. They are redacted and never carry the script, previews or the
run's own token counts — the session's totals come from transcript usage.

**The wire is host-neutral** (``agent_capture``): the import arguments, their
redaction, the sent cache and the send loop are shared with the Codex and
Cursor hosts. ``claude_agent`` adapts this module's agent dicts onto
``agent_capture.Agent``; ``import_arguments`` and the ``server_accepts_*``
predicates (now in ``server_caps``) stay importable from here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import agent_capture
import server_caps
from agent_capture import AGENT_ID, RESULT_PREVIEW_CAP, RUN_ID
from harness_stop import FORK_MARK
from redact import redact, redact_text

_AGENT_PREFIX = agent_capture.AGENT_PREFIX
_WORKFLOWS_DIR = "workflows"
WORKFLOW_AGENT_TYPE = "workflow-subagent"
# Tools whose call is how an agent hands its answer back.
_HANDBACK = "SubagentHandback"
_STRUCTURED = "StructuredOutput"

# The server's limits on a run (§10.3). Clipped here so one long label cannot
# make the server drop the whole run.
_MAX_RUNS = 50
_MAX_LAUNCH_IDS = 20
_MAX_PHASES = 50
_MAX_RUN_AGENTS = 500

# Plugin builds before 2026-09-28 launched the fork with a pointer to a prompt
# file instead of the prompt itself. Those transcripts are still on disk, and a
# resumed session flushes them.
_LEGACY_FORK_PROMPT = re.compile(
    r"^Read \S*memhub-plugin/harness/\S+\.fork-[0-9a-f]+\.prompt and follow it exactly")


def is_harness_prompt(prompt: str) -> bool:
    return prompt.startswith(FORK_MARK) or bool(_LEGACY_FORK_PROMPT.match(prompt))


def session_ended(hook_input: dict) -> bool:
    """Whether this flush is the session's last: the SessionEnd hook, or a
    payload with no tool call that reports a reason (SessionEnd's shape)."""
    if not isinstance(hook_input, dict):
        return False
    if hook_input.get("hook_event_name") == "SessionEnd":
        return True
    return not hook_input.get("tool_input") and bool(hook_input.get("reason"))


# ── reading transcripts ───────────────────────────────────────────────────


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    """Every whole record of a JSONL file. Tolerant line by line: the file may
    be mid-write, and a partial last line is simply not there yet."""
    records: list[dict] = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return records
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _content(record) -> list:
    content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
    return content if isinstance(content, list) else []


def _response_key(record: dict) -> str | None:
    """The response an assistant record belongs to: Claude Code writes one
    record per content block, all sharing ``requestId`` (else ``message.id``)."""
    key = record.get("requestId") or (record.get("message") or {}).get("id")
    return key if isinstance(key, str) and key else None


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


class _Scan:
    """What one transcript says about spawning: every tool call's prompt (an
    ``Agent`` call's, else ""), the tool calls whose result was an error, and
    every ``Workflow`` call with its timestamp and result text."""

    def __init__(self):
        self.spawns: dict[str, str] = {}
        self.errors: set[str] = set()
        self.workflow_uses: list[tuple[str, str | None]] = []
        self.workflow_results: dict[str, str] = {}


def _scan(path: Path) -> _Scan:
    """Substring-gated, so only the lines that can matter are parsed."""
    scan = _Scan()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return scan
    with fh:
        for line in fh:
            has_use = '"tool_use"' in line
            has_error = '"is_error"' in line
            has_wf_result = bool(scan.workflow_uses) and '"tool_result"' in line and any(
                tid in line for tid, _ in scan.workflow_uses)
            if not (has_use or has_error or has_wf_result):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            for block in _content(record):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    name = block.get("name")
                    prompt = (block.get("input") or {}).get("prompt") \
                        if name in ("Agent", "Task") else None
                    scan.spawns.setdefault(block["id"], prompt if isinstance(prompt, str) else "")
                    if name == "Workflow" and block["id"] not in scan.workflow_results \
                            and all(block["id"] != t for t, _ in scan.workflow_uses):
                        ts = record.get("timestamp")
                        scan.workflow_uses.append((block["id"], ts if isinstance(ts, str) else None))
                elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    tid = block["tool_use_id"]
                    if block.get("is_error") is True:
                        scan.errors.add(tid)
                    if any(tid == t for t, _ in scan.workflow_uses):
                        scan.workflow_results.setdefault(tid, _result_text(block))
    return scan


def _response_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return keys
    with fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") == "assistant":
                key = _response_key(record)
                if key:
                    keys.add(key)
    return keys


# ── fork stripping (§10.2) ────────────────────────────────────────────────


def fork_copies(records: list, owner_keys) -> tuple[frozenset, frozenset]:
    """The response keys and tool_use ids of the owner's responses a fork's
    transcript opens with: every LEADING assistant record (after any
    ``fork-context-ref``) whose response key the owner also has."""
    keys: set[str] = set()
    tool_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("type") == "fork-context-ref":
            continue
        if record.get("type") != "assistant":
            break
        key = _response_key(record)
        if not key or key not in owner_keys:
            break
        keys.add(key)
        for block in _content(record):
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and isinstance(block.get("id"), str):
                tool_ids.add(block["id"])
    return frozenset(keys), frozenset(tool_ids)


def drop_fork_copies(records: list, keys=frozenset(), tool_ids=frozenset()) -> list:
    """``records`` without ``fork-context-ref`` records, without the copied
    owner responses (``keys``), and without the ``tool_result`` blocks that
    answer the copied calls (``tool_ids``). A user record left with no content
    is dropped; its other blocks (the fork's directive) stay. A no-op for an
    agent that shares nothing with its owner."""
    out = []
    for record in records:
        if not isinstance(record, dict):
            out.append(record)
            continue
        if record.get("type") == "fork-context-ref":
            continue
        if keys and record.get("type") == "assistant" and _response_key(record) in keys:
            continue
        if tool_ids and record.get("type") == "user":
            content = _content(record)
            kept = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"
                            and b.get("tool_use_id") in tool_ids)]
            if len(kept) != len(content):
                if not kept:
                    continue
                record = {**record, "message": {**record["message"], "content": kept}}
        out.append(record)
    return out


# ── what an agent did ─────────────────────────────────────────────────────


def _responses(records: list) -> list[list[dict]]:
    """Assistant records grouped by response, in order of each response's
    first record."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for i, record in enumerate(records):
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        key = _response_key(record) or f"#{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)
    return [groups[k] for k in order]


def _last_response(records: list) -> list[dict]:
    last = next((r for r in reversed(records)
                 if isinstance(r, dict) and r.get("type") == "assistant"), None)
    if last is None:
        return []
    key = _response_key(last)
    if key is None:
        return [last]
    return [r for r in records if isinstance(r, dict) and r.get("type") == "assistant"
            and _response_key(r) == key]


def agent_status(records: list, *, spawn_failed: bool, ended: bool) -> str:
    """§10.2 status of a (non-workflow) agent, from its stripped records."""
    if spawn_failed:
        return "failed"
    response = _last_response(records)
    if response:
        uses = [b for r in response for b in _content(r)
                if isinstance(b, dict) and b.get("type") == "tool_use"]
        if any(b.get("name") in (_HANDBACK, _STRUCTURED) for b in uses):
            return "done"
        stop = next((r["message"]["stop_reason"] for r in response
                     if (r.get("message") or {}).get("stop_reason") is not None), None)
        if not uses and stop != "tool_use":
            return "done"
    return "stopped" if ended else "running"


def returned_text(records: list) -> str | None:
    """What the agent handed back (§10.2 returned-text rule): from the latest
    response that carries an answer, a ``SubagentHandback``'s message, else a
    ``StructuredOutput``'s input as compact JSON, else its text blocks."""
    for response in reversed(_responses(records)):
        blocks = [b for r in response for b in _content(r) if isinstance(b, dict)]
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == _HANDBACK:
                message = (block.get("input") or {}).get("message")
                if isinstance(message, str) and message:
                    return message
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == _STRUCTURED:
                return json.dumps(block.get("input"), separators=(",", ":"),
                                  ensure_ascii=False)
        text = "\n".join(b["text"] for b in blocks
                         if b.get("type") == "text" and isinstance(b.get("text"), str)
                         and b["text"].strip())
        if text:
            return text
    return None


def _clip(value, cap: int):
    return value[:cap] if isinstance(value, str) else None


# ── discovery ─────────────────────────────────────────────────────────────


class _Survey:
    """Everything both public functions agree on: the agent transcripts found,
    why each is not sent, and which run each ``Workflow`` call launched."""

    def __init__(self):
        self.found: dict[str, dict] = {}
        self.skipped: list[str] = []
        self.reasons: dict[str, str] = {}
        self.owners: dict[str, str | None] = {}
        self.scans: dict[str | None, _Scan] = {}
        self.run_dirs: dict[str, Path] = {}
        self.run_records: dict[str, dict] = {}
        # run id -> [(tool_use id, transcript owner (None = session), timestamp)]
        self.anchors: dict[str, list[tuple[str, str | None, str | None]]] = {}
        self.skipped_runs: dict[str, str] = {}


def _session_dir(session_id: str, transcript_path: str) -> Path:
    return Path(transcript_path).parent / session_id


def _agent_paths(sub: Path) -> list[tuple[Path, str | None]]:
    """``(transcript, run id)`` for the two levels Claude Code writes, in
    sorted path order."""
    paths: list[tuple[Path, str | None]] = [
        (p, None) for p in sub.glob(f"{_AGENT_PREFIX}*.jsonl")]
    wf = sub / _WORKFLOWS_DIR
    if wf.is_dir():
        for run_dir in wf.iterdir():
            if run_dir.is_dir() and RUN_ID.match(run_dir.name):
                paths.extend((p, run_dir.name)
                             for p in run_dir.glob(f"{_AGENT_PREFIX}*.jsonl"))
    return sorted(paths, key=lambda pr: str(pr[0]))


def _run_records(session_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    wf = session_dir / _WORKFLOWS_DIR
    if not wf.is_dir():
        return out
    for path in wf.glob("wf_*.json"):
        run_id = path.stem
        if not RUN_ID.match(run_id):
            continue
        data = _read_json(path)
        if isinstance(data, dict):
            out[run_id] = data
    return out


def _survey(session_id: str, transcript_path: str) -> _Survey:
    survey = _Survey()
    session_dir = _session_dir(session_id, transcript_path)
    sub = session_dir / "subagents"
    if sub.is_dir():
        for path, run_id in _agent_paths(sub):
            agent_id = path.name[len(_AGENT_PREFIX):-len(".jsonl")]
            if agent_id in survey.found:
                survey.skipped.append(f"{agent_id}: duplicate agent id")
                continue
            meta = _read_json(path.with_name(path.name[:-len(".jsonl")] + ".meta.json"))
            survey.found[agent_id] = {"agent_id": agent_id, "path": path, "run_id": run_id,
                                      "meta": meta if isinstance(meta, dict) else {}}
        wf = sub / _WORKFLOWS_DIR
        if wf.is_dir():
            survey.run_dirs = {d.name: d for d in wf.iterdir()
                               if d.is_dir() and RUN_ID.match(d.name)}
    survey.run_records = _run_records(session_dir)
    if not survey.found and not survey.run_records and not survey.run_dirs:
        return survey

    # A fork's transcript repeats its parent's history, spawning calls
    # included, so a call is looked up where it was MADE: in the parent agent
    # the sidecar names (`parentAgentId`), else in the session. Built over
    # every agent found, workflow agents included, so nested spawns resolve.
    survey.scans[None] = _scan(Path(transcript_path))
    for agent_id, agent in survey.found.items():
        survey.scans[agent_id] = _scan(agent["path"])
    calls = {k: s.spawns for k, s in survey.scans.items()}
    found = survey.found

    def why_not(agent_id: str) -> str | None:
        seen: set[str] = set()
        current: str | None = agent_id
        while current is not None:
            if current in seen:
                return "spawn chain loops"
            seen.add(current)
            meta = found[current]["meta"]
            if meta.get("pluginSteered"):
                # A plugin's own fork ($.agent.spawn), not the person's work.
                return "spawned by a plugin" if current == agent_id \
                    else "spawned under a plugin's fork"
            parent = meta.get("parentAgentId")
            owner = parent if isinstance(parent, str) and parent else None
            if owner is not None and owner not in found:
                return "spawning agent not found yet"
            tool_use_id = meta.get("toolUseId")
            if tool_use_id is None:
                # No tool call spawned it (a fork the person opened, or a
                # workflow agent), so no call can be the harness fork's either.
                if current == agent_id:
                    survey.owners[agent_id] = owner
                current = owner
                continue
            prompt = calls[owner].get(tool_use_id) if isinstance(tool_use_id, str) else None
            if prompt is None and owner is None and isinstance(tool_use_id, str):
                # A sidecar without `parentAgentId` (older Claude Code builds):
                # the first other agent that made the call is the parent.
                owner, prompt = next(((aid, c[tool_use_id]) for aid, c in calls.items()
                                      if aid not in (None, current) and tool_use_id in c),
                                     (None, None))
            if prompt is None:
                return "spawning call not found yet"
            if current == agent_id:
                survey.owners[agent_id] = owner
            if is_harness_prompt(prompt):
                return "MemHub harness fork" if current == agent_id \
                    else "spawned under a MemHub harness fork"
            current = owner
        return None

    for agent_id in found:
        reason = why_not(agent_id)
        if reason:
            survey.reasons[agent_id] = reason

    # Which run each Workflow call launched: its result names the run's
    # transcript directory, or the task id the run record carries.
    task_ids = {rid: rec.get("taskId") for rid, rec in survey.run_records.items()
                if isinstance(rec.get("taskId"), str) and rec.get("taskId")}
    run_ids = set(survey.run_dirs) | set(survey.run_records)
    patterns = {rid: re.compile(
        re.escape(f"/subagents/workflows/{rid}") + r"(?![A-Za-z0-9_-])"
        + (r"|Task ID: " + re.escape(task_ids[rid]) + r"(?![A-Za-z0-9_-])"
           if rid in task_ids else ""))
        for rid in run_ids}
    for owner, scan in survey.scans.items():  # session first, then agents in path order
        for tool_use_id, ts in scan.workflow_uses:
            text = scan.workflow_results.get(tool_use_id) or ""
            for rid, pattern in patterns.items():
                if pattern.search(text):
                    survey.anchors.setdefault(rid, []).append((tool_use_id, owner, ts))

    # A run launched only from inside agents that are not sent is not the
    # person's work either: skip it, and its agents with it.
    for rid in sorted(run_ids):
        anchors = survey.anchors.get(rid) or []
        if anchors and all(o is not None and o in survey.reasons for _, o, _ in anchors):
            why = [survey.reasons[o] for _, o, _ in anchors]
            survey.skipped_runs[rid] = (
                "workflow launched under a plugin's fork"
                if all("plugin" in w for w in why)
                else "workflow launched under a MemHub harness fork"
                if any("harness" in w for w in why)
                else f"workflow launched by an agent not sent ({why[0]})")
    for agent_id, agent in found.items():
        if agent_id not in survey.reasons and agent["run_id"] in survey.skipped_runs:
            survey.reasons[agent_id] = survey.skipped_runs[agent["run_id"]]
    return survey


def agents(session_id: str, transcript_path: str, *,
           session_ended: bool) -> tuple[list[dict], list[str]]:
    """The session's agents to capture, and the ids skipped with why.

    Each kept agent is ``{agent_id, path, size, agent_type, description,
    tool_use_id, spawn_depth, workflow_run_id, parent_agent_id, status,
    result_preview}`` plus what the flush and ``workflow_runs`` need
    (``strip_keys``/``strip_tool_use_ids``, ``first_timestamp``,
    ``first_model``, ``workflow_phase``, ``sidecar_model``). A skip reads
    ``"<id>: <reason>"``."""
    survey = _survey(session_id, transcript_path)
    skipped = list(survey.skipped)
    owner_keys: dict[str | None, set[str]] = {}

    def keys_of(owner: str | None) -> set[str]:
        if owner not in owner_keys:
            path = Path(transcript_path) if owner is None else survey.found[owner]["path"]
            owner_keys[owner] = _response_keys(path)
        return owner_keys[owner]

    kept: list[dict] = []
    for agent_id, agent in survey.found.items():
        reason = survey.reasons.get(agent_id)
        if reason:
            skipped.append(f"{agent_id}: {reason}")
            continue
        meta = agent["meta"]
        try:
            size = agent["path"].stat().st_size
        except OSError:
            skipped.append(f"{agent_id}: transcript unreadable")
            continue
        run_id = agent["run_id"]
        owner = survey.owners.get(agent_id)
        records = _read_jsonl(agent["path"])
        first = next((r for r in records if r.get("type") != "fork-context-ref"), None)
        strip_keys, strip_ids = (fork_copies(records, keys_of(owner))
                                 if first is not None and first.get("type") == "assistant"
                                 else (frozenset(), frozenset()))
        records = drop_fork_copies(records, strip_keys, strip_ids)
        tool_use_id = meta.get("toolUseId")
        status = None
        if run_id is None:
            failed = isinstance(tool_use_id, str) and tool_use_id in survey.scans[owner].errors
            status = agent_status(records, spawn_failed=failed, ended=session_ended)
        result = returned_text(records)
        first_ts = next((r.get("timestamp") for r in records
                         if isinstance(r.get("timestamp"), str)), None)
        first_model = next((r["message"]["model"] for r in records
                            if r.get("type") == "assistant"
                            and isinstance((r.get("message") or {}).get("model"), str)), None)
        depth = meta.get("spawnDepth")
        parent = meta.get("parentAgentId")
        parent = parent if isinstance(parent, str) and parent else owner
        kept.append({
            "agent_id": agent_id,
            "path": agent["path"],
            "size": size,
            "agent_type": meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
            "description": meta.get("description") if isinstance(meta.get("description"), str) else None,
            "tool_use_id": tool_use_id,
            "spawn_depth": depth if isinstance(depth, int) and not isinstance(depth, bool)
            and 0 <= depth <= 32 else None,
            "workflow_run_id": run_id,
            "parent_agent_id": parent if run_id is None else None,
            "status": status,
            "result_preview": _clip(redact_text(result), RESULT_PREVIEW_CAP) if result else None,
            "strip_keys": strip_keys,
            "strip_tool_use_ids": strip_ids,
            "first_timestamp": first_ts,
            "first_model": first_model,
            "workflow_phase": meta.get("workflowPhase")
            if isinstance(meta.get("workflowPhase"), str) else None,
            "sidecar_model": meta.get("model") if isinstance(meta.get("model"), str) else None,
        })
    return kept, skipped


# ── workflow runs ─────────────────────────────────────────────────────────


_RUN_STATUS = {"completed": "completed", "failed": "failed", "error": "failed",
               "running": "running"}
_AGENT_STATE = {"done": "done", "failed": "failed", "error": "failed",
                "running": "running", "started": "running",
                "queued": "queued", "pending": "queued", "waiting": "queued"}
_META_BLOCK = re.compile(r"export\s+const\s+meta\b(.*?)\n\}", re.S)
_PHASES_START = re.compile(r"\bphases\s*:\s*\[")
_PHASE_TITLE = re.compile(r"title:\s*(['\"`])(.{1,200}?)\1")


def _iso_ms(ms) -> str | None:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ms_of(iso) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def _script_of(session_dir: Path, run_id: str) -> Path | None:
    scripts = session_dir / _WORKFLOWS_DIR / "scripts"
    if not scripts.is_dir():
        return None
    return next(iter(sorted(scripts.glob(f"*-{run_id}.js"))), None)


def _script_phases(script: Path | None) -> list[str]:
    if script is None:
        return []
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    meta = _META_BLOCK.search(text)
    block = meta.group(1) if meta else ""
    start = _PHASES_START.search(block)
    if not start:
        return []
    return [m.group(2) for m in _PHASE_TITLE.finditer(block[start.end():])]


def _journal(run_dir: Path | None) -> tuple[list[dict], set[str]]:
    """``started`` events in order (one per agent), and the agents with a
    ``result``."""
    if run_dir is None:
        return [], set()
    started: list[dict] = []
    seen: set[str] = set()
    results: set[str] = set()
    for event in _read_jsonl(run_dir / "journal.jsonl"):
        aid = event.get("agentId")
        if not isinstance(aid, str):
            continue
        if event.get("type") == "started" and aid not in seen:
            seen.add(aid)
            started.append(event)
        elif event.get("type") == "result":
            results.add(aid)
    return started, results


def _model(agent: dict | None) -> str | None:
    if agent is None:
        return None
    sidecar = agent.get("sidecar_model")
    if sidecar and sidecar != "inherit":
        return sidecar
    return agent.get("first_model")


def workflow_runs(session_id: str, transcript_path: str, kept: list[dict], *,
                  session_ended: bool) -> tuple[list[dict], list[str]]:
    """The session's dynamic-workflow runs in the §10.2 wire shape (redacted),
    and the runs skipped with why (``"<runId>: <reason>"``)."""
    survey = _survey(session_id, transcript_path)
    session_dir = _session_dir(session_id, transcript_path)
    kept_ids = {a["agent_id"] for a in kept}
    by_run: dict[str, list[dict]] = {}
    for agent in kept:
        if agent.get("workflow_run_id"):
            by_run.setdefault(agent["workflow_run_id"], []).append(agent)

    runs: list[dict] = []
    skipped: list[str] = []
    for run_id in sorted(set(survey.run_dirs) | set(survey.run_records)):
        if run_id in survey.skipped_runs:
            skipped.append(f"{run_id}: {survey.skipped_runs[run_id]}")
            continue
        record = survey.run_records.get(run_id)
        run_dir = survey.run_dirs.get(run_id)
        transcripts = {a["agent_id"]: a for a in by_run.get(run_id, [])}
        started, results = _journal(run_dir)
        anchors = [a for a in survey.anchors.get(run_id, [])
                   if a[1] is None or a[1] in kept_ids]
        launch_ids: list[str] = []
        for tool_use_id, _, _ in anchors:
            if tool_use_id not in launch_ids:
                launch_ids.append(tool_use_id)

        if record is not None:
            name = record.get("workflowName") if isinstance(record.get("workflowName"), str) else None
            raw = record.get("status")
            status = _RUN_STATUS.get(raw, "stopped") if isinstance(raw, str) else "stopped"
            start_ms = record.get("startTime")
            started_at = _iso_ms(start_ms)
            duration = record.get("durationMs")
            duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else None
            summary = _clip(record.get("summary"), 1000)
            result = record.get("result")
            result = result if isinstance(result, str) or result is None \
                else json.dumps(result, separators=(",", ":"), ensure_ascii=False)
            result = _clip(result, RESULT_PREVIEW_CAP)
            phases = [{"index": i + 1, "title": p.get("title"),
                       "detail": p.get("detail") if isinstance(p.get("detail"), str) else None}
                      for i, p in enumerate(record.get("phases") or [])
                      if isinstance(p, dict) and isinstance(p.get("title"), str)]
        else:
            script = _script_of(session_dir, run_id)
            name = script.name[:-len(f"-{run_id}.js")] if script is not None else None
            status = "stopped" if session_ended else "running"
            start_ms = None
            started_at = anchors[0][2] if anchors else None
            duration = summary = result = None
            titles = _script_phases(script)
            if not titles:
                for event in started:
                    phase = event.get("phase")
                    if isinstance(phase, str) and phase not in titles:
                        titles.append(phase)
            phases = [{"index": i + 1, "title": t, "detail": None} for i, t in enumerate(titles)]
        phase_index = {}
        for p in phases:
            phase_index.setdefault(p["title"], p["index"])

        roster: list[dict] = []
        listed: set[str] = set()
        if record is not None:
            for entry in record.get("workflowProgress") or []:
                if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
                    continue
                aid = entry.get("agentId")
                if not isinstance(aid, str) or not AGENT_ID.match(aid) or aid in listed:
                    continue
                listed.add(aid)
                try:
                    pidx = int(entry.get("phaseIndex"))
                except (TypeError, ValueError):
                    pidx = phase_index.get(entry.get("phaseTitle"))
                state = entry.get("state")
                roster.append({
                    "agent_id": aid,
                    "label": entry.get("label") if isinstance(entry.get("label"), str) else None,
                    "phase_index": pidx,
                    "state": _AGENT_STATE.get(state, "stopped") if isinstance(state, str) else "stopped",
                    "model": entry.get("model") if isinstance(entry.get("model"), str)
                    else _model(transcripts.get(aid)),
                    "superseded": False,
                })
        # Agents the roster does not list: every agent while the run has no
        # record; after it, the agents of an earlier attempt of a resumed run
        # (superseded) or of an attempt still going (the run is running).
        extra = [e["agentId"] for e in started if e["agentId"] not in listed]
        extra += [aid for aid in transcripts if aid not in listed and aid not in extra]
        labels = {e["agentId"]: e for e in started}
        for aid in extra:
            if not AGENT_ID.match(aid):
                continue
            agent = transcripts.get(aid)
            event = labels.get(aid) or {}
            label = event.get("label") if isinstance(event.get("label"), str) \
                else (agent or {}).get("description")
            phase = event.get("phase") if isinstance(event.get("phase"), str) \
                else (agent or {}).get("workflow_phase")
            superseded = False
            if record is not None:
                first = _ms_of((agent or {}).get("first_timestamp"))
                # No transcript to date it: it cannot be the attempt the
                # record describes, so it is counted with the earlier one.
                superseded = first is None or not isinstance(start_ms, (int, float)) \
                    or first < start_ms
                if not superseded:
                    status = "running"
                if superseded:
                    phase = (agent or {}).get("workflow_phase") or phase
            if aid in results:
                state = "done"
            else:
                state = "stopped" if session_ended or superseded else "running"
            roster.append({"agent_id": aid, "label": label,
                           "phase_index": phase_index.get(phase),
                           "state": state, "model": _model(agent),
                           "superseded": superseded})

        run = {
            "run_id": run_id,
            "name": _clip(name or run_id, 200),
            "status": status,
            "launch_tool_use_ids": [t for t in launch_ids if len(t) <= 200][:_MAX_LAUNCH_IDS],
            "started_at": started_at,
            "duration_ms": duration,
            "summary": summary,
            "result_preview": result,
            "phases": [{"index": p["index"], "title": p["title"][:200],
                        "detail": _clip(p["detail"], 500)}
                       for p in phases if 1 <= p["index"] <= _MAX_PHASES],
            "agents": [{**a, "label": _clip(a["label"], 200), "model": _clip(a["model"], 100)}
                       for a in roster[:_MAX_RUN_AGENTS]],
        }
        runs.append(run)
    runs.sort(key=lambda r: (r["started_at"] is None, r["started_at"] or "", r["run_id"]))
    return redact(runs[-_MAX_RUNS:]), skipped


# ── the wire: the Claude Code adapter onto agent_capture ─────────────────


def claude_agent(agent: dict) -> agent_capture.Agent:
    """One of ``agents()``' dicts as the host-neutral ``agent_capture.Agent``.
    The revision is the transcript's byte size, so the sent-cache key is the
    one this module has always written; the dict rides along as ``source``
    for the sender (path, fork-strip keys)."""
    return agent_capture.Agent(
        agent_id=agent["agent_id"],
        revision=str(agent.get("size")),
        agent_type=agent.get("agent_type"),
        description=agent.get("description"),
        tool_use_id=agent.get("tool_use_id"),
        spawn_depth=agent.get("spawn_depth"),
        parent_agent_id=agent.get("parent_agent_id"),
        workflow_run_id=agent.get("workflow_run_id"),
        status=agent.get("status"),
        result_preview=agent.get("result_preview"),
        source=agent,
    )


def import_arguments(session_id: str, agent: dict, details: bool = False) -> dict:
    """The agent arguments ``import_conversation`` takes for one of
    ``agents()``' dicts — ``agent_capture.import_arguments`` (redacted, within
    the server's limits) with the session as the parent."""
    return agent_capture.import_arguments(session_id, claude_agent(agent), details)


# Gating moved to server_caps; the old names stay importable from here.
_import_properties = server_caps._import_properties
server_accepts_agents = server_caps.server_accepts_agents
server_accepts_agent_details = server_caps.server_accepts_agent_details
server_accepts_workflow_runs = server_caps.server_accepts_workflow_runs
