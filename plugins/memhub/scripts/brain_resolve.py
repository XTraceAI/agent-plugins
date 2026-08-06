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


def _brains_from(result) -> list[dict]:
    """The brain list out of a ``list_agent_brains`` result, tolerantly."""
    payload = _payload(result, "agent_brains")
    for key in ("agent_brains", "brains", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [b for b in value if isinstance(b, dict)]
    return []


async def _org_ids(session) -> list[str]:
    """Every org this account can act in, default first.

    Returns ``[]`` on any problem, which collapses resolution back to
    default-org-only behaviour — a lookup helper must never be the thing that
    fails a capture.
    """
    try:
        result = await session.call_tool("list_orgs", arguments={})
        if getattr(result, "isError", False):
            return []
        orgs = _payload(result, "orgs").get("orgs")
        if not isinstance(orgs, list):
            return []
        ids = [
            (o["org_id"], bool(o.get("is_default"))) for o in orgs
            if isinstance(o, dict)
            and isinstance(o.get("org_id"), str) and o["org_id"]
        ]
        # Default first: it is the one the room is usually in, so the search
        # stops on the first listing in the common case.
        return [i for i, d in ids if d] + [i for i, d in ids if not d]
    except Exception:  # noqa: BLE001
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
    # ``resolve_due`` is consulted even when a room is already cached, because
    # an entry written before rooms carried their org is present-but-unusable:
    # the id resolves in the wrong org at write time. Guarding on ``room`` alone
    # meant such an entry could never be upgraded — it short-circuited here
    # forever while every capture failed.
    if not resolve_due(cwd, env):
        return room

    name = room_name(cwd)
    if not name:
        return room

    try:
        # EVERY org, not just the default one. A brain is resolved inside
        # exactly one org, and the caller's default follows whichever org was
        # last selected in the MemHub app — so it changes under a running
        # session and is routinely NOT the org holding the repo's room. Listing
        # only the default made such a room invisible to resolution and, once
        # cached, unusable at write time: every capture failed with "Agent brain
        # not found", an error that reads like a deleted brain.
        #
        # Orgs are enumerated FIRST so the org holding the match is known and
        # can be cached WITH it — that pairing is what makes the entry usable
        # later. They come back default-first, which decides only which org is
        # RECORDED for a room visible in more than one; it never decides which
        # brain wins.
        known_orgs = list(await _org_ids(session))
        # ``[None]`` keeps a single default-org listing when ``list_orgs`` is
        # unavailable, exactly as before.
        org_ids: list[str | None] = known_orgs or [None]

        matches: list[tuple[str, str | None]] = []
        for org_id in org_ids:
            args = {"org_id": org_id} if org_id else {}
            result = await session.call_tool("list_agent_brains", arguments=args)
            if getattr(result, "isError", False):
                continue
            for brain in _brains_from(result):
                # Exact match, and only on the id being a usable string — a
                # malformed row must not become the routing target.
                if brain.get("name") != name:
                    continue
                brain_id = brain.get("agent_brain_id") or brain.get("id")
                if isinstance(brain_id, str) and brain_id:
                    matches.append((brain_id, org_id))

        # EVERY org is searched before deciding, deliberately — no early break
        # on the first org that has a hit. Stopping early would hide a genuine
        # cross-org ambiguity: two DIFFERENT brains sharing this repo's room
        # name in two orgs would resolve to whichever org happened to be
        # ordered first. That order comes from the default org, which follows
        # the last org selected in the MemHub app — so the routing target would
        # change when a user merely clicks around the UI, and two teammates
        # would send the same repo's memory to different brains. Ambiguity has
        # to be visible, not settled by a UI artifact.
        distinct = {bid for bid, _ in matches}

        if len(distinct) == 1:
            # One brain, possibly visible from several orgs (a shared room).
            # Record the FIRST org it was seen in — the list is default-first,
            # so that is the one nearest the user. Which org is recorded only
            # affects how the id is looked up later; it is the same brain
            # either way.
            brain_id, org_id = matches[0]
            write_room(brain_id, name=name, env=env, org_id=org_id)
            return {"brain_id": brain_id, **({"org_id": org_id} if org_id else {})}

        if len(distinct) > 1:
            # Duplicate rooms for one repo do happen, and picking whichever the
            # listing returned first would route this repo's memory into an
            # arbitrary one of them — invisibly, and differently for different
            # teammates. Ambiguity is not something a background hook should
            # resolve by guessing. Capture continues to personal memory and the
            # lookup stays DUE (no miss recorded), so merging the duplicates
            # takes effect on the next flush rather than after a TTL.
            print(f"[memhub] {len(distinct)} agent brains are named {name!r} — "
                  "cannot tell which is the repo's room, so this session is "
                  "not routed to one. Merge or rename the duplicates.")
            # ``room``, not None: on a re-resolution this repo may already have
            # a working cached id, and newly-created duplicates elsewhere must
            # not un-route it.
            return room
        # Looked, found nothing. Remember that so the next turn does not ask
        # again; the entry carries no brain_id, so routing is unchanged.
        #
        # ONLY when the search was complete. If ``list_orgs`` was unavailable
        # this pass looked at the default org alone, and a repo whose room
        # lives in another org would be branded room-less for the full
        # MISS_TTL_S — a transient outage silently sending a whole day of
        # sessions to personal memory. An incomplete look records nothing and
        # is simply retried; the per-turn cost of that only applies while the
        # server cannot answer, which is not a state to optimise for.
        if known_orgs:
            write_miss(cwd, env)
    except Exception:  # noqa: BLE001 — capture must never fail on a lookup
        return room
    return room
