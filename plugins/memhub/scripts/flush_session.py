#!/usr/bin/env python3
"""Whole-transcript session flush — fired on commit/PR events and at SessionEnd.

Reads the Claude Code hook input JSON from stdin (``session_id``,
``transcript_path``) and sends the transcript-so-far to ``import_conversation``
with ``conversation_id = session_id``, so every trigger feeds one conversation
and one server-side watermark (``agentic_seen_uuids``): the full transcript is
re-sent, but only the DELTA since the last flush is processed. Commits/PRs are
semantic work boundaries, so flushing there makes memory available mid-session
(parallel sessions see fresh decisions), shapes batch episodes into work-unit
narratives, and gives the gist's fold-forward an outcome-flavored cadence.

**Also the SessionEnd hook**, which used to be an ``agent``-type hook told in
prose to read the transcript and re-emit every record inline. Three things say
it was not capturing anything. A headless run rejects it outright — measured:
``Agent stop hooks are not yet supported outside REPL``. An agent cannot
re-emit a 16 MB transcript even where it does run. And the breadcrumb file it
was instructed to write on BOTH success and failure has never been created on
any machine, which is what let all of that stay invisible.

Being a script also keeps it in step with the other capture paths rather than
drifting: the prose version sent no ``org_id`` (every room in a non-default org
failed with "Agent brain not found"), filtered no slash-command wrappers, and
derived no title.

Deliberately INDEPENDENT of the per-turn hook rather than reusing its cursor.
This is the backstop for exactly the cases where per-turn capture is dormant —
disabled, unauthenticated, or failing — so inheriting its state would mean
inheriting whatever went wrong with it. Re-sending everything and letting the
server's watermark dedup is the property that makes it a second witness.

Discipline mirrors the per-turn hook: THIS SCRIPT NEVER FAILS LOUDLY —
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
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import NonInteractiveAuthRequired, resolve_url_and_auth  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from room_map import env_for_url  # noqa: E402
from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)
from transcript_chunks import slices as make_slices  # noqa: E402
from transcript_filter import drop_command_wrappers  # noqa: E402

# The MCP SDK logs the OAuth flow's exception (with traceback) before it
# propagates to us; a background hook must stay quiet, so silence that logger
# — main() still reports the condition in one friendly line.
logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)


def _log(msg: str) -> None:
    # Hook stdout is only shown in verbose/error views; keep one-liners.
    print(f"[memhub-flush] {msg}")


# Comfortably inside the hooks' 300s budget, so the script decides when to
# stop rather than being killed at an arbitrary point mid-upload.
_DEFAULT_DEADLINE_S = 240.0


def _deadline_s() -> float:
    """How long this flush may spend sending, from the env with a sane floor.

    Read at CALL time and never allowed to raise: parsing at import time means
    a malformed override crashes the module before the handler that keeps this
    hook quiet exists. Zero or negative is rejected rather than honoured — it
    would abandon every session after one slice.
    """
    raw = (os.environ.get("MEMHUB_FLUSH_DEADLINE_S") or "").strip()
    if not raw:
        return _DEFAULT_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_DEADLINE_S
    return value if value > 0 else _DEFAULT_DEADLINE_S


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

    # Slash-command bookkeeping never leaves the machine. Applied HERE as well
    # as in the other two upload paths deliberately: the filter's own contract
    # is that a session cannot come out clean or dirty depending on which path
    # captured it, and this path was the one still shipping `/model` and its
    # reply as things the user said.
    kept = drop_command_wrappers(records)
    if len(kept) != len(records):
        _log(f"dropped {len(records) - len(kept)} slash-command record(s)")
    records = kept
    if not records:
        _log("transcript holds only slash-command records; nothing to flush")
        return

    # The name the transcript carries — the user's rename first, then the one
    # Claude Code generated, then the session's first prompt for a headless run
    # that has neither. Without it this path imports every session unnamed.
    title = custom_title(records) or generated_title(records) \
        or prompt_title(records) or None

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

    # Route into the repo's room. Read AFTER the url resolves so the lookup is
    # keyed by the backend we're actually about to write to — prod and staging
    # hold different brain ids for the same repo. No cache (or no repo) → the
    # import stays personal, exactly as it behaved before, rather than guessing
    # a brain. `/memhub:onboard` and `/memhub:spec init` populate the cache.
    #
    # Only when the TRANSCRIPT told us where it ran. read_room(None) would fall
    # back to this process's cwd, and a hook can fire from a different repo than
    # the session's — routing the session into a room it has nothing to do with.
    # Unknown origin must degrade to personal, never to a guess.
    env = env_for_url(url)

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            # Cached hit is a dict lookup; a miss asks the server once and
            # caches the answer, so this is not a per-flush round-trip.
            room = await resolve_repo_brain(session, cwd, env) if cwd else None

            # Chunked, because this path sends the WHOLE transcript in one
            # call and real sessions outgrow one payload: of 185 local
            # transcripts, 74 exceed 1 MB and the largest is 46 MB. Unchunked,
            # this works on ordinary sessions and fails on precisely the long
            # ones — and as the backstop for when per-turn capture is dormant,
            # failing on the biggest sessions is failing where it matters most.
            # Slices are disjoint and sent in order against one conversation,
            # so the server's watermark sees a normal incremental import.
            payloads = make_slices(records)

            # Bounded by wall clock, not just by slice count. The hook's own
            # budget is 300s; a many-slice session can exceed it, and being
            # KILLED there is the bad ending — the process dies mid-upload,
            # the tail is lost, and nothing says so. Stopping cleanly a minute
            # early turns that into a partial capture that REPORTS what it did
            # not send, which is the difference between a gap you can act on
            # and one you never learn about.
            # Slightly INSIDE the hard timeout in main(), so a run that is
            # merely slow stops here — where it can name what it did not send
            # — instead of being cancelled where it cannot.
            deadline = time.monotonic() + _deadline_s() * 0.9
            for index, payload in enumerate(payloads, 1):
                if time.monotonic() >= deadline:
                    remaining = sum(len(p) for p in payloads[index - 1:])
                    _log(f"deadline reached after {index - 1}/{len(payloads)} "
                         f"slices; {remaining} record(s) not sent. Re-run "
                         f"/memhub:import-session --session {session_id} to "
                         "finish (it resumes from the server's watermark).")
                    return
                arguments = {
                    "messages": payload,
                    "conversation_id": session_id,
                    "source_platform": "claude",
                }
                if not await _send(session, arguments, room, title, namespace,
                                   index, len(payloads)):
                    return


async def _send(session, arguments, room, title, namespace,
                index: int, total: int) -> bool:
    """Send one slice. False means stop — a later slice cannot help.

    Sequential and fail-closed: the slices are ordered, so pressing on after a
    rejection would leave a hole in the middle of the conversation while the
    log reported the later slices as fine.
    """
    if room:
        arguments["agent_brain_id"] = room["brain_id"]
        # The org that OWNS the room, when it is not the caller's default. A
        # brain resolves inside exactly ONE org, and the default follows
        # whichever org was last selected in the MemHub app. Sending the id
        # without its org is how every capture into such a room fails with
        # "Agent brain not found" — an error that reads like a deleted brain.
        if room.get("org_id"):
            arguments["org_id"] = room["org_id"]
    if title:
        arguments["title"] = title
    if namespace:
        # Older servers ignore unknown arguments; newer ones stamp the
        # directive scope from it. Safe either way.
        arguments["namespace"] = namespace

    label = f"slice {index}/{total}: " if total > 1 else ""
    res = await session.call_tool("import_conversation", arguments=arguments)
    texts = [t for t in (getattr(b, "text", None)
                         for b in getattr(res, "content", []) or []) if t]
    # MCP signals tool failure via isError + a message in content, NOT via an
    # exception — without this check a bad token or server error logs as
    # success while memory never updates.
    if getattr(res, "isError", False):
        _log(f"{label}flush FAILED: "
             f"{(texts[0] if texts else 'no detail')[:200]}")
        return False
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
        # Name the destination: "flushed N records" alone can't distinguish a
        # room write from a personal one, which is the failure mode this
        # routing exists to fix.
        dest = (f'room {room["brain_id"][:8]}' if room
                else "personal memory (no room cached)")
        _log(f"{label}flushed {out.get('messages_received')} records "
             f"(conv {str(out.get('conversation_id'))[:8]}, "
             f"path={out.get('path')}) -> {dest} "
             "— server processes the delta")
        return True
    # Not an error per the protocol, but not the shape import_conversation
    # returns either — log what came back instead of claiming success on an
    # arbitrary body, and stop: an unrecognised reply is not a slice landing.
    _log(f"{label}flush response unrecognized: "
         f"{(texts[0] if texts else '')[:120]!r}")
    return False


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
    # Bound BEFORE the try, because the handler reads them. Assigned inside it,
    # any failure earlier in the block — a malformed stdin payload is enough —
    # would make the handler itself raise NameError, and this script's one hard
    # rule is that it never surfaces a traceback in the user's session.
    session_id = ""
    timeout_s = _deadline_s()
    started = time.monotonic()
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = hook_input.get("session_id")
        transcript_path = hook_input.get("transcript_path")
        if not session_id or not transcript_path or not Path(transcript_path).exists():
            _log("missing session_id/transcript_path; skipping")
            return 0
        # SessionEnd carries no tool_input; it reports its reason instead.
        cmd = str((hook_input.get("tool_input") or {}).get("command", ""))[:120]
        reason = str(hook_input.get("reason") or "")[:40]
        _log(f"trigger: {cmd!r}" if cmd
             else f"trigger: session end ({reason or 'no reason given'})")
        # HARD bound on the whole flush, not just on the gaps between slices.
        # The between-slice deadline can only stop the loop where it looks;
        # ONE slow call — an oversized record riding alone, a stalled server —
        # runs past the hook's budget and the process is killed mid-upload,
        # which is the exact ending that check exists to avoid, and the one
        # case it cannot see. Ending ourselves first is what guarantees the
        # breadcrumb below is always written.
        started = time.monotonic()
        asyncio.run(asyncio.wait_for(
            _flush(session_id, transcript_path), timeout=timeout_s))
    # BaseException, not Exception: when anyio's task group mixes a
    # CancelledError into the group (e.g. the auth failure cancelling sibling
    # tasks), the result is a BaseExceptionGroup — a BaseException — which
    # would skip an Exception handler and kill the hook with a traceback.
    # This is a fire-and-forget background hook: exit 0 quietly, always.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        # Told apart by ELAPSED TIME, not by type. Since 3.11 ``socket.timeout``
        # IS ``TimeoutError``, so a network read that gave up inside the client
        # arrives here indistinguishable from our own wall clock — and reporting
        # "timed out after 240s" for a socket that died in 3s sends whoever
        # reads the log looking for a slow transcript instead of a sick
        # connection. Our deadline is the only one that can have consumed the
        # whole budget.
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)) \
                and time.monotonic() - started >= timeout_s * 0.99:
            # Slices already sent are durable and deduped, so this is a
            # partial capture rather than a lost one — say which it is.
            _log(f"timed out after {timeout_s:.0f}s; slices already sent "
                 "are stored. Re-run /memhub:import-session --session "
                 f"{session_id} to finish (it resumes from the server's "
                 "watermark).")
        elif _auth_required(e):
            _log("no cached OAuth token; run /memhub:import-session once "
                 "(or set MEMHUB_TOKEN) to enable commit flush — skipping")
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
