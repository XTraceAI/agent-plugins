"""The agents a Claude Code session spawned, and which of them to capture.

Claude Code writes every subagent, fork and background agent to its own
transcript beside the session's:

    <projects>/<proj>/<session_id>.jsonl                          the session
    <projects>/<proj>/<session_id>/subagents/agent-<id>.jsonl     one agent
    <projects>/<proj>/<session_id>/subagents/agent-<id>.meta.json its sidecar

The sidecar carries ``agentType``, ``description``, ``toolUseId`` (the id of
the ``Agent`` tool_use that spawned it, in the session's transcript or in
another agent's) and ``spawnDepth``. MemHub-Backend captures each agent as its
own conversation linked to the session (spec: MemHub-Backend
docs/specs/agent-sessions-spec.md §4.1).

**The MemHub harness fork is never captured.** The Stop hook's rule fork
(``harness_stop.py``) is the plugin's own machinery, not the person's work. It
is recognised by the launch prompt the plugin wrote — the spawning tool_use's
``input.prompt`` starts with ``FORK_MARK`` (or, from builds before 2026-09-28,
points at a ``memhub-plugin/harness/…fork-….prompt`` file) — never by
``description``, which the spawning agent chooses. A fork a plugin spawned
itself (sidecar ``pluginSteered``) is skipped the same way. Any agent whose
spawn chain reaches either is skipped too, and so is an agent whose spawning
tool_use cannot be found yet: the next flush looks again. Only the plugin can
do this; the server never sees the prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from harness_stop import FORK_MARK

_AGENT_PREFIX = "agent-"

# Plugin builds before 2026-09-28 launched the fork with a pointer to a prompt
# file instead of the prompt itself. Those transcripts are still on disk, and a
# resumed session flushes them.
_LEGACY_FORK_PROMPT = re.compile(
    r"^Read \S*memhub-plugin/harness/\S+\.fork-[0-9a-f]+\.prompt and follow it exactly")


def is_harness_prompt(prompt: str) -> bool:
    return prompt.startswith(FORK_MARK) or bool(_LEGACY_FORK_PROMPT.match(prompt))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _spawns(path: Path) -> dict[str, str]:
    """``{tool_use id: prompt}`` for every tool call in one transcript. Agents
    are spawned by other tools too (a workflow, a skill); only an ``Agent``
    call's prompt can be the harness fork's, so the others map to "".

    Tolerant line by line: the file may be mid-write, and a partial last line
    only means a spawn not visible until the next flush."""
    found: dict[str, str] = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return found
    with fh:
        for line in fh:
            if '"tool_use"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content") \
                if isinstance(record, dict) else None
            for block in content if isinstance(content, list) else ():
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and isinstance(block.get("id"), str)):
                    prompt = (block.get("input") or {}).get("prompt") \
                        if block.get("name") in ("Agent", "Task") else None
                    found.setdefault(block["id"], prompt if isinstance(prompt, str) else "")
    return found


def agents(session_id: str, transcript_path: str) -> tuple[list[dict], list[str]]:
    """The session's agents to capture, and the ids skipped with why.

    Each kept agent is ``{agent_id, path, size, agent_type, description,
    tool_use_id, spawn_depth}``. A skip reads ``"<id>: <reason>"``."""
    sub = Path(transcript_path).parent / session_id / "subagents"
    if not sub.is_dir():
        return [], []
    found: dict[str, dict] = {}
    for path in sorted(sub.glob(f"{_AGENT_PREFIX}*.jsonl")):
        agent_id = path.stem[len(_AGENT_PREFIX):]
        meta = _read_json(path.with_suffix(".meta.json"))
        found[agent_id] = {"agent_id": agent_id, "path": path,
                           "meta": meta if isinstance(meta, dict) else {}}

    # A fork's transcript repeats its parent's history, spawning calls
    # included, so a call is looked up where it was MADE: in the parent agent
    # the sidecar names (`parentAgentId`), else in the session.
    calls = {None: _spawns(Path(transcript_path))}
    for agent_id, agent in found.items():
        calls[agent_id] = _spawns(agent["path"])

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
                # No tool call spawned it (a fork the person opened), so no
                # call can be the harness fork's either.
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
            if is_harness_prompt(prompt):
                return "MemHub harness fork" if current == agent_id \
                    else "spawned under a MemHub harness fork"
            current = owner
        return None

    kept: list[dict] = []
    skipped: list[str] = []
    for agent_id, agent in found.items():
        reason = why_not(agent_id)
        if reason:
            skipped.append(f"{agent_id}: {reason}")
            continue
        meta = agent["meta"]
        depth = meta.get("spawnDepth")
        try:
            size = agent["path"].stat().st_size
        except OSError:
            skipped.append(f"{agent_id}: transcript unreadable")
            continue
        kept.append({
            "agent_id": agent_id,
            "path": agent["path"],
            "size": size,
            "agent_type": meta.get("agentType") if isinstance(meta.get("agentType"), str) else None,
            "description": meta.get("description") if isinstance(meta.get("description"), str) else None,
            "tool_use_id": meta.get("toolUseId"),
            "spawn_depth": depth if isinstance(depth, int) and not isinstance(depth, bool)
            and 0 <= depth <= 32 else None,
        })
    return kept, skipped


def import_arguments(session_id: str, agent: dict) -> dict:
    """The agent arguments ``import_conversation`` takes (spec §4.1), with the
    server's limits applied so a long description cannot fail the import."""
    args = {
        "conversation_id": f"{session_id}/{_AGENT_PREFIX}{agent['agent_id']}",
        "parent_conversation_id": session_id,
        "agent_id": agent["agent_id"],
    }
    if agent.get("agent_type"):
        args["agent_type"] = agent["agent_type"][:100]
    if agent.get("description"):
        args["agent_description"] = agent["description"][:1000]
    if isinstance(agent.get("tool_use_id"), str) and len(agent["tool_use_id"]) <= 200:
        args["parent_tool_use_id"] = agent["tool_use_id"]
    if agent.get("spawn_depth") is not None:
        args["spawn_depth"] = agent["spawn_depth"]
    return args


def server_accepts_agents(tools: list[dict]) -> bool:
    """Whether this server's ``import_conversation`` takes agent arguments.

    An older server ignores arguments it does not know, so sending it an agent
    would file the agent as a session of its own — the exact thing this
    feature exists to avoid. Checked from the tool's input schema, once per
    flush, before any agent is sent."""
    for tool in tools or ():
        if isinstance(tool, dict) and tool.get("name") == "import_conversation":
            props = (tool.get("inputSchema") or {}).get("properties") or {}
            return "agent_id" in props and "parent_conversation_id" in props
    return False
