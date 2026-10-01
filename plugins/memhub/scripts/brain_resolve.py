#!/usr/bin/env python3
"""Find the repo's agent brain on the server, once, and cache it.

``room_map`` routes every ARTIFACT writer to one brain — but only once its cache
holds an id, and the only things that fill it deliberately are
``/memhub:onboard`` and ``/memhub:spec init``.

On a cache miss an artifact writer (``save_artifact.py``, the ``.md``
auto-capture) asks the server once, matches the repo's canonical room name, and
writes the id back. Every later save is a local lookup again. Session capture
never calls this: sessions go to the author's personal memory, never to a brain
(ENG-1172).

**Resolve, never create.** A brain is team-visible — teammates see it appear and
it shapes where memory lands. A background hook firing after a turn is the wrong
place to make that decision on someone's behalf, so an absent brain stays absent
and the artifact stays personal. Creating one remains an explicit
``/memhub:onboard``.

**Exact name match only.** The room name is ``Repo: <org>/<name>`` (see
``room_map.room_name``), derived from the git remote. Fuzzy matching here would
silently route a session into a brain that merely looked similar — worse than
not routing at all, because it is invisible and lands teammate-visible content
somewhere nobody expects.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from room_map import (  # noqa: E402
    read_room,
    resolve_due,
    room_name,
    write_miss,
    write_probe_backoff,
    write_room,
)


# What a backend says when the id we sent is not a brain it holds. Matched on
# the message text because an MCP tool error carries no code — ``isError`` plus
# prose is the whole protocol here. Substring and case-folded so neither the
# server's wrapper prefix ("Error executing tool import_conversation: …") nor a
# reworded sentence silently switches the fallback off.
_MISSING_BRAIN = "agent brain not found"


def is_missing_brain(texts) -> bool:
    """True when a tool error says the brain we routed to does not exist.

    Accepts a single message or the list of text blocks off an MCP result, so
    callers can pass whatever they already extracted.

    Worth being precise about what this does NOT cover: an unreachable server,
    a rejected token, a malformed payload. Those say nothing about the room, and
    treating them as "the brain is gone" would throw away a good cache entry
    over a transient outage. Only this one sentence licenses forgetting.
    """
    if texts is None:
        return False
    if isinstance(texts, str):
        texts = [texts]
    return any(_MISSING_BRAIN in (t or "").lower() for t in texts)


def _payload(result, expected: str) -> dict:
    """Pull the JSON body out of an MCP tool result, tolerantly.

    The payload arrives as ``structuredContent`` or as JSON in a text block,
    and FastMCP sometimes wraps a return in ``{"result": …}`` — ``expected`` is
    the key that tells those two apart. None of this is worth failing a capture
    over, so anything unrecognised yields ``{}`` and the caller treats it as
    "not found".
    """
    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict) and expected not in payload \
            and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            try:
                payload = json.loads(text)
                break
            except json.JSONDecodeError:
                continue
    return payload if isinstance(payload, dict) else {}


def _brains_in(payload: dict) -> list[dict]:
    """The brain rows out of an already-unwrapped listing payload."""
    for key in ("agent_brains", "brains", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [b for b in value if isinstance(b, dict)]
    return []


def _brains_from(result) -> list[dict]:
    """The brain list out of a ``list_agent_brains`` result, tolerantly."""
    return _brains_in(_payload(result, "agent_brains"))


def _repo_slug(name: str) -> str:
    """``"Repo: XTraceAI/agent-plugins"`` → ``"XTraceAI/agent-plugins"`` — what
    ``list_agent_brains(repo=…)`` takes."""
    return name.split(":", 1)[1].strip() if name.startswith("Repo:") else name


async def resolve_repo_brain(session, cwd, env: str) -> dict | None:
    """Return this repo's cached room, resolving it from the server if needed.

    ``session`` is an already-initialised MCP ``ClientSession`` — the caller is
    mid-flush and has one open, so this costs one extra tool call rather than a
    second connection, and only on a cache miss.

    One call: ``list_agent_brains(repo="<org>/<name>")``. The server resolves
    the repo's brain across every org the caller can read — the per-org
    ``list_orgs`` → ``list_agent_brains(org_id=…)`` walk this replaced is gone
    with ``list_agent_brains``'s ``org_id`` parameter, and a brain's org is
    derived from its id at write time, so a room no longer needs its org to be
    usable. The org is still recorded when the row carries one.

    Never raises: a capture hook must not fail because a lookup did. Any problem
    resolves to "no room", which is the behaviour that existed before this
    function.
    """
    room = read_room(cwd, env)
    # ``resolve_due`` is consulted even when a room is already cached, because
    # an entry written without its org is re-asked on a rate-limited clock
    # (``room_map.resolve_due``); guarding on ``room`` alone would skip that.
    if not resolve_due(cwd, env):
        return room

    name = room_name(cwd)
    if not name:
        return room

    try:
        try:
            result = await session.call_tool(
                "list_agent_brains", arguments={"repo": _repo_slug(name)})
        except Exception:  # noqa: BLE001
            result = None
        if result is None or getattr(result, "isError", False):
            # The server did not answer, so nothing was learned about the
            # room: not "found", not "absent". A short backoff retries in
            # minutes rather than branding the repo room-less for a day or
            # re-asking every turn.
            write_probe_backoff(cwd, env)
            return room

        matches: list[tuple[str, str | None]] = []
        for brain in _brains_in(_payload(result, "agent_brains")):
            # Exact name match still, even though the server filtered by repo:
            # a backend that did not honour ``repo`` would hand back every
            # brain, and routing on "the one row that came back" would then
            # send this repo's memory into whatever brain was newest. A
            # malformed row (non-string id) is never a routing target either.
            if brain.get("name") != name:
                continue
            brain_id = brain.get("agent_brain_id") or brain.get("id")
            if isinstance(brain_id, str) and brain_id:
                org_id = brain.get("org_id")
                matches.append((brain_id, org_id if isinstance(org_id, str) and org_id else None))

        distinct = {bid for bid, _ in matches}
        if len(distinct) == 1:
            # One brain, possibly listed more than once (shared into several
            # places). Prefer a sighting that names its org.
            brain_id = next(iter(distinct))
            org_id = next((o for b, o in matches if b == brain_id and o), None)
            write_room(brain_id, name=name, env=env, org_id=org_id)
            return {"brain_id": brain_id, **({"org_id": org_id} if org_id else {})}

        if len(distinct) > 1:
            # Duplicate rooms for one repo do happen, and picking whichever the
            # listing returned first would route this repo's memory into an
            # arbitrary one of them — invisibly, and differently for different
            # teammates. Ambiguity is not something a background hook should
            # resolve by guessing. Artifacts stay personal and the lookup stays
            # DUE (no miss recorded), so merging the duplicates takes effect on
            # the next save rather than after a TTL.
            print(f"[memhub] {len(distinct)} agent brains are named {name!r} — "
                  "cannot tell which is the repo's room, so this repo is "
                  "not routed to one. Merge or rename the duplicates.")
            # ``room``, not None: on a re-resolution this repo may already have
            # a working cached id, and newly-created duplicates elsewhere must
            # not un-route it.
            return room
        # Found nothing, and the server answered. Remember that so the next
        # save does not ask again; the entry carries no brain_id, so routing is
        # unchanged.
        write_miss(cwd, env)
    except Exception:  # noqa: BLE001 — capture must never fail on a lookup
        return room
    return room
