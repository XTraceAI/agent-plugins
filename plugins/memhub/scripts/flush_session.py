#!/usr/bin/env python3
"""Incremental session flush — fired by PostToolUse hooks on commit/PR events.

Reads the Claude Code hook input JSON from stdin (``session_id``,
``transcript_path``), sends the transcript-so-far to ``import_conversation``
with ``conversation_id = session_id`` — the SAME id the SessionEnd hook uses,
so every trigger feeds one conversation and one server-side watermark
(``agentic_seen_uuids``): the full transcript is re-sent, but only the DELTA
since the last flush is processed. Commits/PRs are semantic work boundaries,
so flushing here makes memory available mid-session (parallel sessions see
fresh decisions), shapes batch episodes into work-unit narratives, and gives
the gist's fold-forward an outcome-flavored cadence. SessionEnd remains the
backstop for sessions that never commit.

Discipline mirrors the SessionEnd hook: THIS SCRIPT NEVER FAILS LOUDLY —
any error exits 0 quietly (the hook is async fire-and-forget; memory capture
must never disturb the user's session).

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
refreshed automatically. interactive=False — a background hook must never
pop a browser, so with no cached token it degrades quietly (run any memhub
terminal script once, e.g. /memhub:import-session, to seed the cache).
Endpoint: $MEMHUB_MCP_BASE_URL(+_SERVER_PATH) > the plugin's .mcp.json
mcpServers.*.url > a default derived from the plugin install path (prod for
`memhub`, staging for `memhub-staging`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import NonInteractiveAuthRequired, resolve_url_and_auth  # noqa: E402

# The MCP SDK logs the OAuth flow's exception (with traceback) before it
# propagates to us; a background hook must stay quiet, so silence that logger
# — main() still reports the condition in one friendly line.
logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)


def _log(msg: str) -> None:
    # Hook stdout is only shown in verbose/error views; keep one-liners.
    print(f"[memhub-flush] {msg}")


async def _flush(session_id: str, transcript_path: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # Tolerant parse, NOT json.loads-or-die: this hook reads the transcript
    # while Claude Code is still appending to it, so a truncated final line
    # is the EXPECTED case here, not corruption. One partial line must not
    # silently kill the whole flush (the outer except would eat it) — skip
    # it; the next flush's watermark pass picks the record up once complete.
    records = []
    malformed = 0
    with open(transcript_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    if malformed:
        _log(f"skipped {malformed} partial/malformed line(s) (mid-write read)")
    if not records:
        _log("empty transcript; nothing to flush")
        return

    # Working-context scope for captured directives: git remote basename from
    # the transcript's cwd, resolved client-side (a worktree dir name would
    # stamp a scope that hides directives from the canonical repo's recalls —
    # the remote is stable across worktrees). None → import stays unscoped.
    cwd = next((r.get("cwd") for r in records
                if isinstance(r, dict) and isinstance(r.get("cwd"), str)
                and r.get("cwd")), None)
    namespace = None
    if cwd:
        try:
            out = subprocess.run(
                ["git", "-C", cwd, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=2,
            )
            u = out.stdout.strip()
            if out.returncode == 0 and u:
                namespace = u.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        except (OSError, subprocess.SubprocessError):
            pass

    url, headers, auth = resolve_url_and_auth(interactive=False)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            arguments = {
                "messages": records,
                "conversation_id": session_id,
                "source_platform": "claude",
            }
            if namespace:
                # Server ignores unknown args pre-#722; stamps directive
                # scope after.
                arguments["namespace"] = namespace
            res = await session.call_tool("import_conversation", arguments=arguments)
            texts = [t for t in (getattr(b, "text", None)
                                 for b in getattr(res, "content", []) or []) if t]
            # MCP signals tool failure via isError + a message in content,
            # NOT via an exception — without this check a bad token or
            # server error logs as success while memory never updates.
            if getattr(res, "isError", False):
                _log(f"flush FAILED: {(texts[0] if texts else 'no detail')[:200]}")
                return
            out = getattr(res, "structuredContent", None)
            if isinstance(out, dict) and "conversation_id" not in out \
                    and isinstance(out.get("result"), dict):
                out = out["result"]  # FastMCP wraps some returns in {"result": …}
            if not isinstance(out, dict):
                for text in texts:
                    try:
                        out = json.loads(text)
                        break
                    except json.JSONDecodeError:
                        continue
            if isinstance(out, dict) and "conversation_id" in out:
                _log(f"flushed {out.get('messages_received')} records "
                     f"(conv {str(out.get('conversation_id'))[:8]}, "
                     f"path={out.get('path')}) — server processes the delta")
            else:
                # Not an error per the protocol, but not the shape
                # import_conversation returns either — log what came back
                # instead of claiming success on an arbitrary body.
                _log("flush response unrecognized: "
                     f"{(texts[0] if texts else '')[:120]!r}")


def _auth_required(e: BaseException) -> bool:
    """True if NonInteractiveAuthRequired is anywhere in the exception tree.

    The MCP client runs auth inside anyio task groups, so the raise from our
    redirect_handler can surface wrapped in ExceptionGroups or as a __cause__.
    """
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
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = hook_input.get("session_id")
        transcript_path = hook_input.get("transcript_path")
        if not session_id or not transcript_path or not Path(transcript_path).exists():
            _log("missing session_id/transcript_path; skipping")
            return 0
        cmd = str((hook_input.get("tool_input") or {}).get("command", ""))[:120]
        _log(f"trigger: {cmd!r}")
        asyncio.run(_flush(session_id, transcript_path))
    # BaseException, not Exception: when anyio's task group mixes a
    # CancelledError into the group (e.g. the auth failure cancelling sibling
    # tasks), the result is a BaseExceptionGroup — a BaseException — which
    # would skip an Exception handler and kill the hook with a traceback.
    # This is a fire-and-forget background hook: exit 0 quietly, always.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        if _auth_required(e):
            _log("no cached OAuth token; run /memhub:import-session once "
                 "(or set MEMHUB_TOKEN) to enable commit flush — skipping")
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
