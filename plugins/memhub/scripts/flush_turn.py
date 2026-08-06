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

Auth = the SAME OAuth the /mcp connector uses (shared ``_memhub_auth``), non
interactive: a per-turn background hook must never pop a browser.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stdlib-only and side-effect free, so it imports at module scope like the
# rest of the cursor/tail logic and stays testable under a bare python3.
from transcript_filter import drop_command_wrappers  # noqa: E402

# ``_memhub_auth`` pulls in the mcp SDK, so it is imported lazily inside
# :func:`_flush` rather than at module scope. That keeps this module importable
# under a bare python3 — which is what lets the cursor/tail/lock logic, where
# the silent failures live, be tested without the dependency.

# The MCP SDK logs the OAuth flow's exception (with traceback) before it
# propagates; a per-turn background hook must stay quiet.
logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)

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
    "permission-mode", "ai-title", "file-history-snapshot",
    "file-history-delta",
})


def _log(msg: str) -> None:
    print(f"[memhub-turn] {msg}")


# ── cursor ────────────────────────────────────────────────────────────

def _read_state(session_id: str) -> dict:
    try:
        state = json.loads((STATE_DIR / f"{session_id}.json").read_text())
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = _read_state(session_id)
    state.update(fields)
    state["at"] = time.time()
    tmp = STATE_DIR / f"{session_id}.json.tmp"
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_DIR / f"{session_id}.json")


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


def _title(records: list[dict]) -> str | None:
    """The session title Claude Code generated, if this delta carries one.

    Claude Code writes its own title as an ``ai-title`` record (``aiTitle``),
    and regenerates it as the session develops — so the LAST one wins. Without
    this the automatic capture paths import every session unnamed, and a
    sessions list reads as a wall of untitled rows.

    Harvested even from deltas we do not send: ``ai-title`` is an inert
    record type, so the title often arrives in a batch that is consumed
    without a server call. Remembering it there is what makes it available to
    the next real flush.
    """
    title = None
    for r in records:
        if isinstance(r, dict) and r.get("type") == "ai-title":
            value = (r.get("aiTitle") or "").strip()
            if value:
                title = value
    return title


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
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from _memhub_auth import resolve_url_and_auth
    from brain_resolve import resolve_repo_brain
    from room_map import env_for_url

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
    sendable = drop_command_wrappers(records)

    if not sendable or all(
        isinstance(r, dict) and r.get("type") in _INERT_RECORD_TYPES
        for r in sendable
    ):
        # The title usually arrives in exactly this kind of batch, so read it
        # before dropping the records on the floor.
        inert_title = _title(records)
        if inert_title:
            _save_state(session_id, offset=consumed, title=inert_title)
        else:
            _save_state(session_id, offset=consumed)
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
    title = _title(records) or state.get("title") or None
    url, headers, auth = resolve_url_and_auth(interactive=False)
    # Resolved AFTER the url — prod and staging hold different brain ids for the
    # same repo. Only when the TRANSCRIPT said where it ran: a hook can fire
    # from a different repo than the session's, and an unknown origin must
    # degrade to personal memory rather than guess a room.
    env = env_for_url(url)

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
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
            if namespace:
                arguments["namespace"] = namespace
            if title:
                # Re-sent on every flush so a regenerated title updates the
                # conversation rather than sticking at whatever the first
                # turn happened to be called.
                arguments["title"] = title
            res = await session.call_tool("import_conversation", arguments=arguments)

            # MCP signals tool failure via isError + a message, NOT an
            # exception. Without this the cursor would advance past records the
            # server rejected, losing them permanently.
            texts = [t for t in (getattr(b, "text", None)
                                 for b in getattr(res, "content", []) or []) if t]
            if getattr(res, "isError", False):
                _log(f"flush FAILED: {(texts[0] if texts else 'no detail')[:200]}")
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
                _log(f"response unrecognized: {(texts[0] if texts else '')[:120]!r}")
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
                _save_state(session_id, unsupported=True)
                return

            # Committed server-side — only now is it safe to move the cursor.
            _save_state(session_id, offset=consumed,
                        last_uuid=out.get("ack_through"), cwd=cwd,
                        namespace=namespace, title=title)
            # Sent count, not read count — and the byte span stays the READ
            # span, because that is what the cursor just advanced past. The
            # filtered records are the difference between the two, so showing
            # it makes an under-sent delta diagnosable from the log alone.
            filtered = len(records) - len(sendable)
            _log(f"+{len(sendable)} rec ({consumed - offset}B) "
                 + (f"filtered={filtered} " if filtered else "")
                 + f"new={out.get('records_new')} pending={out.get('pending')} "
                 f"draining={out.get('draining')}")


def _auth_required(e: BaseException) -> bool:
    """True if NonInteractiveAuthRequired is anywhere in the exception tree.

    The MCP client runs auth inside anyio task groups, so our raise can surface
    wrapped in ExceptionGroups or as a __cause__.

    The import is lazy and guarded: when the mcp SDK itself is what failed to
    load, there is no auth class to compare against and the answer is simply no.
    """
    try:
        from _memhub_auth import NonInteractiveAuthRequired
    except Exception:  # noqa: BLE001 — the SDK is what's missing
        return False

    seen: set[int] = set()
    stack: list[BaseException] = [e]
    while stack:
        exc = stack.pop()
        if id(exc) in seen:
            continue
        seen.add(id(exc))
        if isinstance(exc, NonInteractiveAuthRequired):
            return True
        stack.extend(getattr(exc, "exceptions", ()) or ())
        for link in (exc.__cause__, exc.__context__):
            if link is not None:
                stack.append(link)
    return False


def main() -> int:
    lock_fd: int | None = None
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
        elif _auth_required(e):
            _log("no cached OAuth token; run /memhub:import-session once "
                 "(or set MEMHUB_TOKEN) to enable per-turn capture — skipping")
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
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
