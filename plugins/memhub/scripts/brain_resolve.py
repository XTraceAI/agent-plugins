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

import asyncio
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


async def _org_ids(session) -> list[tuple[str, str | None]]:
    """Every org this account can act in as ``(id, name)``, default first.

    The NAME rides along so a listing can be checked against the scope the
    server says it applied — see the scope guard in :func:`resolve_repo_brain`.

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
        rows = [
            (o["org_id"], o.get("name"), bool(o.get("is_default")))
            for o in orgs
            if isinstance(o, dict)
            and isinstance(o.get("org_id"), str) and o["org_id"]
        ]
        # Default first: it decides which org is RECORDED for a room visible
        # from more than one, never which brain wins.
        return ([(i, n) for i, n, d in rows if d]
                + [(i, n) for i, n, d in rows if not d])
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
        # ``[(None, None)]`` keeps a single default-org listing when
        # ``list_orgs`` is unavailable, exactly as before.
        scopes: list[tuple[str | None, str | None]] = known_orgs or [(None, None)]

        # Two different kinds of "not everything was seen", and they carry
        # different weight.
        #
        # ``listings_ok`` — every scope this pass DID try answered. A listing
        # that errored leaves a known hole: the room, or the second half of a
        # duplicate, could be in exactly the org that failed. Deciding anything
        # on that is deciding on missing evidence.
        #
        # ``known_orgs`` empty means ``list_orgs`` was unavailable, so there is
        # only ever one scope to try and no way to learn of others. That is not
        # a hole this pass can close by retrying — it is simply the pre-org
        # behaviour, and refusing to route there would break room routing
        # entirely on any backend that cannot enumerate orgs. So a clean
        # default-org answer is still acted on; it just cannot be recorded with
        # an org, which leaves the entry due for a rate-limited upgrade later.
        listings_ok = True

        # Concurrently: every org must be listed before deciding, and the calls
        # are independent, so paying for them one after another would put N
        # sequential round trips inside a per-turn flush. Order still comes
        # from ``scopes``, which is what decides the org recorded.
        async def _list(org_id: str | None):
            args = {"org_id": org_id} if org_id else {}
            return await session.call_tool("list_agent_brains", arguments=args)

        results = await asyncio.gather(
            *(_list(org_id) for org_id, _ in scopes), return_exceptions=True,
        )

        matches: list[tuple[str, str | None]] = []
        for (org_id, org_name), result in zip(scopes, results):
            if isinstance(result, BaseException) \
                    or getattr(result, "isError", False):
                listings_ok = False
                continue
            payload = _payload(result, "agent_brains")
            # The server echoes the scope it applied. Checking it means the
            # recorded org is confirmed by the responder rather than assumed
            # from the request — the brain rows themselves carry no org, so
            # this echo is the only confirmation available. A mismatch means
            # the listing was not scoped as asked, so its contents say nothing
            # about that org and are dropped rather than mis-attributed.
            applied = (payload.get("scope") or {}).get("org_name") \
                if isinstance(payload.get("scope"), dict) else None
            if org_name and applied and applied != org_name:
                listings_ok = False
                continue
            # A scope was asked for but none came back: the brains are real,
            # but WHICH org holds them is unconfirmed. Record the match without
            # an org rather than attributing it to the org we happened to ask
            # about. That degrades to the pre-org behaviour — routing still
            # works, the entry is simply org-less and due for a rate-limited
            # upgrade — instead of either inventing an attribution or refusing
            # to resolve at all, which would break every backend that does not
            # echo a scope.
            attributed = org_id if (applied or not org_name) else None
            for brain in _brains_in(payload):
                # Exact match, and only on the id being a usable string — a
                # malformed row must not become the routing target.
                if brain.get("name") != name:
                    continue
                brain_id = brain.get("agent_brain_id") or brain.get("id")
                if isinstance(brain_id, str) and brain_id:
                    matches.append((brain_id, attributed))

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

        if len(distinct) == 1 and listings_ok:
            # One brain, possibly visible from several orgs (a shared room).
            # Prefer a sighting whose org the server CONFIRMED, then fall back
            # to the first. Taking ``matches[0]`` blindly would throw away a
            # confirmed org whenever the default-org listing happened to be the
            # one without a scope echo — caching the entry org-less, and so
            # leaving it due for a re-probe it did not need.
            brain_id = next(iter(distinct))
            org_id = next(
                (o for b, o in matches if b == brain_id and o),
                matches[0][1],
            )
            write_room(brain_id, name=name, env=env, org_id=org_id)
            return {"brain_id": brain_id, **({"org_id": org_id} if org_id else {})}

        if not listings_ok:
            # A match found while some org could not be listed is not the same
            # fact as a match found across all of them: the org that failed is
            # precisely where a second brain of the same name would sit, so
            # committing here would cache an arbitrary pick for a day and
            # reintroduce the ambiguity this function refuses to guess at. The
            # zero-match case lands here too — "not found" is equally unsafe
            # when part of the search did not happen.
            #
            # Nothing is recorded but a short backoff, so the decision is
            # retried in minutes rather than branded for a day or re-asked
            # every turn.
            write_probe_backoff(cwd, env)
            return room

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
        # Found nothing, and every scope tried answered. Remember that so the
        # next turn does not ask again; the entry carries no brain_id, so
        # routing is unchanged.
        if known_orgs:
            write_miss(cwd, env)
        else:
            # ...but only the DEFAULT org could be tried, because ``list_orgs``
            # was unavailable. A day-long "this repo has no room" is too strong
            # a conclusion to draw from a search that could not see other orgs,
            # and both deployed backends do expose ``list_orgs`` — so this is a
            # transient state, and minutes is the right retry, not a day.
            write_probe_backoff(cwd, env)
    except Exception:  # noqa: BLE001 — capture must never fail on a lookup
        return room
    return room
