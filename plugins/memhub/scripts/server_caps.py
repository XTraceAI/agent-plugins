"""What this MemHub server's ``import_conversation`` accepts for agents, read
from the tool's input schema and, for hosts that ask often, cached on disk.

Agent capture is gated on the schema because an older server ignores the
arguments it does not know: sent an agent, it would file the agent as a
session of its own — the exact thing agent capture exists to avoid. Three
generations matter (MemHub-Backend docs/specs/agent-sessions-spec.md):

    agents   §4.1   ``agent_id`` + ``parent_conversation_id``: link the agent
    details  §10.2  ``workflow_run_id`` too: parent agent, status, preview
    runs     §10.3  ``workflow_runs`` on the session's last slice

and one more, read from the tool list itself rather than a schema:

    bug_origin  ENG-1268 §3.3  a ``report_bug_origin`` tool exists — the
                pr-link hook asks for a bug-origin investigation only then

The predicates (``server_accepts_*``) are pure functions of a ``tools/list``
answer; ``agent_transcripts`` re-exports them under their old names.

**The cache** (``get``) is for hosts whose flush runs on every turn (Codex,
Cursor): listing the tools costs a round trip, and the answer changes only on
a deploy. ``<state_dir>/_server_caps`` holds ``{url: {agents, details, runs,
bug_origin, checked_at}}``; a positive ``agents`` answer is trusted for
``POSITIVE_TTL_S`` (6 h), anything else for ``NEGATIVE_TTL_S`` (30 min) so a
server that gains agent capture is noticed soon. An entry written before a capability existed (no
``bug_origin`` key) is a miss, not a "no": otherwise a pre-upgrade cache would
hide a tool the server has for up to 6 h. The file name has **no ``.json`` suffix** on
purpose: ``capture_health`` globs ``<state_dir>/*.json`` as breadcrumbs. A
tool-input error that names an agent argument means the cache is stale —
``invalidate`` drops the entry, and ``names_agent_argument`` recognises such
an error.

Claude Code does not use the cache: ``flush_session`` lists the tools once per
flush, as it always has, and passes the answer to ``from_tools``.

Stdlib only. Nothing here raises on a bad cache file: it reads as empty.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

CACHE_NAME = "_server_caps"
POSITIVE_TTL_S = 6 * 3600
NEGATIVE_TTL_S = 30 * 60

#: Agent arguments a server-side input error may name (§4.1 + §10.2).
AGENT_ARGUMENTS = ("agent_id", "parent_conversation_id", "parent_tool_use_id",
                   "agent_type", "agent_description", "spawn_depth",
                   "parent_agent_id", "workflow_run_id", "agent_status",
                   "agent_result_preview")


def _import_properties(tools) -> dict:
    for tool in tools or ():
        if isinstance(tool, dict) and tool.get("name") == "import_conversation":
            props = (tool.get("inputSchema") or {}).get("properties")
            return props if isinstance(props, dict) else {}
    return {}


def server_accepts_agents(tools: list[dict]) -> bool:
    """Whether this server's ``import_conversation`` takes agent arguments.

    An older server ignores arguments it does not know, so sending it an agent
    would file the agent as a session of its own — the exact thing this
    feature exists to avoid. Checked from the tool's input schema, before any
    agent is sent."""
    props = _import_properties(tools)
    return "agent_id" in props and "parent_conversation_id" in props


def server_accepts_agent_details(tools: list[dict]) -> bool:
    """Whether the server takes the §10.2 agent details (parent agent,
    workflow run, status, returned text)."""
    return server_accepts_agents(tools) and "workflow_run_id" in _import_properties(tools)


def server_accepts_workflow_runs(tools: list[dict]) -> bool:
    """Whether the server stores a session's ``workflow_runs``."""
    return "workflow_runs" in _import_properties(tools)


def server_accepts_bug_origin(tools: list[dict]) -> bool:
    """Whether the server lists ``report_bug_origin`` (MemHub-Backend
    docs/specs/pr-weight-success-spec.md §3.3): the tool's presence is the
    whole capability signal."""
    return any(isinstance(tool, dict) and tool.get("name") == "report_bug_origin"
               for tool in tools or ())


@dataclass(frozen=True)
class Caps:
    """One server's answer. ``details`` implies ``agents``."""
    agents: bool = False
    details: bool = False
    runs: bool = False
    bug_origin: bool = False


def from_tools(tools) -> Caps:
    """The capabilities a ``tools/list`` answer advertises."""
    return Caps(agents=server_accepts_agents(tools),
                details=server_accepts_agent_details(tools),
                runs=server_accepts_workflow_runs(tools),
                bug_origin=server_accepts_bug_origin(tools))


def _cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / CACHE_NAME


def _read(state_dir: Path) -> dict:
    try:
        data = json.loads(_cache_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(state_dir: Path, data: dict) -> None:
    try:
        import atomic_write  # noqa: PLC0415 — beside this file
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        atomic_write.publish(_cache_path(state_dir), json.dumps(data))
    except Exception:  # noqa: BLE001 — a lost cache entry costs one list call
        pass


def cached(state_dir: Path, url: str, *, now: float | None = None) -> Caps | None:
    """The cached answer for ``url`` while it is fresh, else None."""
    entry = _read(state_dir).get(url)
    if not isinstance(entry, dict):
        return None
    checked = entry.get("checked_at")
    if not isinstance(checked, (int, float)) or isinstance(checked, bool):
        return None
    if "bug_origin" not in entry:      # written before the field existed
        return None
    caps = Caps(agents=entry.get("agents") is True,
                details=entry.get("details") is True,
                runs=entry.get("runs") is True,
                bug_origin=entry.get("bug_origin") is True)
    # Keyed on ``agents`` alone, so a server that gains agent capture is still
    # noticed within the negative TTL; bug_origin accepts the shorter window.
    ttl = POSITIVE_TTL_S if caps.agents else NEGATIVE_TTL_S
    now = time.time() if now is None else now
    if not 0 <= now - checked < ttl:
        return None
    return caps


def remember(state_dir: Path, url: str, caps: Caps, *, now: float | None = None) -> None:
    data = _read(state_dir)
    data[url] = {**asdict(caps), "checked_at": time.time() if now is None else now}
    _write(state_dir, data)


def get(state_dir: Path, url: str, list_tools: Callable[[], list],
        *, now: float | None = None) -> Caps | None:
    """The server's capabilities: the cache while fresh, else one
    ``list_tools()`` call, remembered. None when the tools cannot be listed
    (nothing is cached then, so the next flush asks again)."""
    hit = cached(state_dir, url, now=now)
    if hit is not None:
        return hit
    try:
        tools = list_tools()
    except Exception:  # noqa: BLE001 — the caller sends no agents this time
        return None
    caps = from_tools(tools)
    remember(state_dir, url, caps, now=now)
    return caps


def invalidate(state_dir: Path, url: str) -> None:
    """Forget ``url``'s answer, so the next ``get`` lists the tools again."""
    data = _read(state_dir)
    if data.pop(url, None) is not None:
        _write(state_dir, data)


def names_agent_argument(detail: str) -> bool:
    """Whether a server's input-validation error names an agent argument —
    the sign that the cached answer is stale."""
    text = str(detail or "")
    return any(name in text for name in AGENT_ARGUMENTS)
