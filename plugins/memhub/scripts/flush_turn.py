#!/usr/bin/env python3
"""Per-turn session flush — fired by the Stop hook after every assistant turn.

Ships only the transcript BYTES WRITTEN SINCE THE LAST SUCCESSFUL FLUSH, using
``flush="auto"`` so the server stores each turn durably on arrival but extracts
only once the accumulated span is batch-sized. Extracting per turn would hand
the batch extractor a 2-5 event fragment and shred the episode boundaries it
derives; buffering server-side keeps episodes whole while making each turn
durable the moment it happens.

**Why a byte cursor and not "re-send the transcript".** Re-sending the whole
file every turn needs no client state, and the server's uuid watermark makes it
correct — but it re-uploads the entire session on each turn, so total upload
grows with the SQUARE of the session length. On a long coding session that is
gigabytes to say what a few kilobytes would. So the client sends its delta and
the server holds what it has not yet extracted.

**The cursor advances only on success.** Every uncertainty resolves toward
re-sending: no cursor, a shrunken file, a failed call, an ambiguous response.
The server dedups incoming records against (extracted ∪ buffered), so
over-sending costs bandwidth while under-sending leaves a gap nothing will ever
notice. That asymmetry is the whole reason this file is written the way it is.

**One flush per session at a time.** The server expects a session's turns to
arrive in order, so an advisory ``flock`` keeps two hooks from overlapping when
one turn finishes before the previous flush has returned. Losing that race is
not an error: the cursor did not move, so the next turn's flush carries both.

Discipline mirrors the other capture hooks: THIS SCRIPT NEVER FAILS LOUDLY.
Any error exits 0 quietly — memory capture must never disturb the session.

Auth = the plugin's OWN token cache (shared ``_memhub_auth``) — a different
store from the /mcp connector's, so being connected in /mcp does NOT mean this
hook can authenticate. Non-interactive: a per-turn background hook must never
pop a browser, so it can only consume a token ``/memhub:login`` already minted.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stdlib-only and side-effect free, so it imports at module scope like the
# rest of the cursor/tail logic and stays testable under a bare python3.
from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)
from redact import redact_records, redact_text  # noqa: E402
from transcript_filter import drop_command_wrappers  # noqa: E402

# All at module scope now. These used to be deferred into :func:`_flush`
# because ``_memhub_auth`` dragged in the mcp SDK and this module has to stay
# importable under a bare python3 — that is what lets the cursor/tail/lock
# logic, where the silent failures live, be tested without the dependency.
# Nothing here needs the SDK any more, so the indirection went with it.
import atomic_write  # noqa: E402
import mcp_http  # noqa: E402
from _memhub_auth import resolve_bearer  # noqa: E402
from brain_resolve import is_missing_brain, resolve_repo_brain  # noqa: E402
from room_map import env_for_url, forget_room  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"

# Cap on one flush's whole round-trip. The flock is held for its duration and
# the prefilter skips while held, so this is also the longest a hung server
# can stall capture for the session.
_DEFAULT_FLUSH_TIMEOUT_S = 60.0


def _flush_timeout_s() -> float:
    """The round-trip cap, from the env with the default as a floor.

    Read at CALL time and never allowed to raise. Parsing this at import
    time meant a non-numeric or empty override crashed the module before the
    handler that keeps this hook quiet could run — a traceback in the user's
    session, which is the one thing this script must never produce.

    Zero or negative is rejected rather than honoured: it would time every
    flush out instantly, so the cursor would never advance and per-turn
    capture would be silently dead. ``MEMHUB_TURN_FLUSH=0`` is how you turn
    this off; a timeout of nothing is a misconfiguration, not an intent.
    """
    raw = (os.environ.get("MEMHUB_TURN_FLUSH_TIMEOUT_S") or "").strip()
    if not raw:
        return _DEFAULT_FLUSH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FLUSH_TIMEOUT_S
    return value if value > 0 else _DEFAULT_FLUSH_TIMEOUT_S

# UI bookkeeping the client writes for its own display: mode switches, the
# generated title, queue state. They carry no content and no ``message``, and
# a batch made only of them is REJECTED by the server — with no ``message``
# among the records it reads the batch as plain chat and fails role
# validation. Consuming them avoids a round-trip that could never succeed.
#
# ``attachment`` is deliberately absent from this set. It has no ``message``
# either, so an attachment-only delta is rejected the same way — but an
# attachment is real content (a pasted file, an image). Leaving the cursor
# pinned means the next turn re-sends it alongside the message records that
# make the batch valid. One wasted call beats dropping the file.
_INERT_RECORD_TYPES = frozenset({
    "mode", "last-prompt", "pr-link", "queue-operation",
    "permission-mode", "ai-title", "custom-title",
    "file-history-snapshot", "file-history-delta",
})


def _log(msg: str) -> None:
    print(f"[memhub-turn] {msg}")


# ── cursor ────────────────────────────────────────────────────────────

def _read_state(session_id: str) -> dict:
    try:
        state = json.loads((STATE_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_cursor(state: dict, size: int) -> int:
    """Byte offset to resume from — 0 whenever the cursor cannot be trusted.

    A file SMALLER than the cursor means the transcript was rewritten under us,
    so the offset points into different content and every byte must be re-sent.
    Returning 0 is always safe (the server dedups); returning a stale offset
    would skip records permanently.
    """
    try:
        offset = int(state.get("offset", 0))
    except (ValueError, TypeError):
        return 0
    return offset if 0 <= offset <= size else 0


def _save_state(session_id: str, **fields) -> None:
    """Merge ``fields`` into the session's state and write it atomically.

    MERGE, not replace: the session remembers several things resolved at
    different moments — the cursor, the repo it ran in, its namespace, its
    title — and a flush that only knows some of them must not erase the rest.

    Atomic, because a torn read that reported a larger offset than was actually
    shipped would skip those records for good.
    """
    state = _read_state(session_id)
    state.update(fields)
    state["at"] = time.time()
    # Atomic even though the flock makes this hook the only writer of this file
    # today: the state carries the cursor, and a torn one means re-sent or
    # skipped records. Cheap insurance against the next hook that needs to
    # write here — the SessionEnd backstop briefly did, and the lost-update it
    # caused is why it now keeps its own file.
    #
    # 0600 by default: not a secret exactly, but it holds the session title, the
    # repo path and server error text, none of which needs to be world-readable.
    atomic_write.publish(STATE_DIR / f"{session_id}.json", json.dumps(state))


# ── health breadcrumb ─────────────────────────────────────────────────
#
# Everything below exists because this hook is `async: true`, and Claude Code
# surfaces an async hook's stdout NOWHERE — not to the user, not to the agent.
# So every failure path here ended in a print nobody could read, and the state
# dir could not tell the two cases apart either: a session whose flushes all
# failed left a `.lock` and no `.json`, byte-identical to a session where the
# hook never ran. Per-turn capture died on prod when a token cached before the
# server advertised `offline_access` expired unrenewably, and it stayed dead
# for a day without a single visible symptom.
#
# The fix is to write the failure down where a SYNCHRONOUS hook can find it:
# `capture_health.py` reads these fields on SessionStart and reports them via
# `systemMessage`, the one channel that reaches the user.

def _mark_failure(session_id: str, reason: str, detail: str = "") -> None:
    """Record WHY this flush did not ship, for `capture_health.py` to surface.

    Never raises and never touches ``offset``: a breadcrumb must not be able to
    corrupt the cursor it is reporting on. ``reason`` is a stable slug the
    health check branches on; ``detail`` is truncated free text for the human.
    """
    try:
        _save_state(session_id, last_error=reason,
                    last_error_detail=detail[:200] or None,
                    last_error_at=time.time())
    except OSError:
        pass  # a breadcrumb is never worth failing the hook over


def _mark_success(session_id: str, **fields) -> None:
    """Save ``fields`` and clear any recorded failure in the same write.

    One write, not two: a success that advanced the cursor but left the error
    behind would have the health check crying wolf for the rest of the session,
    and a crash between two writes would make that permanent.
    """
    _save_state(session_id, last_ok_at=time.time(), last_error=None,
                last_error_detail=None, last_error_at=None, **fields)


# ── lock ──────────────────────────────────────────────────────────────

def _acquire(session_id: str) -> int | None:
    """Claim this session's flush slot via ``flock``, or return None.

    Returns the held file descriptor — the caller must keep it OPEN for the
    lock to hold, and closing it releases.

    ``flock`` rather than an ``O_EXCL`` lockfile because the kernel owns the
    lifetime: it releases on process exit however that happens, including a
    SIGKILL. A lockfile needs staleness heuristics to recover from a crashed
    flush, and reclaiming a stale one is inherently racy — two hooks can both
    judge it abandoned and both take it, which is exactly the overlap the lock
    exists to prevent. There is no such thing as a stale flock.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_DIR / f"{session_id}.lock",
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None  # another flush holds it; ours is redundant anyway
    return fd


