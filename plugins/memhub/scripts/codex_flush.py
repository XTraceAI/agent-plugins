#!/usr/bin/env python3
"""Codex capture: hook-triggered flush of a Codex session into MemHub.

The Codex sibling of ``cursor_flush.py``, one seam simpler: rollouts are
append-only JSONL, so "anything new?" is a byte-size comparison rather than a
blob-set. Everything downstream is the shared machinery (``readers.codex``
transform, ``redact``, ``resolve_bearer``, ``brain_resolve``, ``room_map``,
``mcp_http`` — bare python3, no mcp SDK).

Codex clones Claude's hook contract (same ``hooks.json`` shape, same
``${CLAUDE_PLUGIN_ROOT}``), so the payload is EXPECTED Claude-shaped —
``session_id`` / ``transcript_path`` — but this script trusts nothing it
hasn't verified live: identity is taken from whichever of those fields is
present (the rollout filename carries the uuid), and a payload with neither
logs and exits rather than guessing ``latest`` (importing the wrong session
would fold-forward the wrong conversation's gist).

Every event flushes with server mode "now" — same reason as cursor_flush:
staging showed "auto"-buffered records being dedup-registered without
persisting, and Codex has no SessionEnd hook to guarantee a later drain.
Revert to boundary-only "now" when the backend folds dedup registration
into the drain.

Events wired (see hooks/codex-hooks.json, generated): ``Stop`` = turn
boundary, always flush on growth; ``PostToolUse`` = milestone commands only
(git commit / gh pr), so ordinary tool traffic stays quiet.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_write  # noqa: E402
import mcp_http  # noqa: E402
from _memhub_auth import resolve_bearer  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from readers import codex as codex_reader  # noqa: E402
from redact import redact_records  # noqa: E402
from room_map import env_for_url  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "codexflush"
FLUSH_TIMEOUT_S = 240.0

_MILESTONE_RE = re.compile(r"\bgit\b.*\bcommit\b|\bgh\b.*\bpr\b", re.S)


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [codex-flush] {msg}\n"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = STATE_DIR / "log"
        if log.exists() and log.stat().st_size > 256_000:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            log.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with open(log, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _read_state(sid: str) -> dict:
    try:
        return json.loads((STATE_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(sid: str, **fields) -> None:
    state = _read_state(sid)
    state.update(fields)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write.publish(STATE_DIR / f"{sid}.json", json.dumps(state))


def locate_rollout(payload: dict) -> tuple[Path | None, str | None]:
    """(rollout path, session uuid) from a hook payload — path preferred,
    bare session_id resolved through the reader, no fields → (None, None)."""
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        p = Path(tp.strip())
        if p.is_file():
            return p, codex_reader.rollout_uuid(p) or p.stem
    sid = payload.get("session_id") or payload.get("conversation_id")
    if isinstance(sid, str) and sid.strip():
        p, _err = codex_reader.locate(sid.strip())
        if p is not None:
            return p, sid.strip()
    return None, None


def _command_text(payload: dict) -> str:
    """The tool command as text — Codex sends list-form commands, Claude
    strings; the milestone gate only greps, so join and move on."""
    ti = payload.get("tool_input")
    cmd = (ti or {}).get("command") if isinstance(ti, dict) else None
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    if isinstance(cmd, str):
        return cmd
    return json.dumps(ti) if isinstance(ti, dict) else ""


def should_flush(event: str, payload: dict, state: dict, size: int) -> bool:
    """Pure gate. Growth is a precondition for every event; Stop is the turn
    boundary and always ships growth; PostToolUse ships only milestones."""
    if size <= (state.get("rollout_size") or 0):
        return False
    if event == "Stop":
        return True
    if event == "PostToolUse":
        return bool(_MILESTONE_RE.search(_command_text(payload)))
    return False


async def _flush(sid: str, rollout: Path, size: int) -> None:
    records, meta = codex_reader.to_canonical(rollout)
    sendable = redact_records(records)
    if not sendable:
        return

    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        _log("no usable credential — skipping (run /memhub:login)")
        _save_state(sid, last_error="no_credential")
        return
    env = env_for_url(url)
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)

    cwd = meta.get("cwd")
    room = await resolve_repo_brain(session, cwd, env) if cwd else None
    namespace = None
    if cwd:
        import subprocess
        try:
            out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=2)
            u = out.stdout.strip()
            if out.returncode == 0 and u:
                namespace = u.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        except (OSError, subprocess.SubprocessError):
            pass

    arguments = {
        "messages": sendable,
        "conversation_id": f"codex-{sid}",
        "source_platform": "claude",
        "flush": "now",
    }
    if room:
        arguments["agent_brain_id"] = room["brain_id"]
        if room.get("org_id"):
            arguments["org_id"] = room["org_id"]
    if namespace:
        arguments["namespace"] = namespace
    if meta.get("title"):
        arguments["title"] = meta["title"]

    try:
        await session.call_tool("import_conversation", arguments=arguments)
    except mcp_http.McpRateLimited as e:
        _log(f"rate limited: {e} — a later hook retries (state unmoved)")
        _save_state(sid, last_error="rate_limited")
        return
    except mcp_http.McpError as e:
        _log(f"import failed: {e}")
        _save_state(sid, last_error=str(e)[:200])
        return

    _save_state(sid, rollout_size=size, last_ok_at=time.time(), last_error=None)
    _log(f"flushed {len(sendable)} records → codex-{sid}"
         + (f" (room {room['brain_id'][:8]}…)" if room else " (personal)"))


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    rollout, sid = locate_rollout(payload)
    if rollout is None or not sid:
        _log(f"{event}: no session identity in payload — skipping")
        return 0

    try:
        size = rollout.stat().st_size
    except OSError as e:
        _log(f"{event}: rollout unreadable ({e}) — skipping")
        return 0

    if not should_flush(event, payload, _read_state(sid), size):
        return 0

    try:
        asyncio.run(asyncio.wait_for(_flush(sid, rollout, size),
                                     timeout=FLUSH_TIMEOUT_S))
    except Exception as e:
        _log(f"{event}: flush error: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
