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
arrive in order, so an atomic lockfile keeps two hooks from overlapping when one
turn finishes before the previous flush has returned. Losing that race is not an
error: the cursor did not move, so the next turn's flush carries both.

Discipline mirrors the other capture hooks: THIS SCRIPT NEVER FAILS LOUDLY.
Any error exits 0 quietly — memory capture must never disturb the session.

Auth = the SAME OAuth the /mcp connector uses (shared ``_memhub_auth``), non
interactive: a per-turn background hook must never pop a browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ``_memhub_auth`` pulls in the mcp SDK, so it is imported lazily inside
# :func:`_flush` rather than at module scope. That keeps this module importable
# under a bare python3 — which is what lets the cursor/tail/lock logic, where
# the silent failures live, be tested without the dependency.

# The MCP SDK logs the OAuth flow's exception (with traceback) before it
# propagates; a per-turn background hook must stay quiet.
logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"
# Must match turn_flush_prefilter._LOCK_STALE_S.
_LOCK_STALE_S = 360


def _log(msg: str) -> None:
    print(f"[memhub-turn] {msg}")


# ── cursor ────────────────────────────────────────────────────────────

def _read_cursor(session_id: str, size: int) -> int:
    """Byte offset to resume from — 0 whenever the cursor cannot be trusted.

    A file SMALLER than the cursor means the transcript was rewritten under us,
    so the offset points into different content and every byte must be re-sent.
    Returning 0 is always safe (the server dedups); returning a stale offset
    would skip records permanently.
    """
    try:
        state = json.loads((STATE_DIR / f"{session_id}.json").read_text())
        offset = int(state.get("offset", 0))
    except (OSError, ValueError, TypeError):
        return 0
    return offset if 0 <= offset <= size else 0


def _write_cursor(session_id: str, offset: int, last_uuid: str | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"{session_id}.json.tmp"
    # Written atomically: a torn cursor read as a larger offset than was
    # actually shipped would skip records for good.
    tmp.write_text(json.dumps({
        "offset": offset, "last_uuid": last_uuid, "at": time.time(),
    }))
    tmp.replace(STATE_DIR / f"{session_id}.json")


# ── lock ──────────────────────────────────────────────────────────────

def _acquire(session_id: str) -> Path | None:
    """Atomically claim this session's flush slot, or return None.

    ``O_CREAT | O_EXCL`` is the actual guard — the prefilter's check is only an
    optimisation to avoid paying for a process spawn we would then discard.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = STATE_DIR / f"{session_id}.lock"
    payload = json.dumps({"pid": os.getpid(), "at": time.time()})
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Reclaim only if genuinely abandoned — a crashed flush must not wedge
        # capture for the rest of the session.
        try:
            held = json.loads(lock.read_text())
            if time.time() - float(held.get("at", 0)) <= _LOCK_STALE_S:
                return None
        except (OSError, ValueError, TypeError):
            pass
        try:
            lock.write_text(payload)
            return lock
        except OSError:
            return None
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    return lock


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
    from room_map import env_for_url, read_room

    size = os.path.getsize(transcript_path)
    offset = _read_cursor(session_id, size)
    records, consumed = _read_tail(transcript_path, offset)
    if not records:
        return

    cwd, namespace = _namespace(records)
    url, headers, auth = resolve_url_and_auth(interactive=False)
    # Read AFTER the url resolves — prod and staging hold different brain ids
    # for the same repo. Only when the TRANSCRIPT said where it ran: a hook can
    # fire from a different repo than the session's, and an unknown origin must
    # degrade to personal memory rather than guess a room.
    room = read_room(cwd, env_for_url(url)) if cwd else None

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            arguments = {
                "messages": records,
                "conversation_id": session_id,
                "source_platform": "claude",
                # The whole point: durable on arrival, extracted in batches.
                "flush": "auto",
            }
            if room:
                arguments["agent_brain_id"] = room["brain_id"]
            if namespace:
                arguments["namespace"] = namespace
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

            # Committed server-side — only now is it safe to move the cursor.
            _write_cursor(session_id, consumed, out.get("ack_through"))
            _log(f"+{len(records)} rec ({consumed - offset}B) "
                 f"new={out.get('records_new')} pending={out.get('pending')} "
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
    lock: Path | None = None
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = (hook_input.get("session_id") or "").strip()
        transcript_path = (hook_input.get("transcript_path") or "").strip()
        if not session_id or not transcript_path \
                or not Path(transcript_path).exists():
            return 0
        lock = _acquire(session_id)
        if lock is None:
            return 0  # a flush is already in flight; its successor carries ours
        asyncio.run(_flush(session_id, transcript_path))
    # BaseException, not Exception: anyio mixes CancelledError into task
    # groups, producing a BaseExceptionGroup that an Exception handler would
    # miss — killing the hook with a traceback in the user's session.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        if _auth_required(e):
            _log("no cached OAuth token; run /memhub:import-session once "
                 "(or set MEMHUB_TOKEN) to enable per-turn capture — skipping")
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass  # stale-reclaim handles it
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
