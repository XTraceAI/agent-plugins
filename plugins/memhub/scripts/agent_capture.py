"""Host-neutral agent capture: how an agent a session spawned is planned,
described to ``import_conversation`` and sent, linked to its parent.

Every host that captures agents (Claude Code today; Codex and Cursor next)
finds its agents its own way and then hands them here. This module owns what
must not differ between hosts: the import arguments and their redaction, the
sent cache, the deadline, and the order of operations. MemHub-Backend
docs/specs/agent-sessions-spec.md §4.1 and §10.2 define the wire.

The seam
--------

``Agent``
    One agent, described host-neutrally. ``revision`` is what changes when
    the agent's transcript does (Claude Code and Codex: its byte size,
    decompressed for ``.jsonl.zst``; Cursor: a digest of its store blobs or
    its JSONL size). ``status`` is already one of ``STATUSES`` or None
    (``normalise_status`` maps host words). ``load`` returns the agent's
    canonical records with any fork copy already stripped and **not yet
    redacted**; a host whose sender reads the transcript itself may leave it
    None. ``source`` carries the host's own object through untouched.

``import_arguments(parent_conv_id, agent, details) -> dict``
    The agent arguments for one import: ``conversation_id =
    <parent_conv_id>/agent-<agent_id>``, ``parent_conversation_id``,
    ``agent_id``, ``agent_type``, ``agent_description``,
    ``parent_tool_use_id``, ``spawn_depth``; with ``details`` (the server
    takes §10.2) also ``parent_agent_id``, ``workflow_run_id`` or
    ``agent_status``, and ``agent_result_preview``. The parent is whatever
    the host calls its conversation (``<session>``, ``codex-<root>``,
    ``cursor-<root>``). Text arguments are redacted (``redact.redact_text``)
    and then clipped to the server's limits. The spawning tool id is sent
    byte-identical or not at all (``tool_use_id_ok``): a NUL, an id that is
    blank or longer than 200 characters is dropped, and an embedded ``\\n``
    (Cursor's ``call-…\\nfc_…``) is **kept**, because the parent's stored
    tool_use carries the same bytes and the backend matches them exactly.

``sent_key(agent, details) -> str``
    ``"<revision>:<status or ->:<d|b>"`` — what the sent cache records once an
    agent landed whole. Any change re-sends the agent; the server's watermark
    dedups its records.

``load_sent(cache_path) -> dict`` / ``plan(cache_path, agents) -> Plan``
    The sent cache (``<state_dir>/<session>.agents``, a JSON object
    ``{agent_id: key}``) and the agents not yet sent WITH details — what may be
    due before the server's answer is known, so a host can skip listing the
    server's tools when nothing is.

``async send_agents(caps, *, parent_conv_id, agents, cache_path, deadline,
send_one, log, sent=None) -> SendReport``
    Sends each due agent through the host's ``send_one(agent, arguments) ->
    bool`` (True: every slice landed). The rules every host gets:

    - call it only after the parent's own save was confirmed — an agent sent
      first would sit unlinked;
    - nothing goes to a server whose ``caps.agents`` is false;
    - an agent's key is written only after it landed whole, so a failed or
      cut-short agent goes again next time;
    - it stops when less than ``MIN_SLICE_BUDGET_S`` of ``deadline``
      (``time.monotonic()``) is left, and the next flush sends the rest;
    - the first agent that fails (``send_one`` returns False or raises an
      ``Exception``; a cancellation still propagates) stops the run; the
      failure is counted in
      the ``SendReport``, never in the parent's own failure streak (a host
      that keeps one must not feed it from here).

``STATUSES`` / ``normalise_status(word)``
    The four statuses the backend stores (``running``, ``done``, ``failed``,
    ``stopped``) and the host words that map onto them.

Gating lives in ``server_caps`` (``Caps``, ``from_tools``, the on-disk cache).
The Claude Code adapter is ``agent_transcripts.claude_agent`` plus
``flush_session._flush_agents``; the Claude path's behaviour is unchanged by
this module except that ``agent_type`` and ``agent_description`` are now
redacted like every other captured text.

Stdlib only.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from redact import redact_text

AGENT_PREFIX = "agent-"
RUN_ID = re.compile(r"^wf_[A-Za-z0-9_-]{1,64}$")
AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RESULT_PREVIEW_CAP = 2000
AGENT_TYPE_CAP = 100
DESCRIPTION_CAP = 1000
TOOL_USE_ID_CAP = 200
#: Stop when less than this is left of the deadline; a send that starts with
#: two seconds left only times out (flush_session's slice rule, shared).
MIN_SLICE_BUDGET_S = 10.0

STATUSES = ("running", "done", "failed", "stopped")
#: Host words for an agent's state -> the backend's. Unknown words map to None
#: (no status sent), never to a guess.
STATUS_MAP = {
    "running": "running", "started": "running", "in_progress": "running",
    "done": "done", "completed": "done", "complete": "done", "success": "done",
    "succeeded": "done",
    "failed": "failed", "error": "failed", "errored": "failed",
    "stopped": "stopped", "aborted": "stopped", "interrupted": "stopped",
    "cancelled": "stopped", "canceled": "stopped",
}


def normalise_status(word) -> str | None:
    """A host's status word as one of ``STATUSES``, or None."""
    if not isinstance(word, str):
        return None
    return STATUS_MAP.get(word.strip().lower())


