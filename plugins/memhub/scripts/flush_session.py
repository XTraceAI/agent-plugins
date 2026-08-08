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

Auth = the plugin's OWN token cache (shared `_memhub_auth`), which is a
different store from the /mcp connector's despite sharing an Auth0 client:
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
refreshed automatically. interactive=False — a background hook must never
pop a browser, so with no cached token it degrades quietly (run
/memhub:login once to seed the cache).
Endpoint: $MEMHUB_MCP_BASE_URL(+_SERVER_PATH) > the plugin's .mcp.json
mcpServers.*.url > a default derived from the plugin install path (prod for
`memhub`, staging for `memhub-staging`).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_write  # noqa: E402
import mcp_http  # noqa: E402 — stdlib-only now, so no reason to defer it
from _memhub_auth import NonInteractiveAuthRequired, resolve_bearer  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from room_map import env_for_url  # noqa: E402
from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)
from transcript_chunks import slices as make_slices  # noqa: E402
from redact import redact_records  # noqa: E402
from transcript_filter import drop_command_wrappers  # noqa: E402



def _log(msg: str) -> None:
    # Hook stdout is only shown in verbose/error views; keep one-liners.
    print(f"[memhub-flush] {msg}")


# This hook's breadcrumbs go in their OWN file, beside the per-turn hook's.
#
# The obvious thing — write into the per-turn state file, which already has the
# format and the writer — is wrong, and subtly so. `_save_state` is a
# read-modify-write, and the per-turn flush serialises its own writes with a
# flock that this hook does not hold. Making the files atomic fixed torn reads
# but not LOST UPDATES: both processes read, both modify, both write, and the
# later writer silently discards the earlier one's changes. For that file the
# discarded change could be the cursor, which means re-sent or skipped records.
#
# Two files and no shared mutable state beats one file and a lock: there is no
# window to get wrong, and `capture_health` already scans the directory rather
# than any particular name.
_SESSION_STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"


def _breadcrumb(session_id, reason: str, detail: str = "",
                ok: bool = False) -> None:
    """Record this path's outcome where `capture_health` will find it.

    Same field names as the per-turn hook, because the health check reads one
    vocabulary; a separate FILE, because the two hooks have no lock in common.

    Never raises: a breadcrumb is not worth failing a flush over.
    """
    if not session_id:
        return
    try:
        record = {"at": time.time()}
        if ok:
            record["last_ok_at"] = time.time()
        else:
            record.update(last_error=reason,
                          last_error_detail=(detail or "")[:200] or None,
                          last_error_at=time.time())
        atomic_write.publish(
            _SESSION_STATE_DIR / f"{session_id}.sessionflush.json",
            json.dumps(record))
    except Exception:  # noqa: BLE001
        pass


# Comfortably inside the hooks' 300s budget, so the script decides when to
# stop rather than being killed at an arbitrary point mid-upload.
_DEFAULT_DEADLINE_S = 240.0


def _stop_before_slice(index: int, now: float, deadline: float) -> bool:
    """Whether to give up rather than send slice ``index`` (1-based).

    The FIRST slice always goes. A backstop that sends nothing is not a
    degraded capture, it is no capture — and the sessions reaching this path
    are the ones per-turn capture already missed, so "some of it" and "none of
    it" are the two outcomes that matter most to keep apart.

    As the code stands the check could not fire on slice 1 anyway: the deadline
    is established after every piece of setup, so no time has passed when the
    first slice is considered. But that is a property of where ONE LINE sits,
    and moving it would silently convert "captured a prefix" into "captured
    nothing" with the log still reading like a bounded, deliberate stop. The
    guarantee is written down here so it cannot be lost by accident.
    """
    return index > 1 and now >= deadline


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

    # Tolerant parse, NOT json.loads-or-die: this hook reads the transcript
    # while Claude Code is still appending to it, so a truncated final line
    # is the EXPECTED case here, not corruption. One partial line must not
    # silently kill the whole flush (the outer except would eat it) — skip
    # it; the next flush's watermark pass picks the record up once complete.
    #
    # Explicit utf-8: transcripts are UTF-8 regardless of the OS locale, and a
    # bare open() decodes with the locale codec — on a non-UTF-8 default
    # (cp950, cp1252, …) one em-dash raises UnicodeDecodeError and this hook
    # dies silently on EVERY commit/PR and at SessionEnd. errors="replace" so a
    # single bad byte degrades one character instead of dropping the flush.
    records = []
    malformed = 0
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
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

    # Same reasoning as the filter above: a session must not come out with
    # credentials in it depending on which path captured it. Every upload path
    # redacts, or none of them can be relied on.
    records = redact_records(records)

    # The name the transcript carries — the user's rename first, then the one
    # Claude Code generated, then the session's first prompt for a headless run
    # that has neither. Without it this path imports every session unnamed.
    #
    # Derived AFTER the redaction above, and that order is load-bearing: a
    # session whose first prompt is `export MEMHUB_TOKEN=mhk_…` would otherwise
    # ship its key as the conversation's NAME. Moving this above the redaction
    # would reintroduce that silently.
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

    # In a thread: resolving may renew the token with blocking urllib calls
    # (~25s of socket timeout), and a synchronous call cannot be cancelled by
    # the deadline this flush runs under.
    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        # Breadcrumb BEFORE raising. The raise is caught by main()'s catch-all,
        # which logs to an async hook's discarded stdout and records nothing —
        # so without this the backstop's most likely failure, having no
        # credential at all, was the one condition it reported least. Every
        # other failure on this path leaves a trace; this one has to as well.
        _breadcrumb(session_id, "auth", "no usable credential")
        # Same contract the SDK's NonInteractiveAuthRequired had: a background
        # hook degrades quietly rather than popping a browser at the user.
        raise NonInteractiveAuthRequired(
            "no usable credential (key, token or cached login)")

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

    # Stateless server, no handshake needed — see mcp_http for the probe
    # that established this. One round trip instead of three.
    # A PER-CALL cap, not the whole budget. This path sends a transcript in
    # slices, and giving each call the entire deadline means one stalled slice
    # consumes it and every later slice is skipped — the between-slice deadline
    # check cannot help, because it only runs between calls.
    #
    # A quarter, floored so a short deadline still permits a real upload — and
    # then CLAMPED BY THE DEADLINE ITSELF, because the floor could otherwise
    # exceed it: `MEMHUB_FLUSH_DEADLINE_S=10` gave a 30s per-call cap, letting
    # one call overrun the entire budget so the wall-clock check never got to
    # stop the run cleanly. A per-call limit larger than the total is not a
    # limit.
    deadline = _deadline_s()
    session = mcp_http.Session(url, bearer,
                               timeout=min(deadline, max(30.0, deadline / 4)))
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
        if _stop_before_slice(index, time.monotonic(), deadline):
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
        # Bound this call by the time LEFT, not a fixed fraction of the
        # budget. `_stop_before_slice` only checks BEFORE a slice, so a
        # slice starting just under the deadline could otherwise run a full
        # per-call timeout past it — and with the deadline tuned up (say
        # MEMHUB_FLUSH_DEADLINE_S=300) that overrun crosses the hook's own
        # 300s cap, so the process is killed mid-upload and the summary
        # naming the unsent slices never gets written. That summary is the
        # entire reason this path stops itself rather than being stopped.
        if not await _send(session, arguments, room, title, namespace,
                           index, len(payloads),
                           budget=max(1.0, deadline - time.monotonic())):
            return