# ── flush ─────────────────────────────────────────────────────────────

def _read_tail(transcript: str, offset: int) -> tuple[list[dict], int]:
    """Records appended since ``offset``, plus the offset actually consumed.

    Opened in binary and decoded per line so the returned offset is a true BYTE
    count — a character count would drift on any non-ASCII turn and silently
    mis-seek the next flush.

    The final line is routinely a PARTIAL write, because Claude Code is still
    appending while this runs. That is the expected case, not corruption: stop
    at the last complete line and leave the cursor before the partial one, so
    the next flush picks the record up whole.
    """
    records: list[dict] = []
    consumed = offset
    with open(transcript, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial trailing write — resume here next turn
            consumed += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # unparseable but complete: skip, keep the offset
    return records, consumed


def _titles(records: list[dict], state: dict) -> tuple[str | None, str | None]:
    """``(title_to_send, custom_title_to_remember)`` for this delta.

    Three sources, in strict precedence — see ``session_title`` for the
    measurements behind the order:

    1. the name the USER gave the session, which the client keeps re-emitting
       the stale generated title alongside, so it must win by TYPE rather than
       by whichever record came last;
    2. the name Claude Code generated, freshest first — it is regenerated as
       the session develops, so this delta's beats the remembered one;
    3. failing both, the session's first real prompt. Only a client that never
       writes a title record at all reaches here, which in practice means a
       headless run — without it those import unnamed.

    Each is remembered, so a delta that carries no title at all keeps sending
    the one already resolved rather than reverting to a fresh guess.

    Harvested even from deltas we do not send: the title records are inert, so
    a title often arrives in a batch that is consumed without a server call.
    Remembering it there is what makes it available to the next real flush.
    """
    custom = custom_title(records) or state.get("custom_title") or None
    generated = generated_title(records) or state.get("title") or None
    title = custom or generated or prompt_title(records) or None
    # Redacted HERE, at the source, and not at the send site. A title is derived
    # from the RAW records — the redaction downstream only covers `sendable` —
    # so a session whose first prompt is `export MEMHUB_TOKEN=mhk_…` would ship
    # its key as the conversation's NAME: the most visible field there is, and
    # metadata that redaction was supposed to have covered. Doing it here also
    # keeps the copy persisted into state clean, which matters because that copy
    # is re-sent on every later flush.
    return (redact_text(title) if title else None,
            redact_text(custom) if custom else None)


def _namespace(records: list[dict]) -> tuple[str | None, str | None]:
    """(cwd, git-remote basename) — the session's working-context scope.

    Resolved client-side from the transcript's cwd, never server-side: a
    worktree directory's basename would stamp a scope that HIDES directives
    from the canonical repo's recalls.
    """
    cwd = next((r.get("cwd") for r in records
                if isinstance(r, dict) and isinstance(r.get("cwd"), str)
                and r.get("cwd")), None)
    if not cwd:
        return None, None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return cwd, url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd, None


async def _flush(session_id: str, transcript_path: str) -> None:

    size = os.path.getsize(transcript_path)
    state = _read_state(session_id)
    offset = _read_cursor(state, size)
    records, consumed = _read_tail(transcript_path, offset)
    if not records:
        return

    # Only user / assistant / attachment records carry ``cwd`` — the UI sidecar
    # types (mode, last-prompt, ai-title, …) never do. A delta made up solely of
    # sidecars therefore resolves no cwd, and without the remembered one this
    # flush would route to personal memory AND key the conversation on the
    # un-namespaced source_id — splitting one session across two conversations,
    # half in the repo's room and half outside it. So the first flush that
    # resolves a cwd remembers it for the rest of the session.
    # Checked BEFORE resolving cwd, because resolving shells out to git and an
    # inert delta should cost nothing at all.
    # Slash-command bookkeeping never leaves the machine. Dropped from what is
    # SENT, not from what is read: the metadata harvest below still sees every
    # record, and the cursor still advances past these, because they are
    # deliberately never shipped rather than deferred.
    # Redact AFTER filtering and before anything leaves the machine. Applied to
    # what is SENT rather than what is read, so the cursor still advances past
    # a record whose secret was stripped — the alternative would pin the cursor
    # on any turn that mentioned a key and stall capture permanently.
    sendable = redact_records(drop_command_wrappers(records))

    if not sendable or all(
        isinstance(r, dict) and r.get("type") in _INERT_RECORD_TYPES
        for r in sendable
    ):
        # The title usually arrives in exactly this kind of batch, so read it
        # before dropping the records on the floor.
        inert_title, inert_custom = _titles(records, state)
        fields = {"offset": consumed}
        if inert_title:
            fields["title"] = inert_title
        if inert_custom:
            fields["custom_title"] = inert_custom
        # Plain ``_save_state``, NOT ``_mark_success`` — deliberately. This
        # branch returns above ``resolve_url_and_auth`` and never touches the
        # network, so reaching it says nothing about whether the server or the
        # credential is healthy. Clearing ``last_error`` here would retract a
        # real, still-unresolved failure on the strength of a purely local
        # no-op, and inert deltas are common enough (the title arrives in one)
        # that a broken session would routinely erase its own alarm. Only a
        # committed round-trip is evidence of recovery, which is why exactly
        # one call site clears the error.
        _save_state(session_id, **fields)
        return

    # Each falls back INDEPENDENTLY. Tying the namespace's fallback to the cwd's
    # left a real gap: a delta can carry a cwd while git resolution fails on it
    # (a timeout, or a checkout with no origin). Then cwd is set, the fallback is
    # skipped, and the namespace is silently None for that flush — so its
    # directives extract unscoped and are recalled in every repo, even though an
    # earlier flush had already resolved the name.
    cwd, namespace = _namespace(records)
    if not cwd:
        cwd = state.get("cwd") or None
    if not namespace:
        # Remembered, so this is a dict lookup rather than re-running git.
        namespace = state.get("namespace") or None

    # This delta's title if it carries one, else whatever we last saw.
    title, custom = _titles(records, state)
    # In a THREAD, because resolving can renew the token — two blocking urllib
    # calls, up to ~25s of socket timeout. `asyncio.wait_for` cannot cancel a
    # synchronous call, so run inline it would pin the event loop AND hold the
    # flock past the flush deadline, blocking every later turn's capture for
    # the session. Offloading is what makes the timeout mean anything here.
    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        # Not an error — the state a background hook must degrade quietly on.
        # Raised rather than returned so the one handler in main() records the
        # breadcrumb, keeping every failure path reported the same way.
        raise _NoCredential("no usable credential (key, token or cached login)")
    # Resolved AFTER the url — prod and staging hold different brain ids for the
    # same repo. Only when the TRANSCRIPT said where it ran: a hook can fire
    # from a different repo than the session's, and an unknown origin must
    # degrade to personal memory rather than guess a room.
    env = env_for_url(url)

    # No connection to open: the server is stateless, so a Session is just
    # the endpoint and the credential. Verified against the live server —
    # it negotiates no session id and does not require `initialize`, so this
    # is ONE round trip where the SDK did three.
    # Per call, and deliberately less than the whole flush budget: this hook
    # makes TWO calls on a cold cache — the room lookup and the import — so
    # granting each the full timeout lets a stalled lookup consume the budget
    # and the import, the only call that actually captures anything, never
    # happens. Half guarantees the second call still gets a turn.
    session = mcp_http.Session(url, bearer, timeout=_flush_timeout_s() / 2)
    # No `initialize` handshake: verified against the live server, a fresh
    # process can call a tool directly and get a result. Dropping it removes two
    # of the three round trips this hook used to make per turn.
    # Cached hit is a dict lookup; a miss asks the server once and
    # caches the answer, so this is not a per-turn round-trip.
    room = await resolve_repo_brain(session, cwd, env) if cwd else None
    arguments = {
        "messages": sendable,
        "conversation_id": session_id,
        "source_platform": "claude",
        # The whole point: durable on arrival, extracted in batches.
        "flush": "auto",
    }
    if room:
        arguments["agent_brain_id"] = room["brain_id"]
        # The org that OWNS the room, when it is not the caller's
        # default. A brain resolves inside exactly ONE org, and the
        # default follows whichever org was last selected in the MemHub
        # app — so it changes under a running session. Sending the id
        # without its org is how every capture into such a room failed
        # with "Agent brain not found": silently, because this hook is
        # async and its output goes nowhere, and indistinguishably from
        # a deleted brain.
        if room.get("org_id"):
            arguments["org_id"] = room["org_id"]
    if namespace:
        arguments["namespace"] = namespace
    if title:
        # Re-sent on every flush so a regenerated title updates the
        # conversation rather than sticking at whatever the first
        # turn happened to be called.
        arguments["title"] = title
    # Transport failures are CLASSIFIED here, not left to the catch-all in
    # main(). Falling through to it recorded every one as reason="error" — a
    # permanent-looking breadcrumb — which is actively wrong for the two cases
    # that are not faults at all: a 429 is backpressure this design expects
    # (one seat's throughput, a fleet flushing every turn), and a 401 is a
    # revoked or lapsed credential whose fix is one command. Both were being
    # reported to the user as "the capture hook hit an unexpected error".
    #
    # The cursor is unmoved in every branch, so all of them retry next turn.
    async def _import(args: dict):
        """Send one import, classifying transport failures. None = already
        reported, and the caller must return without touching the cursor.

        A closure rather than the straight-line ladder it replaces because this
        hook can now send TWICE — once routed to the repo's room, and again
        unrouted when the server says that room does not exist. A 401 or a 429
        must be classified identically on both, and keeping one copy is what
        guarantees that; a second inline ladder is exactly how the two drift.
        """
        try:
            return await session.call_tool("import_conversation", arguments=args)
        except mcp_http.McpRateLimited as e:
            wait = f" (retry-after {e.retry_after:.0f}s)" if e.retry_after else ""
            _log(f"rate limited{wait} — the next turn retries (cursor unmoved)")
            _mark_failure(session_id, "rate_limited", str(e))
        except mcp_http.McpNoResponse as e:
            # Reached the server, it streamed, no answer came. That is a reply
            # we could not use — the same bucket as a body we could not read —
            # not a generic fault.
            _log(f"no response frame: {e}")
            _mark_failure(session_id, "unrecognized_response", str(e))
        except mcp_http.McpError as e:
            if e.status == 401:
                # Unauthenticated: no credential, or one the server won't
                # accept. /memhub:login mints a new one, so the advice
                # converges.
                _log("credential rejected; run /memhub:login — skipping")
                _mark_failure(session_id, "auth",
                              "server rejected the credential (401)")
            elif e.status == 403:
                # Authorized-but-forbidden. Re-logging in mints an equivalent
                # credential and changes NOTHING, so telling them to is the
                # same non-converging loop the `no_refresh` advice was fixed
                # for. The cause is scope or org access, and that is what to
                # name.
                _log("credential lacks permission (403) — check the key's "
                     "scopes and that it can reach this brain's org; skipping")
                _mark_failure(session_id, "forbidden", str(e))
            else:
                _log(f"transport error: {e}")
                _mark_failure(session_id, "error", str(e))
        return None

    def _texts(result) -> list[str]:
        return [t for t in (getattr(b, "text", None)
                            for b in getattr(result, "content", []) or []) if t]

    res = await _import(arguments)
    if res is None:
        return

    # MCP signals tool failure via isError + a message, NOT an
    # exception. Without this the cursor would advance past records the
    # server rejected, losing them permanently.
    texts = _texts(res)

    # A cached room the backend does not have must not cost the turn. Routing
    # already degrades to long-term memory when NO room is cached — but that
    # check reads the CACHE, not the server, so a present-but-wrong id sailed
    # past it and the flush simply died. That is how a `production` entry
    # holding a staging brain id took out per-turn capture and the SessionEnd
    # backstop together, for days: every turn re-sent an id the server had
    # already rejected, and `write_miss` refused to overwrite it, so nothing
    # could ever correct the cache. Forget the id — so the next turn resolves
    # honestly — and send again unrouted, which is where the no-room path
    # would have put this turn anyway.
    if getattr(res, "isError", False) and room and is_missing_brain(texts):
        _log(f"room {room['brain_id'][:8]} does not exist on this backend — "
             "dropping it from the cache and flushing to long-term memory")
        forget_room(cwd, env)
        room = None
        arguments.pop("agent_brain_id", None)
        arguments.pop("org_id", None)
        res = await _import(arguments)
        if res is None:
            return
        texts = _texts(res)

    if getattr(res, "isError", False):
        detail = (texts[0] if texts else "no detail")[:200]
        _log(f"flush FAILED: {detail}")
        _mark_failure(session_id, "server_rejected", detail)
        return

    out = getattr(res, "structuredContent", None)
    if isinstance(out, dict) and "conversation_id" not in out \
            and isinstance(out.get("result"), dict):
        out = out["result"]  # FastMCP wraps some returns
    if not isinstance(out, dict):
        for text in texts:
            try:
                out = json.loads(text)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(out, dict) or "conversation_id" not in out:
        # Unrecognized body: do NOT advance. Re-sending is free.
        detail = (texts[0] if texts else "")[:120]
        _log(f"response unrecognized: {detail!r}")
        _mark_failure(session_id, "unrecognized_response", detail)
        return

    # ``ack_through`` is only returned by a server that performed the
    # durable receive this hook depends on. Without it we are talking to
    # an older server that queues the import in the background and
    # treats every turn as an immediate extraction — per-turn LLM cost,
    # and the episode fragmentation the batching exists to avoid. Say so
    # loudly; silently doing the expensive wrong thing is worse than
    # noisy logs a user can act on.
    if "ack_through" not in out:
        # This server queues the import in the background instead of
        # committing it before replying, so a well-formed response
        # does NOT mean the records are durable — advancing on it
        # could drop them. It also extracts every turn immediately,
        # which is the cost this hook exists to avoid. Go dormant for
        # the session rather than pay for the wrong behaviour: the
        # commit/PR and SessionEnd hooks still capture it.
        _log("server has no per-turn support (no ack_through) — "
             "disabling per-turn flush for this session; commit/PR "
             "and session-end capture still apply. Upgrade the MemHub "
             "server to enable it.")
        # ``unsupported`` and NOT a failure breadcrumb. This is a
        # deliberate degrade, not a break: per-turn goes dormant while
        # the commit/PR and SessionEnd paths keep capturing, so there
        # is nothing the user must drop what they are doing to fix.
        #
        # It also cannot be retracted. Dormancy means no further flush
        # runs, so no success can ever clear a breadcrumb — and because
        # the condition is environmental, every NEW session rediscovers
        # it and warns again. That is a banner on every session start
        # for a day, about a known state with a working fallback, which
        # is precisely how a warning becomes wallpaper and stops being
        # read on the day it matters.
        #
        # Surfacing it properly needs a once-ever channel keyed by
        # server, not the per-session one; until then the log line
        # above records it.
        # Clears THIS path's stale error, and deliberately does NOT stamp
        # `last_ok_at`.
        #
        # Stamping one was a bug: since the health check retracts a failure when
        # any path reports success for the same session, a success recorded here
        # would silently retract a REAL failure the SessionEnd backstop had
        # recorded — reintroducing the invisible capture failure this whole
        # series exists to remove, and doing it from a branch that captured
        # nothing.
        #
        # Clearing the error is still right: dormancy means no later per-turn
        # flush runs to retract it, so an older error would be stranded forever.
        # But this branch speaks only for itself. It reached the server and got
        # an answer; it did not capture anything, so it is in no position to
        # vouch for another path.
        _save_state(session_id, unsupported=True, last_error=None,
                    last_error_detail=None, last_error_at=None)
        return

    # Committed server-side — only now is it safe to move the cursor.
    # ``custom_title`` is stored SEPARATELY from the title that was
    # sent, and only when there is one: it is the one source a later
    # delta must not be able to override, and merging a None over it
    # would let the next ``ai-title`` — which the client keeps
    # emitting with the pre-rename value — take the name back.
    _mark_success(session_id, offset=consumed,
                  last_uuid=out.get("ack_through"), cwd=cwd,
                  namespace=namespace, title=title,
                  **({"custom_title": custom} if custom else {}))
    # Sent count, not read count — and the byte span stays the READ
    # span, because that is what the cursor just advanced past. The
    # filtered records are the difference between the two, so showing
    # it makes an under-sent delta diagnosable from the log alone.
    filtered = len(records) - len(sendable)
    _log(f"+{len(sendable)} rec ({consumed - offset}B) "
         + (f"filtered={filtered} " if filtered else "")
         + f"new={out.get('records_new')} pending={out.get('pending')} "
         f"draining={out.get('draining')}")


class _NoCredential(RuntimeError):
    """No usable bearer. Replaces the SDK's NonInteractiveAuthRequired.

    A plain exception now, not something buried in an anyio task group, so the
    handler recognises it by type instead of walking an exception tree — which
    is what the SDK's wrapping forced.
    """


# `_auth_required` used to live here: thirty lines walking an exception tree
# for NonInteractiveAuthRequired, because the SDK raised it inside anyio task
# groups and it surfaced wrapped in ExceptionGroups or chained as __cause__.
# Resolving the credential ourselves means the miss is now a plain exception
# raised on our own stack, so `isinstance` is the whole check.


def main() -> int:
    lock_fd: int | None = None
    # Bound BEFORE the try so the handler can always write a breadcrumb. Reading
    # stdin or parsing it is itself a failure path, and a NameError raised from
    # inside the one handler that exists to keep this hook quiet would surface
    # the traceback it was written to prevent.
    session_id = ""
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = (hook_input.get("session_id") or "").strip()
        transcript_path = (hook_input.get("transcript_path") or "").strip()
        if not session_id or not transcript_path \
                or not Path(transcript_path).exists():
            return 0
        lock_fd = _acquire(session_id)
        if lock_fd is None:
            return 0  # a flush is already in flight; its successor carries ours
        # Bounded, because the lock is held for the whole round-trip and the
        # prefilter skips every later turn while it is held. Without a cap, one
        # hung request would stall capture for this session until the hook's own
        # 300s timeout — five minutes of turns silently not shipping. A minute
        # is far longer than a small delta needs and an order of magnitude
        # tighter than that. Timing out is safe: the cursor has not moved, so
        # the next turn re-sends.
        timeout_s = _flush_timeout_s()
        asyncio.run(asyncio.wait_for(
            _flush(session_id, transcript_path), timeout=timeout_s))
    # BaseException, not Exception: anyio mixes CancelledError into task
    # groups, producing a BaseExceptionGroup that an Exception handler would
    # miss — killing the hook with a traceback in the user's session.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            _log(f"timed out after {_flush_timeout_s():.0f}s — the next turn retries (cursor unmoved)")
            reason, detail = "timeout", f"no response in {_flush_timeout_s():.0f}s"
        elif isinstance(e, _NoCredential):
            _log("no usable credential; run /memhub:login "
                 "(or set MEMHUB_TOKEN) to enable per-turn capture — skipping")
            # The one failure the user must act on personally, and the one that
            # stays broken forever until they do: no retry can mint a token.
            reason, detail = "auth", "no usable cached OAuth token"
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
            reason, detail = "error", f"{type(e).__name__}: {e}"
        # Only with a session to file it under, and never at the cost of the
        # quiet exit — a breadcrumb that raised would defeat this whole handler.
        if session_id:
            try:
                _mark_failure(session_id, reason, detail)
            except BaseException:  # noqa: BLE001
                pass
    finally:
        if lock_fd is not None:
            # Closing releases the flock. The kernel would do this at exit
            # anyway; doing it here keeps the held window tight.
            try:
                os.close(lock_fd)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