@dataclass
class Agent:
    """One agent to capture. See the module docstring for each field."""
    agent_id: str
    revision: str
    agent_type: str | None = None
    description: str | None = None
    tool_use_id: str | None = None
    spawn_depth: int | None = None
    parent_agent_id: str | None = None
    workflow_run_id: str | None = None
    status: str | None = None
    result_preview: str | None = None
    load: Callable[[], list] | None = field(default=None, repr=False, compare=False)
    source: Any = field(default=None, repr=False, compare=False)


def tool_use_id_ok(value) -> bool:
    """Whether a spawning tool id can be sent as it is: a string, not blank,
    at most ``TOOL_USE_ID_CAP`` characters, no NUL. Never normalised — an
    embedded newline stays, because the id must match the parent's byte for
    byte."""
    return (isinstance(value, str) and bool(value.strip())
            and len(value) <= TOOL_USE_ID_CAP and "\x00" not in value)


def _text(value, cap: int) -> str | None:
    """A text argument as it may leave the machine: redacted, THEN clipped —
    clipping first could cut a key short enough to escape the pattern."""
    if not isinstance(value, str) or not value:
        return None
    return redact_text(value)[:cap] or None


def import_arguments(parent_conv_id: str, agent: Agent, details: bool = False) -> dict:
    """The agent arguments ``import_conversation`` takes (spec §4.1, and the
    §10.2 details when ``details``), redacted and within the server's limits
    so a long description cannot fail the import."""
    args = {
        "conversation_id": f"{parent_conv_id}/{AGENT_PREFIX}{agent.agent_id}",
        "parent_conversation_id": parent_conv_id,
        "agent_id": agent.agent_id,
    }
    workflow = agent.workflow_run_id
    agent_type = _text(agent.agent_type, AGENT_TYPE_CAP)
    if agent_type:
        args["agent_type"] = agent_type
    description = _text(agent.description, DESCRIPTION_CAP)
    if description:
        args["agent_description"] = description
    if not workflow and tool_use_id_ok(agent.tool_use_id):
        args["parent_tool_use_id"] = agent.tool_use_id
    if agent.spawn_depth is not None:
        args["spawn_depth"] = agent.spawn_depth
    if details:
        parent = agent.parent_agent_id
        if isinstance(parent, str) and AGENT_ID.match(parent):
            args["parent_agent_id"] = parent
        if isinstance(workflow, str) and RUN_ID.match(workflow):
            args["workflow_run_id"] = workflow
        elif agent.status in STATUSES:
            args["agent_status"] = agent.status
        preview = _text(agent.result_preview, RESULT_PREVIEW_CAP)
        if preview:
            args["agent_result_preview"] = preview
    return args