async def _send(session, arguments, room, title, namespace,
                index: int, total: int, budget: float | None = None) -> bool:
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
    # Transport failures stop the run cleanly — slices already sent stay durable
    # and the server dedups on re-send — AND leave a breadcrumb.
    #
    # The breadcrumb is the point. This hook is async fire-and-forget, so its
    # stdout goes nowhere, and it is the BACKSTOP: the path that catches exactly
    # the sessions per-turn capture already missed. Returning False silently
    # would leave it failing invisibly, which is the precise bug this whole
    # series exists to remove — and it would be worse here than in the per-turn
    # path, because nothing downstream is left to notice.
    try:
        res = await session.call_tool("import_conversation",
                                      arguments=arguments, timeout=budget)
    except mcp_http.McpRateLimited as e:
        wait = f" (retry-after {e.retry_after:.0f}s)" if e.retry_after else ""
        _log(f"{label}rate limited{wait}; slices already sent are stored")
        _breadcrumb(arguments.get("conversation_id"), "rate_limited", str(e))
        return False
    except mcp_http.McpNoResponse as e:
        _log(f"{label}no response frame: {e}")
        _breadcrumb(arguments.get("conversation_id"), "unrecognized_response", str(e))
        return False
    except mcp_http.McpError as e:
        if e.status == 401:
            _log(f"{label}credential rejected (401); run /memhub:login")
            _breadcrumb(arguments.get("conversation_id"), "auth", str(e))
        elif e.status == 403:
            _log(f"{label}credential lacks permission (403); check its scopes "
                 "and org access")
            _breadcrumb(arguments.get("conversation_id"), "forbidden", str(e))
        else:
            _log(f"{label}transport error: {e}")
            _breadcrumb(arguments.get("conversation_id"), "error", str(e))
        return False
    texts = [t for t in (getattr(b, "text", None)
                         for b in getattr(res, "content", []) or []) if t]
    # MCP signals tool failure via isError + a message in content, NOT via an
    # exception — without this check a bad token or server error logs as
    # success while memory never updates.
    if getattr(res, "isError", False):
        detail = (texts[0] if texts else "no detail")[:200]
        _log(f"{label}flush FAILED: {detail}")
        # Breadcrumbed like the transport failures above. Leaving this path
        # silent made the backstop's MOST LIKELY server-side failure — a slice
        # the server rejects — the one it reported least, which is the same
        # asymmetry the transport paths were fixed for one round earlier.
        _breadcrumb(arguments.get("conversation_id"), "server_rejected", detail)
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
        # Retract any earlier failure on this path. Without it a single
        # throttled slice would keep warning for a day even though the backstop
        # went on to work — the same crying-wolf the per-turn hook clears with
        # `_mark_success`.
        _breadcrumb(arguments.get("conversation_id"), "", ok=True)
        return True
    # Not an error per the protocol, but not the shape import_conversation
    # returns either — log what came back instead of claiming success on an
    # arbitrary body, and stop: an unrecognised reply is not a slice landing.
    detail = (texts[0] if texts else "")[:120]
    _log(f"{label}flush response unrecognized: {detail!r}")
    # The last silent exit on this path. Every way `_send` can return False now
    # leaves a trace — the guarantee is only worth something if it has no holes.
    _breadcrumb(arguments.get("conversation_id"), "unrecognized_response", detail)
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
            _log("no cached OAuth token; run /memhub:login "
                 "(or set MEMHUB_TOKEN) to enable commit flush — skipping")
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
