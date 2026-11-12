#!/usr/bin/env python3
"""Codex capture: hook-triggered flush of a Codex session into MemHub.

The Codex sibling of ``cursor_flush.py``, one seam simpler: rollouts are
append-only JSONL, so "anything new?" is a byte-size comparison rather than a
blob-set. Everything downstream is the shared machinery (``readers.codex``
transform, ``redact``, ``resolve_bearer``, ``room_map``'s git helpers,
``mcp_http`` — bare python3, stdlib only).

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

Sub-agents (Codex CLI and desktop, multi-agent v2): after the root's own save
is confirmed, ``codex_agents.discover`` finds the threads it spawned and
``agent_capture.send_agents`` sends each as ``codex-<root>/agent-<child>``,
linked to ``codex-<root>`` — gated on the server's ``import_conversation``
schema (``server_caps``, cached in ``_server_caps``), remembered in
``<root>.agents`` so an unchanged agent is not re-sent. A child is never filed
as ``codex-<child>``: a hook naming a child's rollout is routed to its root,
and a child flushed on its own is still skipped as a bot thread. Agent sends
never move the root's ``fail_streak``. A Stop rescans the root's agents even
when the root did not grow, and also re-checks a few other roots whose agents
were last seen running (a child can finish after its root's last Stop).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_capture  # noqa: E402
import atomic_write  # noqa: E402
import codex_agents  # noqa: E402
import portable_lock  # noqa: E402
import mcp_http  # noqa: E402
import pr_provenance  # noqa: E402
import server_caps  # noqa: E402
from _memhub_auth import resolve_bearer, skill_command  # noqa: E402
from readers import codex as codex_reader  # noqa: E402
from readers.jsonl import ZstdUnavailable  # noqa: E402
from redact import redact_records, redact_text  # noqa: E402
from transcript_filter import elide_oversized_tool_results  # noqa: E402
from room_map import git_env, git_readonly  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "codexflush"
FLUSH_TIMEOUT_S = 240.0

# The milestone must be in COMMAND POSITION, not merely mentioned: a bare
# substring match fires a full flush on `echo "remember to git commit"` or
# `grep 'git commit' log`. Command position = start of the text, just after a
# shell separator (; && || | &), or inside a `bash -lc "..."` wrapper — which
# is how Codex actually delivers shell calls, so a plain ^ anchor would miss
# every real milestone it has.
_MILESTONE_RE = re.compile(
    # Position: start of text, after a shell separator, or inside a
    # `bash -lc "..."` wrapper (how Codex delivers shell calls).
    r"""(?:^|[;&|]\s*|\b(?:ba)?sh\s+-[a-z]*c\s*['"]?)\s*"""
    # Leading wrappers an agent may prepend.
    r"""(?:(?:sudo|env|command|time|nice)\s+(?:[A-Za-z_]\w*=\S*\s+)*)*"""
    # Options BETWEEN the tool and the subcommand: `git -C <dir> commit` is a
    # routine agent form, and requiring adjacency silently skipped it.
    # `gh pr` alone also matched read-only listings (`gh pr list`, `gh pr
    # view`), each buying a whole-rollout send; only PR ACTIONS are
    # milestones. Kept identical to cursor_flush's gate.
    r"""(?:git(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+[^\s-]\S*)?)*\s+commit\b"""
    r"""|gh(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+[^\s-]\S*)?)*"""
    r"""\s+pr\s+(?:create|merge|ready|edit|close|reopen|comment)\b)""")

# Bound on how much of a command we scan for a milestone. The regex above is
# linear (bounded quantifiers, no nested repetition — a 200 KB input scans in
# ~2 ms), so this is NOT backtracking protection; it is only a sanity limit on
# a pathological multi-megabyte argument. 16 KiB clears any realistic agent
# command — including a long `bash -lc "cd <deep/path> && … && git commit"`
# whose verb lands well past the old 512-byte cap — by a wide margin. Kept in
# step with cursor_flush's cap.
_MILESTONE_SCAN_LIMIT = 16384

# A persistently failing backend must not buy a full re-parse on every hook:
# the watermark only advances on success, so each retry re-reads and
# re-redacts the whole (growing) rollout. One attempt per cooldown window.
ERROR_COOLDOWN_S = 60.0
# Dormancy (an unconfirmable server — see _verdict) must not be a one-way
# door: going dormant means never flushing again, so nothing can ever observe
# that the server was upgraded. Re-probe occasionally instead.
DORMANT_RETRY_S = 1800.0
# Cap on how long Stop waits for a concurrent flush (see _acquire).
LOCK_WAIT_S = 60.0
# See cursor_flush: a server PERMANENTLY answering "unconfirmed" (always
# records_dropped>0, say) must not drive full-rollout re-uploads on every
# event forever. After this many consecutive unconfirmed replies the session
# goes dormant like an unsupported one, on the same re-probe timer.
MAX_UNCONFIRMED = 5
# Other roots a Stop re-checks for agents last seen running, and how far back.
AGENT_RESCAN_ROOTS = 3
AGENT_RESCAN_WINDOW_S = 24 * 3600


def _note_failure(sid: str, reason: str) -> None:
    """Record a SERVER-CONTACTED failure. Stamps last_error_at (feeding the
    60s cooldown for the transient case) and increments a single fail_streak
    over EVERY kind — rate-limit, McpError, timeout, unconfirmed import. At
    MAX_UNCONFIRMED the session goes dormant, the only thing that also bounds
    Stop (cooldown-exempt) against a hard-down backend; the streak resets at
    dormancy so each re-probe gets a fresh budget.

    "Consecutive" means consecutive CONTACTED attempts since the last success:
    a confirmed import clears it, and so does any outcome that never reached
    the server (empty rollout, missing credential), so those neutral no-ops
    cannot accumulate a healthy session into dormancy."""
    now = time.time()
    st = _read_state(sid)
    if st.get("unsupported"):
        # A re-probe of a dormant session FAILED. Stay dormant and reset the
        # timer, so a persistently-down server is attempted exactly once per
        # DORMANT_RETRY_S — not given a fresh MAX_UNCONFIRMED budget that
        # would let it hammer between windows.
        _save_state(sid, last_error=reason, last_error_at=now,
                    unsupported=True, unsupported_at=now, fail_streak=0)
        return
    streak = int(st.get("fail_streak") or 0) + 1
    if streak >= MAX_UNCONFIRMED:
        _log(f"{streak} consecutive failed imports ({reason}) — per-event "
             f"flush is dormant for this session; run {skill_command('import-session')} "
             f"to capture it. Re-probes in {DORMANT_RETRY_S / 60:.0f} min.")
        _save_state(sid, last_error=reason, last_error_at=now,
                    unsupported=True, unsupported_at=now, fail_streak=0)
    else:
        _save_state(sid, last_error=reason, last_error_at=now,
                    unsupported=False, fail_streak=streak)


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


def _acquire(sid: str, blocking: bool = False) -> int | None:
    """Per-session flock. Non-blocking returns None when held; blocking WAITS.

    A milestone `git commit` is very often the last action before a turn ends,
    so PostToolUse and Stop fire within the same second — and the bridge
    detaches each into its own process. Without this, both read the same
    rollout_size, both re-parse and re-redact the whole rollout, both upload,
    then race to write the watermark back.

    For most events the loser is redundant by construction (the holder is
    sending the same or newer content), so they skip — hence non-blocking.
    Stop is the exception and passes ``blocking=True``: Codex has no
    SessionEnd, so a Stop that lost this lock to a concurrent PostToolUse
    would be the session's LAST event and never retry, and if that
    PostToolUse then failed the tail would go uncaptured until an
    import-session sweep. Waiting is cheap here — the holder's flush is
    itself bounded by asyncio.wait_for(FLUSH_TIMEOUT_S), and Stop runs
    detached — and once Stop acquires, should_flush re-checks growth so it
    ships only the delta (or no-ops if the peer already covered it).

    flock rather than a lockfile because the kernel releases it on process
    exit however that happens — there is no stale flock to reclaim.
    The caller must keep the fd OPEN; closing releases.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC is belt-and-braces: CPython has made os.open fds
    # non-inheritable by default since PEP 446 (3.4), so a subprocess cannot
    # already pin this lock past our exit — the flag states the invariant in
    # code so a future refactor cannot quietly drop it. getattr because the
    # constant is Unix-only and the capture scripts run on native Windows.
    fd = os.open(STATE_DIR / f"{_safe_sid(sid)}.flush.lock",
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    if not blocking:
        try:
            portable_lock.lock_exclusive(fd, blocking=False)
            return fd
        except OSError:
            os.close(fd)
            return None
    # BOUNDED wait, never an unbounded flock(LOCK_EX): a live peer stuck
    # outside its asyncio.wait_for (a huge synchronous parse, a wedged
    # subprocess) would otherwise hang one detached process per boundary
    # event. Poll up to LOCK_WAIT_S, then give up (the sweep backstops);
    # a dead peer releases via the kernel and this returns instantly.
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            portable_lock.lock_exclusive(fd, blocking=False)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _safe_sid(sid: str) -> str:
    """A sid that is safe as a filename component and stable per session.

    Payload identity is semi-trusted: anything outside [A-Za-z0-9._-]
    (separators, ..-with-slash tricks) is flattened so the state file can
    never land outside STATE_DIR. Real Codex sids are plain uuids, which
    pass through unchanged — including as the conversation_id namespace."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:80]


def _contained(p: Path) -> Path | None:
    """``p`` resolved, but only if it is a real file inside the reader's own
    rollout store — else ``None``. Both sides are resolved so a symlink
    cannot smuggle a path across the boundary."""
    try:
        rp = p.expanduser().resolve()
        root = codex_reader.sessions_root().resolve()
        if not rp.is_relative_to(root):
            return None
        # A payload (or the state DB) can still name rollout-X.jsonl after
        # Codex compressed it to rollout-X.jsonl.zst beside it.
        found = codex_reader.existing_rollout(rp)
        return found.resolve() if found is not None and found.resolve().is_relative_to(root) else None
    except (OSError, ValueError):  # untrusted strings (NUL bytes raise)
        return None


def _sid_of(rollout: Path) -> str:
    """Session identity derived from the RESOLVED rollout, never from the
    payload field that happened to locate it.

    Both branches end at the same file, but a payload can carry
    transcript_path on one event and only session_id on the next; keying off
    whichever arrived would give one session two state files, resetting the
    byte-growth watermark and re-uploading the whole rollout. The filename's
    uuid is the one identity every branch agrees on."""
    return _safe_sid(codex_reader.rollout_uuid(rollout) or rollout.stem)


def locate_rollout(payload: dict) -> tuple[Path | None, str | None]:
    """(rollout path, session uuid) from a hook payload — path preferred,
    bare session_id resolved through the reader, no fields → (None, None).

    Identity is deterministic per session: rollout_uuid is a pure regex on
    the filename (it cannot succeed on one event and fail on the next), and
    the stem fallback for uuid-less names is the same string every event.

    EVERY branch's result passes :func:`_contained`. The payload is
    semi-trusted, and both fields reach the filesystem: ``transcript_path``
    obviously, and ``session_id`` because ``codex_reader.locate`` accepts a
    path as well as a uuid (``session_id: "/etc/passwd"`` would otherwise be
    read and UPLOADED). Containment belongs on the way out, once, rather
    than on each way in."""
    sid = payload.get("session_id") or payload.get("conversation_id")
    sid = sid.strip() if isinstance(sid, str) else ""
    tp = payload.get("transcript_path")
    # DISAGREEMENT IS REFUSAL. When both fields are present and name
    # different sessions, the payload is self-contradictory — and a stale or
    # hostile session_id is no more trustworthy than a stale transcript_path
    # (codex_reader.locate resolves a bare uuid too, so a session_id that is
    # a valid uuid of ANOTHER stored session would flush that one). Picking
    # either side risks folding the wrong conversation's gist forward. So the
    # single rule is: use
    # a field only when nothing contradicts it; on conflict, refuse and let
    # the import-session sweep capture the real session.
    if isinstance(tp, str) and tp.strip():
        tp_rollout = _contained(Path(tp.strip()))
        if tp_rollout is None:
            # transcript_path was SUPPLIED but does not resolve inside the
            # rollout store. Falling through to the session_id branch would
            # flush session_id's rollout while ignoring a contradictory (and
            # possibly hostile) transcript_path — the same wrong-session risk
            # the disagreement check guards against. A present-but-unusable
            # transcript_path is a suspect payload: refuse, sweep backstops.
            _log("transcript_path present but not inside the rollout store — "
                 "refusing rather than fall through to session_id")
            return None, None
        from_path = codex_reader.rollout_uuid(tp_rollout)
        # sid present ⇒ transcript_path's identity must be CONFIRMED to match.
        # `from_path is None` (a uuid-less filename) cannot be confirmed, so it
        # counts as disagreement too — otherwise a valid session_id paired with
        # a contained-but-unparseable transcript_path folds that rollout's
        # content forward under a session the id contradicted. (sid empty ⇒
        # transcript_path alone is trusted, once contained.)
        if sid and from_path != sid:
            # A sub-agent's hook: Codex names the ROOT as session_id and the
            # child's rollout as transcript_path (child PreToolUse,
            # PostToolUse, SubagentStart). Not a conflict when the child's
            # header chain reaches that very session — route to the root,
            # whose flush sends the child as its agent. Anything else is
            # still refused.
            root = _root_rollout_of_child(tp_rollout, sid)
            if root is not None:
                _log(f"child thread {from_path!r} of codex-{sid[:8]} — "
                     "routed to its root")
                return root, _sid_of(root)
            _log(f"payload conflict: transcript_path names {from_path!r}, "
                 f"session_id names {sid!r} — refusing rather than risk "
                 f"flushing the wrong session")
            return None, None
        if not sid:
            # transcript_path alone, naming a child: route to its root rather
            # than file the child (it would be skipped as a bot thread).
            root = _root_rollout_of_child(tp_rollout, None)
            if root is not None:
                return root, _sid_of(root)
        return tp_rollout, _sid_of(tp_rollout)
    if sid and "/" not in sid and sid != "latest":
        # codex_reader.locate ALSO honors "latest" (newest by mtime) and any
        # path — a payload session_id of "latest" would flush whichever
        # session is newest, not the one that triggered this hook, and a path
        # would read an arbitrary file. Only a plain id reaches locate, which
        # then matches the session uuid exactly.
        found, _err = codex_reader.locate(sid)
        p = _contained(found) if found is not None else None
        if p is not None:
            return p, _sid_of(p)
    return None, None


def _root_rollout_of_child(child: Path, sid: str | None) -> Path | None:
    """The contained rollout of the root a child rollout belongs to — only
    when its header chain reaches ``sid`` (when given). None otherwise."""
    try:
        root_id = codex_agents.child_root(child, sid)
    except Exception:  # noqa: BLE001 — an unreadable chain is just "not a child"
        return None
    if not root_id or "/" in root_id:
        return None
    found, _err = codex_reader.locate(root_id)
    return _contained(found) if found is not None else None


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
    """Pure gate. Growth or pending PR telemetry is required; Stop is the turn
    boundary and PostToolUse ships only milestones."""
    watermark = state.get("rollout_size") or 0
    if size < watermark:
        # SHRINK: the rollout was truncated, rotated, or a smaller file now
        # reuses this uuid (session compaction). The byte watermark is stale
        # and would block this session forever, so treat it as a fresh
        # rollout — flush from the start; _flush resets the watermark to the
        # new size. (A shrink can't be dormancy-gated away: capture must
        # recover.)
        return True
    # Dormancy gates EVERY event including Stop: a persistently-down server
    # is re-probed once per DORMANT_RETRY_S, never hammered per-turn. Stop's
    # last-chance property is served by the 60s-cooldown exemption in
    # _flush_locked (a transient blip ships the tail immediately, before the
    # streak ever reaches dormancy), and by the import-session sweep, which
    # captures a session whose final tail fell inside a dormant window.
    if (state.get("unsupported") and
            time.time() - (state.get("unsupported_at") or 0) < DORMANT_RETRY_S):
        return False
    if size <= watermark and not state.get("pending_pr_urls"):
        return False
    if event == "Stop":
        return True
    if event == "PostToolUse":
        # Bound the match input (see _MILESTONE_SCAN_LIMIT): the regex is
        # linear, so this only guards against a pathological megabyte-long
        # argument — it is large enough that a real `git commit`/`gh pr` verb,
        # even behind a long wrapper prefix, is never truncated away.
        return bool(_MILESTONE_RE.search(_command_text(payload)[:_MILESTONE_SCAN_LIMIT]))
    return False


# Reading a remote URL means reading the TARGET repo's own config — that is
# the data we want — but a repo's local config can also carry execution
# primitives (core.fsmonitor runs a command, credential helpers run on
# network access, hooksPath redirects hooks). The rollout's cwd is
# semi-trusted, so those are disarmed explicitly rather than relying on
# `remote get-url` not happening to reach them today.
def _verdict(res, expected_conversation_id: str | None = None) -> str:
    """``"ok"`` | ``"unconfirmed"`` | ``"unsupported"`` for an import reply.

    Identical to cursor_flush's, deliberately: a returned call is not a
    stored call (MCP reports failure via isError, and this backend has
    shipped a 200-with-nothing-stored mode — which is how uuid-less codex
    imports persisted nothing for months while replying success).

    A server that OMITS ack_through gets "unsupported" rather than either
    binary answer: trusting it risks a session's last flush, distrusting it
    re-uploads the whole rollout on every event forever. The caller goes
    dormant instead — no loss, no loop.
    """
    if getattr(res, "isError", False):
        _log(f"server rejected the import: {mcp_http.texts_of(res)[:1]}")
        return "unconfirmed"
    ack = mcp_http.ack_of(res, expected_conversation_id)
    if ack is None:
        _log("import response unrecognized — holding the watermark")
        return "unconfirmed"
    if ack.get("records_dropped"):
        _log(f"server dropped {ack['records_dropped']} record(s) — "
             f"holding the watermark")
        return "unconfirmed"
    if "ack_through" not in ack:
        return "unsupported"
    if not ack["ack_through"]:
        _log("import NOT confirmed (ack_through null) — holding the "
             "watermark so the next event re-sends")
        return "unconfirmed"
    return "ok"


def _cwd_ok(cwd) -> bool:
    """``cwd`` is read out of the ROLLOUT, so it is session content, not a
    trusted path — and not necessarily even a string. isinstance first: a
    corrupt or hostile rollout can carry a list or a number, and .startswith
    would raise past the clause below, costing the whole flush."""
    if not isinstance(cwd, str) or not cwd or cwd.startswith("-"):
        return False
    try:
        return Path(cwd).is_absolute() and Path(cwd).is_dir()
    except (OSError, ValueError):
        return False


def _git_remote_basename(cwd: str) -> str | None:
    """The directive namespace: the origin remote's basename.

    ``cwd`` comes out of the rollout, so it is validated before it becomes
    a `git -C` argument: an absolute, existing directory that cannot be read
    as an option. Pointing git at an arbitrary directory would let it honor
    that repo's local config."""
    import subprocess
    if not _cwd_ok(cwd):
        return None
    try:
        out = subprocess.run(git_readonly(cwd) + ["remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=2,
                             env=git_env())
        u = out.stdout.strip()
        if out.returncode == 0 and u:
            return u.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    # ValueError covers an origin URL git prints in bytes that do not decode:
    # that is "no namespace", not a flush error counted toward dormancy.
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _agents_cache(sid: str) -> Path:
    """The root's agent sent cache: ``{agent_id: "<size>:<status>:<d|b>"}``.
    No ``.json`` suffix — capture_health reads ``*.json`` as breadcrumbs."""
    return STATE_DIR / f"{_safe_sid(sid)}.agents"


async def _flush_agents(sid: str, rollout: Path, rows, session, url: str,
                        bearer: str, namespace, deadline: float):
    """Send the agents this root spawned, each linked to ``codex-<sid>``.

    Called only after the root's own save was confirmed. Discovery reads the
    child rollouts; the server's tools are listed (through the
    ``_server_caps`` cache) only when some agent is actually due. Agent
    failures land in the root's state as ``agents_*`` fields, never in its
    ``fail_streak`` or ``last_error`` — a failed agent must not push a healthy
    session toward dormancy or raise the capture banner."""
    try:
        found = codex_agents.discover(rollout, rows=rows, log=_log)
    except Exception as exc:  # noqa: BLE001 — agents never cost the root its flush
        _log(f"agents not read ({type(exc).__name__}: {exc})")
        return None
    for tid, why in found.skipped:
        _log(f"agent {tid[:8]} of codex-{sid[:8]} skipped: {why}")
    if not found.agents:
        return None
    cache = _agents_cache(sid)
    planned = agent_capture.plan(cache, found.agents)
    if not planned.todo:
        _save_agent_state(sid, found.agents, planned.sent, None)
        return None
    caps = await asyncio.to_thread(server_caps.get, STATE_DIR, url,
                                   lambda: mcp_http.list_tools(url, bearer))
    if caps is None:
        _log(f"agents not sent: could not list the server's tools "
             f"({len(planned.todo)} waiting)")
        return None
    conv_id = f"codex-{sid}"

    async def send_one(agent, arguments: dict) -> bool:
        try:
            records = agent.load()
        except Exception as exc:  # noqa: BLE001 — one unreadable agent, not the root
            _log(f"agent {agent.agent_id[:8]} not read ({type(exc).__name__}: {exc})")
            return False
        sendable = redact_records(elide_oversized_tool_results(records))
        if not sendable:
            return True
        call = {"messages": sendable, **arguments,
                "source_platform": codex_reader.HOST, "flush": "now"}
        if namespace:
            call["namespace"] = namespace
        left = max(5.0, deadline - time.monotonic())
        try:
            res = await session.call_tool("import_conversation", arguments=call,
                                          timeout=left)
        except mcp_http.McpError as exc:
            _log(f"agent {agent.agent_id[:8]} import failed: {str(exc)[:160]}")
            if server_caps.names_agent_argument(str(exc)):
                server_caps.invalidate(STATE_DIR, url)
            return False
        if _verdict(res, arguments["conversation_id"]) != "ok":
            detail = " ".join(mcp_http.texts_of(res))
            if getattr(res, "isError", False) and server_caps.names_agent_argument(detail):
                server_caps.invalidate(STATE_DIR, url)
            _log(f"agent {agent.agent_id[:8]} import not confirmed")
            return False
        return True

    report = await agent_capture.send_agents(
        caps, parent_conv_id=conv_id, agents=planned.todo, cache_path=cache,
        deadline=deadline, send_one=send_one, log=_log, sent=planned.sent)
    _save_agent_state(sid, found.agents, agent_capture.load_sent(cache), report)
    return report


def _save_agent_state(sid: str, agents, sent: dict, report) -> None:
    """The root's agent summary, for capture_health's agent report: how many
    were found, how many landed, which are still running or waiting, and the
    last agent failure. Kept apart from last_error on purpose."""
    landed = [a for a in agents if str(sent.get(a.agent_id, "")).startswith(f"{a.revision}:")]
    running = [a.agent_id for a in agents if a.status == "running"]
    fields = {"agents_found": len(agents), "agents_sent": len(landed),
              "agents_running": running, "agents_checked_at": time.time()}
    if report is not None:
        fields["agents_pending"] = report.deferred
        if report.failed:
            fields["agents_failed"] = report.failed
            fields["agents_failed_at"] = time.time()
        elif report.sent:
            fields["agents_failed"] = []
        if report.gated:
            fields["agents_gated"] = True
    _save_state(sid, **fields)


async def _agents_only(sid: str, rollout: Path) -> None:
    """A rescan of a root that did not grow: its agents may have (a child
    can finish after its root's last Stop). Needs the root already captured
    — agents never go ahead of their parent."""
    rows = codex_reader.load_rollout(rollout)
    if not codex_agents.spawned(rows) and not _agents_cache(sid).exists():
        return
    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        return
    meta = rows[0].get("payload") if rows and isinstance(rows[0].get("payload"), dict) else {}
    cwd = meta.get("cwd")
    namespace = await asyncio.to_thread(_git_remote_basename, cwd) if _cwd_ok(cwd) else None
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)
    await _flush_agents(sid, rollout, rows, session, url, bearer, namespace,
                        time.monotonic() + FLUSH_TIMEOUT_S - 20)


def _root_captured(state: dict, size: int) -> bool:
    """Whether the root's current content is already confirmed saved."""
    return bool(state.get("last_ok_at")) and state.get("rollout_size") == size \
        and not state.get("unsupported") and not state.get("skipped_thread_source")


def _roots_with_running_agents(exclude: str) -> list[str]:
    """Other roots whose agents were last sent as running, newest first."""
    now = time.time()
    found = []
    try:
        caches = sorted(STATE_DIR.glob("*.agents"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    except OSError:
        return []
    for cache in caches:
        sid = cache.name[: -len(".agents")]
        try:
            if sid == exclude or now - cache.stat().st_mtime > AGENT_RESCAN_WINDOW_S:
                continue
        except OSError:
            continue
        sent = agent_capture.load_sent(cache)
        if any(isinstance(v, str) and ":running:" in v for v in sent.values()):
            found.append(sid)
        if len(found) >= AGENT_RESCAN_ROOTS:
            break
    return found


def _rescan_other_roots(current: str) -> None:
    """Re-check a few roots whose agents were last seen running: a child that
    ended after its root's last Stop is only noticed by a later session."""
    for sid in _roots_with_running_agents(current):
        found, _err = codex_reader.locate(sid)
        rollout = _contained(found) if found is not None else None
        if rollout is None:
            continue
        lock_fd = _acquire(sid, blocking=False)
        if lock_fd is None:
            continue
        try:
            if not _root_captured(_read_state(sid), codex_reader.rollout_size(rollout)):
                continue
            asyncio.run(asyncio.wait_for(_agents_only(sid, rollout),
                                         timeout=FLUSH_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 — a rescan never fails the hook
            _log(f"agent rescan of codex-{sid[:8]} failed: {type(exc).__name__}")
        finally:
            os.close(lock_fd)


async def _flush(sid: str, rollout: Path, size: int,
                 landed: dict | None = None) -> None:
    """``landed`` (when given) gets ``root=True`` the moment the root's own
    save is confirmed, so the caller can tell a timeout in the agent phase
    from a failed root flush."""
    # NB on shrink: the watermark is NOT reset here (before the send). Doing
    # so would drop the truncated rollout's content on a failed send —
    # `size <= watermark` would then hold and block re-flush. It is advanced
    # only on a CONFIRMED send (the success path below), so a failed shrink
    # send simply retries; the retry is bounded by the 60s cooldown (non-Stop)
    # and by dormancy after MAX_UNCONFIRMED failures (Stop included), so it is
    # a re-probe, not an every-event loop. The sweep is the final backstop.
    # Codex's own threads (guardian action-reviews, spawned subagents) are not
    # the person's work and must never be captured as sessions: they copy the
    # conversation under review, so they arrive looking like real transcripts.
    # Checked from the bounded header, before the parse — this runs per event.
    try:
        source = codex_reader.thread_source_of_path(rollout)
    except (OSError, ValueError) as exc:
        source = None                       # unreadable header: capture as usual
        _log(f"could not read thread_source ({exc!r}) — treating as own thread")
    if source in codex_reader.BOT_THREAD_SOURCES:
        _log(f"not this person's thread (thread_source={source!r}) — not captured")
        # Clear any earlier failure the way the other never-contacted-the-
        # server no-ops do: nothing will ever retry this session, so a stale
        # last_error would warn about a recovery that cannot come.
        _save_state(sid, skipped_thread_source=source, rollout_size=size,
                    last_error=None, last_error_at=0, fail_streak=0)
        return
    if source not in codex_reader.KNOWN_OWN_THREAD_SOURCES:
        # A Feature surface we have not seen before. It IS captured — the
        # variant means a product surface the person is using — but say so, so
        # a new kind gets classified deliberately rather than by silence. Not
        # a failure, so deliberately NOT a last_error: a health banner reading
        # "capture failed" over a session that captured fine is its own bug.
        _log(f"unfamiliar thread_source={source!r} — capturing it as the "
             f"person's work; classify it upstream if that is wrong")
    started = time.monotonic()
    from rewind.capture import prepare as prepare_rewind, chunks as rewind_chunks
    from rewind.state import Unavailable as RewindUnavailable
    try:
        records, meta, comparison = prepare_rewind(rollout, sid)
    except RewindUnavailable as exc:
        _note_failure(sid, str(exc))
        return
    state = _read_state(sid)
    pending_pr_urls, accepted_pr_urls, missing_pr_urls = (
        pr_provenance.queued_urls(state, records)
    )
    if missing_pr_urls:
        _log(f"{missing_pr_urls} direct gh pr create result(s) had no "
             "canonical GitHub PR URL")
    if pending_pr_urls or accepted_pr_urls:
        _save_state(
            sid,
            pending_pr_urls=pending_pr_urls,
            accepted_pr_urls=accepted_pr_urls,
        )
    sendable = redact_records(elide_oversized_tool_results(records))
    if not sendable:
        # Nothing to send. Empty is normal for a rollout with no user turns
        # yet — but records>0 with sendable==0 means EVERYTHING redacted
        # away, which is also what an over-broad redaction bug looks like, so
        # log the counts rather than silently advancing past real content.
        if records:
            _log(f"all {len(records)} record(s) redacted away — nothing to "
                 f"send (check redact rules if this recurs on real content)")
        # Advance the watermark anyway: growth gates purely on bytes, so
        # leaving it behind would re-parse and re-redact the whole (ever
        # larger) rollout on every later event just to discard it again. Any
        # future growth re-triggers a full re-read. fail_streak clears: the
        # server was never contacted (see _note_failure).
        _save_state(sid, rollout_size=size, fail_streak=0)
        return

    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        _log(f"no usable credential — skipping (run {skill_command('login')})")
        # Local auth gap, not a server failure — clear any failure run rather
        # than let a login blip tip the session toward dormancy.
        _save_state(sid, last_error="no_credential",
                    last_error_at=time.time(), fail_streak=0)
        return
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)

    cwd = meta.get("cwd")
    if _cwd_ok(cwd):
        # Off the loop, like resolve_bearer above: a git subprocess.
        namespace = await asyncio.to_thread(_git_remote_basename, cwd)
    else:
        if cwd:
            _log(f"ignoring unusable cwd from rollout: {str(cwd)[:60]!r}")
        namespace = None

    # No `agent_brain_id` and no `org_id`, ever: a session is captured into
    # the author's personal memory, never into a brain — the repo's room
    # included. Naming a brain on any flush makes the server pin the whole
    # session to it and re-extract its earlier turns there.
    arguments = {
        "messages": sendable,
        "conversation_id": f"codex-{sid}",
        "source_platform": codex_reader.HOST,
        "flush": "now",
    }
    if namespace:
        arguments["namespace"] = namespace
    provenance = pr_provenance.import_provenance(pending_pr_urls)
    if provenance:
        arguments["provenance"] = provenance
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        # Bound a semi-trusted session title (store content, like cwd): a
        # non-str from a corrupt meta is dropped rather than sent as-is, and a
        # runaway length is capped so it can't bloat every re-send.
        #
        # Redacted HERE because the title is derived from RAW records -- the
        # `redact_records` above covers only `sendable`, so a session whose
        # first prompt is `export MEMHUB_TOKEN=mhk_...` would otherwise ship
        # its key as the conversation's NAME, the most visible field there is.
        # Redact BEFORE the cap. Capping first can chop a straddling key
        # below `_SECRET`'s {16,} floor, and the surviving fragment then
        # matches nothing and ships in the clear. Verified both ways.
        arguments["title"] = redact_text(title.strip())[:200]

    # The account's latest rate-limit snapshot (plan, window usage, credits),
    # already reduced to an allowlist by the reader. It rides the call that is
    # happening anyway — repo_allow, bot-thread and redaction gating above are
    # unchanged — as a top-level argument a backend that predates it ignores.
    limits = meta.get("account_limits")
    if isinstance(limits, dict) and limits:
        arguments["account_limits"] = limits

    try:
        if comparison is None:
            res = await session.call_tool("import_conversation", arguments=arguments)
        else:
            for lineage_chunk in rewind_chunks(comparison):
                res = await session.call_tool("import_conversation",
                    arguments={**arguments, "messages": sendable if lineage_chunk["prefix_map_complete"] else [],
                               "comparison_lineage": lineage_chunk})
                # No ordinary fallback. The final persisted ACK alone advances capture.
                if getattr(res, "isError", False):
                    raise RewindUnavailable("comparison_lineage_rejected")
                if not lineage_chunk["prefix_map_complete"]:
                    pending = mcp_http.ack_of(res, f"codex-{sid}")
                    expected_offset = lineage_chunk["prefix_map_offset"] + len(lineage_chunk["prefix_mapping"])
                    if (not pending or pending.get("ack_through") is not None or
                            pending.get("comparison", {}).get("next_prefix_map_offset") != expected_offset):
                        raise RewindUnavailable("comparison_prefix_ack_unconfirmed")
    except mcp_http.McpRateLimited as e:
        _log(f"rate limited: {e}")
        _note_failure(sid, "rate_limited")
        return
    except mcp_http.McpError as e:
        _log(f"import failed: {e}")
        _note_failure(sid, f"mcp_error: {str(e)[:80]}")
        return

    # A returned call is NOT a persisted call — see _persisted. Advancing the
    # byte watermark on a 200-with-nothing-stored reply skips that span
    # forever, and on a session's last flush there is no later event to
    # re-send it.
    verdict = _verdict(res, f"codex-{sid}")
    if verdict == "unsupported":
        _log("server does not report ack_through — per-event flush is "
             f"dormant for this session; run {skill_command('import-session')} to "
             "capture it, or upgrade the server")
        _save_state(sid, unsupported=True, unsupported_at=time.time(),
                    fail_streak=0)
        return
    if verdict != "ok":
        _note_failure(sid, "compute_budget_exhausted"
                      if mcp_http.is_compute_budget_rejection(res)
                      else "unconfirmed_import")
        return

    ack = mcp_http.ack_of(res, f"codex-{sid}")
    if comparison is not None and sendable:
        from rewind.state import branch_path, read as rewind_read, write as rewind_write
        branch = rewind_read(branch_path(sid))
        branch["last_capture_watermark"] = {"event_id": sendable[-1]["uuid"], "capture_version": 1}
        rewind_write(branch_path(sid), branch)
    reconciled = pr_provenance.acknowledge_confirmed_import(
        pending_pr_urls,
        accepted_pr_urls,
        ack,
    )
    pending_pr_urls, accepted_pr_urls = reconciled
    if pending_pr_urls:
        # The transcript committed, but its optional URL write did not. Hold
        # the byte watermark so the next Stop can retry the same deduplicated
        # transcript with the still-pending URL.
        _save_state(sid, pending_pr_urls=pending_pr_urls,
                    accepted_pr_urls=accepted_pr_urls)
        _note_failure(sid, "unconfirmed_provenance")
        return
    _save_state(sid, rollout_size=size, last_ok_at=time.time(),
                last_error=None, last_error_at=0,
                # The re-probe worked: this server confirms after all.
                unsupported=False, unsupported_at=0,
                pending_pr_urls=pending_pr_urls,
                accepted_pr_urls=accepted_pr_urls,
                fail_streak=0)
    _log(f"flushed {len(sendable)} records → codex-{sid} (personal)")
    if landed is not None:
        landed["root"] = True
    # Agents go only now, after the root's own save was confirmed, inside
    # what is left of this flush's budget.
    try:
        # 20 s short of the flush's own timeout: the last agent call is bounded
        # by what is left, so it ends before wait_for would cancel the flush
        # and count a timeout against the root.
        await _flush_agents(sid, rollout, None, session, url, bearer, namespace,
                            started + FLUSH_TIMEOUT_S - 20)
    except Exception as exc:  # noqa: BLE001 — the root's save stands; never its streak
        _log(f"agents not sent ({type(exc).__name__}: {exc})")


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
        size = codex_reader.rollout_size(rollout)
    except ZstdUnavailable as e:
        # A compressed rollout on an interpreter that cannot read zstd. Not a
        # server failure (fail_streak untouched), but capture is blocked
        # until a zstd tool or Python 3.14 is there, so the banner says so.
        _log(f"{event}: {e} — skipping")
        _save_state(sid, last_error="zstd_unavailable", last_error_at=time.time())
        return 0
    except (OSError, ValueError) as e:
        _log(f"{event}: rollout unreadable ({e}) — skipping")
        return 0

    # One flush per session at a time — the whole check-then-act, so a
    # concurrent hook cannot read the same watermark, re-send the same
    # rollout, and race the write back.
    # Stop is the last-chance event (no SessionEnd), so it WAITS for a
    # concurrent flush rather than skip and never run again.
    lock_fd = _acquire(sid, blocking=(event == "Stop"))
    if lock_fd is None:
        _log(f"{event}: another flush is running for this session — skipping")
        return 0
    try:
        _flush_locked(event, payload, sid, rollout, size)
    finally:
        os.close(lock_fd)  # releases the flock
    if event == "Stop":
        _rescan_other_roots(sid)
    return 0


def _flush_locked(event: str, payload: dict, sid: str, rollout: Path,
                  size: int) -> int:
    """The gated flush, run while this session's flock is held."""
    state = _read_state(sid)
    if not should_flush(event, payload, state, size):
        if event == "Stop" and _root_captured(state, size):
            # Nothing new in the root, but its agents may have moved on.
            try:
                asyncio.run(asyncio.wait_for(_agents_only(sid, rollout),
                                             timeout=FLUSH_TIMEOUT_S))
            except Exception as e:  # noqa: BLE001 — never the root's failure
                _log(f"{event}: agent rescan error: {type(e).__name__}: {e}")
        return 0
    # Cooldown after a failure — including a TIMEOUT, which lands in the
    # broad handler below. The watermark only advances on success, so an
    # unhealthy backend would otherwise buy a full re-parse and re-redact of
    # the (ever-growing) rollout on every single hook, hammering the server
    # hardest exactly when it is already struggling. Nothing is lost: the
    # next event past the window retries the whole span.
    # Stop is EXEMPT: Codex has no SessionEnd hook, so the last Stop of a
    # session is the final chance to ship its tail — nothing follows to
    # retry it. Cooling that one down trades a lost conversation for a
    # saved request, which is backwards. PostToolUse milestones still
    # cool down; there is always another one.
    since_error = time.time() - (state.get("last_error_at") or 0)
    if event != "Stop" and since_error < ERROR_COOLDOWN_S:
        _log(f"{event}: {state.get('last_error')} {since_error:.0f}s ago — "
             f"cooling down ({ERROR_COOLDOWN_S:.0f}s)")
        return 0

    landed: dict = {}
    try:
        asyncio.run(asyncio.wait_for(_flush(sid, rollout, size, landed),
                                     timeout=FLUSH_TIMEOUT_S))
    except Exception as e:
        if landed.get("root"):
            # The root's save was already confirmed; only its agent phase ran
            # out (a cancel from wait_for is not an Exception inside _flush,
            # so it unwinds to here). Never the root's failure: no streak, no
            # last_error, no cooldown. The next Stop sends the agents.
            _log(f"{event}: agent phase cut off after the root's save "
                 f"({type(e).__name__}) — the next Stop sends them")
            _save_state(sid, agents_failed_at=time.time(),
                        agents_error=f"flush_error: {type(e).__name__}")
            return 0
        # Timeouts included: record the stamp so the cooldown applies to a
        # slow upload too, not just to server-reported errors.
        _log(f"{event}: flush error: {e}")
        _note_failure(sid, f"flush_error: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
