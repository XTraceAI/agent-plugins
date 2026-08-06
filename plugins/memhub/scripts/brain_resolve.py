#!/usr/bin/env python3
"""Find the repo's agent brain on the server, once, and cache it.

``room_map`` routes every writer to one brain — but only once its cache holds an
id, and until now the only things that filled it were ``/memhub:onboard`` and
``/memhub:spec init``. So a user who never ran either had every automatic
capture land in personal memory even when their team's repo brain existed. The
plugin looked like it was working; the brain just stayed empty.

This closes that: on a cache miss the capture path asks the server once, matches
the repo's canonical room name, and writes the id back. Every later flush is a
local lookup again.

**Resolve, never create.** A brain is team-visible — teammates see it appear and
it shapes where memory lands. A background hook firing after a turn is the wrong
place to make that decision on someone's behalf, so an absent brain stays absent
and capture continues to personal memory exactly as before. Creating one remains
an explicit ``/memhub:onboard``.

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
    write_room,
)


def _brains_from(result) -> list[dict]:
    """Pull the brain list out of an MCP tool result, tolerantly.

    The payload arrives as ``structuredContent`` or as JSON in a text block,
    and FastMCP sometimes wraps a return in ``{"result": …}``. None of that is
    worth failing a capture over, so anything unrecognised yields no brains and
    the caller treats it as "not found".
    """
    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict) and "agent_brains" not in payload \
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
    if not isinstance(payload, dict):
        return []
    for key in ("agent_brains", "brains", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [b for b in value if isinstance(b, dict)]
    return []


async def resolve_repo_brain(session, cwd, env: str) -> dict | None:
    """Return this repo's cached room, resolving it from the server if needed.

    ``session`` is an already-initialised MCP ``ClientSession`` — the caller is
    mid-flush and has one open, so this costs one extra tool call rather than a
    second connection, and only on a cache miss.

    Never raises: a capture hook must not fail because a lookup did. Any problem
    resolves to "no room", which is the behaviour that existed before this
    function.
    """
    room = read_room(cwd, env)
    if room or not resolve_due(cwd, env):
        return room

    name = room_name(cwd)
    if not name:
        return None

    try:
        result = await session.call_tool("list_agent_brains", arguments={})
        if getattr(result, "isError", False):
            return None
        matches = []
        for brain in _brains_from(result):
            # Exact match, and only on the id being a usable string — a
            # malformed row must not become the routing target.
            if brain.get("name") != name:
                continue
            brain_id = brain.get("agent_brain_id") or brain.get("id")
            if isinstance(brain_id, str) and brain_id:
                matches.append(brain_id)

        if len(matches) == 1:
            write_room(matches[0], name=name, env=env)
            return {"brain_id": matches[0]}

        if len(matches) > 1:
            # Duplicate rooms for one repo do happen, and picking whichever the
            # listing returned first would route this repo's memory into an
            # arbitrary one of them — invisibly, and differently for different
            # teammates. Ambiguity is not something a background hook should
            # resolve by guessing. Capture continues to personal memory and the
            # lookup stays DUE (no miss recorded), so merging the duplicates
            # takes effect on the next flush rather than after a TTL.
            print(f"[memhub] {len(matches)} agent brains are named {name!r} — "
                  "cannot tell which is the repo's room, so this session is "
                  "not routed to one. Merge or rename the duplicates.")
            return None
        # Looked, found nothing. Remember that so the next turn does not ask
        # again; the entry carries no brain_id, so routing is unchanged.
        write_miss(cwd, env)
    except Exception:  # noqa: BLE001 — capture must never fail on a lookup
        return None
    return None