def sent_key(agent: Agent, details: bool) -> str:
    """What the sent cache records for an agent that landed whole: its
    revision, its status and whether the server took the details. Any change
    re-sends it — the server's watermark dedups the records. A legacy integer
    entry never matches, so each agent cached before keys were strings is
    re-sent once."""
    return f"{agent.revision}:{agent.status or '-'}:{'d' if details else 'b'}"


def load_sent(cache_path: Path) -> dict:
    """The sent cache, ``{agent_id: key}``; empty when absent or unreadable."""
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class Plan:
    """The sent cache as read, and the agents that may be due."""
    sent: dict
    todo: list


def plan(cache_path: Path, agents: list[Agent]) -> Plan:
    """The agents not yet sent with the details. Whether the server takes the
    details is known only once its tools are listed, so anything not yet sent
    WITH them may be due; ``send_agents`` narrows it with the real answer."""
    sent = load_sent(cache_path)
    return Plan(sent, [a for a in agents if sent.get(a.agent_id) != sent_key(a, True)])


@dataclass
class SendReport:
    """What one ``send_agents`` run did. ``failed`` is the agent that stopped
    the run, if one did; ``deferred`` counts the agents left for next time
    (deadline or failure); ``gated`` is set when the server takes no agents."""
    sent: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    deferred: int = 0
    gated: bool = False


def _publish(cache_path: Path, sent: dict) -> None:
    try:
        import atomic_write  # noqa: PLC0415 — beside this file
        atomic_write.publish(Path(cache_path), json.dumps(sent))
    except Exception:  # noqa: BLE001 — worst case the agent is re-sent
        pass


async def send_agents(caps, *, parent_conv_id: str, agents: list[Agent],
                      cache_path: Path, deadline: float,
                      send_one: Callable[[Agent, dict], Awaitable[bool]],
                      log: Callable[[str], None] = lambda _m: None,
                      sent: dict | None = None,
                      clock: Callable[[], float] = time.monotonic) -> SendReport:
    """Send each due agent as its own conversation linked to
    ``parent_conv_id``. ``caps`` is a ``server_caps.Caps``; ``agents`` the
    candidates (usually ``plan(...).todo``); ``sent`` the cache as already
    read, else it is read from ``cache_path``. See the module docstring for
    the rules."""
    report = SendReport()
    if not agents:
        return report
    if not caps.agents:
        log(f"agents not sent: this server predates agent capture "
            f"({len(agents)} waiting)")
        report.gated = True
        report.deferred = len(agents)
        return report
    details = bool(caps.details)
    sent = load_sent(cache_path) if sent is None else sent
    todo = [a for a in agents if sent.get(a.agent_id) != sent_key(a, details)]
    for index, agent in enumerate(todo):
        if deadline - clock() < MIN_SLICE_BUDGET_S:
            log(f"deadline reached; {len(todo) - index} agent(s) "
                "not sent — the next flush sends them")
            report.deferred = len(todo) - index
            return report
        try:
            ok = await send_one(agent, import_arguments(parent_conv_id, agent, details))
        except Exception as e:  # noqa: BLE001 — one agent must not end the host's flush
            # The parent already landed: an agent whose transcript vanished
            # or would not read is that agent's failure, reported here, not
            # an exception that unwinds the host's bookkeeping after the save.
            # BaseException (a cancel from the host's own deadline) still goes.
            log(f"agent {agent.agent_id} not sent ({type(e).__name__}: {e}); "
                "the next flush tries again")
            ok = False
        if not ok:
            report.failed.append(agent.agent_id)
            report.deferred = len(todo) - index
            return report
        # The session's own id, short: "cursor-<uuid>" names "<uuid>"[:8].
        short = parent_conv_id.rsplit("-", 4)[0].rsplit("-", 1)[-1][:8] \
            if parent_conv_id.count("-") >= 4 else parent_conv_id[:8]
        log(f"agent {agent.agent_id} sent, linked to session {short}")
        sent[agent.agent_id] = sent_key(agent, details)
        report.sent.append(agent.agent_id)
        _publish(cache_path, sent)
    return report
