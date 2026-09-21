#!/usr/bin/env python3
"""The harness-tied memory sensor. ON by default since v0.69.0.

`MEMHUB_HARNESS_EXTRACT=0` (or off/false/no) turns the whole lane off. With it
on — which is now the default for every install — each turn is classified and
what survives is authored OFF the person's thread:

  Stop(turn N)  `stop`    takes the turn's closed error arcs from the rulebook
                          hook, spawns a detached `extract` child, and returns.
                          The child builds the redacted window, asks the
                          classifier, and on a signal records the MOMENT (turn,
                          kind, router hint, state stamp).
                          The same Stop also spawns a detached `author` pass
                          for whatever is already waiting, and says one line
                          about what a FINISHED pass left (§ report_outcomes).
  detached      `author`  one `claude -p --resume <owner> --fork-session` per
                          moment, under the plugin's OWN MCP server and
                          credential, filing through the create-rule skill.
                          Nothing it does touches the person's session.

Why detached rather than blocking the stop, which is what shipped before: the
block was itself a fix. A line injected at the next prompt reached the agent 19
times in five real sessions and produced 0 `create_rule` calls, because a line
stapled to the person's live request loses to the request. Blocking fixed the
attention problem and paid for it with the person's turn and context window.
A detached pass keeps what the block bought — nothing competes for the agent's
attention, because it is a different agent — and drops what it cost.

What the person sees: ONE line, when a rule was filed, or when a pass could not
RUN. A pass that ran and found no lesson says nothing. Silence is allowed to
mean "nothing worth filing" and nothing else — a broken pipeline that looks
exactly like a quiet one is the defect this lane keeps rediscovering.

Selection reads by REPO across every `*.moments.jsonl`, not by session id. A
session can never hand its own last moment (that classifier child is still
running when its Stop fires), and one that ends mid-work leaves the rest; read
per-session, those are lost, because nothing opens those files again. Measured
before this change, across 25 local sessions: 167 moments flagged, 86 handed,
**81 never handed**.

Whatever a pass decides, a proposal lands `proposed` and a person activates it:
nothing here fires or activates a rule.

Files, under $MEMHUB_HARNESS_DIR (default ~/.config/memhub-plugin/harness),
all created private:

  <session>.moments.jsonl    flagged moments, then one row per outcome
                             (`handed` only once a pass DECIDED — a failed
                             pass leaves the moment for a later drain, because
                             a watermark past work nobody did is the silent
                             version of this lane's bug). Append-only: several
                             lanes write it at once.
  <session>.meta.json        last extracted turn, repo, cwd, transcript cursor
  <session>.meta.json.lock   serializes the meta file's read-merge-write
  <session>.turn-*.claim     the turn an extract child already took
  <session>.drain.claim      the author pass holding this session
  stop.log / extract.log / author.log   one line per step, never prompt text

What default-on costs the person, said plainly because they did not ask for
it: one classifier call per flagged turn against the MemHub backend, and one
`claude -p --resume` per moment the drain authors — that second one spends
THEIR model quota, not the server's. One machine's dogfood ran 32 such children
in two hours. `MEMHUB_HARNESS_EXTRACT=0` stops both.

Every path fails open and silent: a broken sensor must never touch the tool
call or the session. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness_extract as hx  # noqa: E402

#: Two bounds used to live here — at most 8 blocked stops per session, and
#: never a moment more than 3 turns old. Both were right for a design that
#: INTERRUPTS: they capped what one hand-off cost the person, because every
#: hand-off blocked their stop. Measured across 25 local sessions, they were
#: also where the input went: 167 moments flagged, 86 handed, **81 never
#: handed** — one session flagged 33 and handed exactly 8. Nothing ever read
#: those files again, so each bound doubled as a deletion.
#:
#: They are gone with the block. Authoring is detached now, so an extra moment
#: costs the person nothing and there is nothing to ration. The intent the cap
#: really carried — do not drop 33 rules into a reviewer's queue — belongs at
#: the FILING step and lives there instead (spec §4.3.4); rationing at hand-off
#: conflated "do not interrupt the person" with "do not flood the reviewer".
FILING_BUDGET_PER_PASS = 8   # rules one drain may propose; a reviewer's bound
#: Past this a moment is dropped rather than authored: a lesson about a branch
#: two weeks gone is not worth a review round. Dropped WITH a log line — silent
#: dropping is this lane's whole bug.
MOMENT_TTL_S = 14 * 24 * 3600
#: One drain per session at a time. A holder that died leaves the claim behind,
#: so it is breakable once it stops being touched.
DRAIN_CLAIM_STALE_S = 900


# --------------------------------------------------------------- plumbing
def _log(msg: str) -> None:
    try:
        path = hx.log_path("stop.log")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def meta_path(session: str) -> Path:
    return hx.session_file(session, ".meta.json")


def moments_path(session: str) -> Path:
    return hx.session_file(session, ".moments.jsonl")


def load_meta(session: str) -> dict:
    try:
        got = json.loads(meta_path(session).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _publish(path: Path, text: str) -> None:
    """Atomic and 0600, through `atomic_write.publish` beside this file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        import atomic_write  # noqa: PLC0415
        atomic_write.publish(path, text)
    except Exception:
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)


def _meta_lock(session: str):
    """The rulebook hook's session-state lock, taken on this session's meta
    file. None past its short wait or with no hook: it fails open."""
    rh = hx._hook()
    if rh is None or not hasattr(rh, "_state_lock"):
        return None
    try:
        path = meta_path(session)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return rh._state_lock(str(path))
    except Exception:
        return None


def _release(lock) -> None:
    if lock is None:
        return
    try:
        hx._hook().portable_lock.unlock(lock.fileno())
    except Exception:
        pass
    lock.close()


def save_meta(session: str, *, advance_turn: int | None = None, **fields) -> dict:
    """Merge `fields` into the session's meta, the read and the write under one
    lock. With `advance_turn` the write is a turn's: it moves `last_turn`
    forward, and a child for an OLDER turn than the one recorded (two children
    overlap on a slow classifier) changes nothing."""
    lock = _meta_lock(session)
    try:
        meta = load_meta(session)
        if advance_turn is not None:
            if advance_turn < int(meta.get("last_turn") or 0):
                return meta
            fields["last_turn"] = advance_turn
        meta.update(fields)
        meta["session_id"] = session
        _publish(meta_path(session), json.dumps(meta, indent=1, default=str))
        return meta
    finally:
        _release(lock)


def env_name() -> str:
    """Which MemHub the stamp's `env` names, derived from the plugin's own
    backend URL rather than configured a second time."""
    try:
        from _memhub_auth import default_url  # noqa: PLC0415
        host = default_url()
    except Exception:
        return "unknown"
    return "staging" if "staging" in host else "production"


def repo_of(cwd: str) -> str:
    rh = hx._hook()
    if rh is None or not cwd:
        return ""
    try:
        return rh.repo_info(cwd)[0] or ""
    except Exception:
        return ""


def _read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_subagent(payload: dict) -> bool:
    """A subagent's hook call carries a top-level `agent_id`. Its turns are not
    the person's, and its prompts must not take the main agent's moment."""
    return bool(str(payload.get("agent_id") or "").strip())


# --------------------------------------------------------------- stop lane
def cmd_stop(payload: dict) -> int:
    """Millisecond budget: two small file reads, one spawn, no transcript and
    no network. The hook is SYNCHRONOUS, so what it records is the turn's
    boundary: Claude Code appends no queued prompt and runs no next-turn tool
    hook until it returns. (The shell gate in claude-hooks.json keeps it free
    with the flag off.)

    The child is bounded by the transcript's size taken here, so whatever the
    blocked continuation appends is not this turn's. The handoff is chosen
    BEFORE the child exists, so it can only ever hand an earlier turn."""
    session = str(payload.get("session_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not session or not transcript or not os.path.isfile(transcript):
        return 0
    if _is_subagent(payload):
        return 0
    if payload.get("stop_hook_active"):
        # The blocked continuation's own Stop: no extraction and no second
        # block, but its error arcs (the create-rule flow runs commands) are
        # drained here, or the next ordinary turn would inherit them and be
        # classified on failures it never had (Codex, #230).
        rh = hx._hook()
        if rh is not None and hasattr(rh, "take_error_arcs"):
            try:
                rh.take_error_arcs(session)
            except Exception:
                pass
        return 0
    args = ["extract", "--session", session, "--transcript", transcript, "--cwd", cwd]
    # The transcript's size NOW is the turn boundary, taken inside the
    # synchronous hook. A queued prompt can be appended before the detached
    # child opens the file, and the child must still classify the turn that
    # stopped, not the one that just began.
    try:
        args += ["--upto", str(os.path.getsize(transcript))]
    except OSError:
        pass
    # The turn's error arcs are taken HERE, at the boundary, not by the child:
    # by the time a detached child gets to them the next turn may have added
    # its own.
    rh = hx._hook()
    if rh is not None and hasattr(rh, "take_error_arcs"):
        try:
            arcs = rh.take_error_arcs(session)
        except Exception:
            arcs = []
        if arcs:
            arcs_path = hx.session_file(session, f".arcs-{time.time_ns()}.json")
            try:
                _publish(arcs_path, json.dumps(arcs))
                args += ["--arcs", str(arcs_path)]
            except Exception:
                pass
    # Choose the handoff before spawning: a child that wins the race could
    # otherwise append THIS turn's moment first, and the block would hand the
    # turn that is stopping (Codex, #230). Selection only reads the moments file
    # and appends a `handed` row; the boundary above is already taken.
    # What a finished drain left for the person — the only thing this lane
    # ever says to them, and it happens at a Stop because a detached child has
    # no channel of its own.
    said = report_outcomes(session)
    if said:
        print(json.dumps({"systemMessage": said}))
    rc = hand_off(session)
    hx.spawn_detached(args, script=Path(__file__).resolve(), log_name="stop.log")
    return rc


def _claim_turn(session: str, marker: str) -> bool:
    """Exactly one child takes a turn. Two Stops for one turn, or two children
    racing on a slow classifier, would otherwise both spend a call on it."""
    digest = hashlib.sha1(marker.encode("utf-8")).hexdigest()[:12]
    claim = hx.session_file(session, f".turn-{digest}.claim")
    try:
        claim.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return True             # fail open: an unwritable claim does not stop the sensor
    os.close(fd)
    prefix = hx.session_file(session, ".turn-").name
    for old in claim.parent.glob(f"{prefix}*.claim"):
        if old != claim:
            try:
                old.unlink()
            except OSError:
                pass
    return True


def read_turns(transcript: str, cursor, upto: int = -1) -> list[dict]:
    """The transcript's turns up to byte `upto`. A long session must not be
    rescanned from byte zero at every Stop, so each child leaves a cursor at the
    start of the turn before the one it extracted, and the next reads from
    there: the two turns a window needs, and the new one. A cursor that no
    longer lands on the human message it recorded (a rewritten or replaced
    transcript), or that leaves fewer than two turns, falls back to a full
    read."""
    def bounded(turns: list[dict]) -> list[dict]:
        return [t for t in turns if upto < 0 or int(t.get("offset") or 0) < upto]

    try:
        start = int(cursor.get("offset") or 0)
        before = int(cursor.get("before") or 0)
        uuid = str(cursor.get("uuid") or "")
    except (AttributeError, TypeError, ValueError):
        start, before, uuid = 0, 0, ""
    if start > 0 and uuid and (upto < 0 or start < upto):
        part = bounded(hx.turns_from_transcript(transcript, start=start, before=before))
        if len(part) >= 2 and part[0].get("offset") == start and part[0].get("uuid") == uuid:
            return part
    return bounded(hx.turns_from_transcript(transcript))


def cmd_extract(session: str, transcript: str, cwd: str, arcs_file: str = "",
                upto: int = -1) -> int:
    """The child: the turn in progress at byte `upto` (the transcript's size
    when Stop fired), once. Its records flushed after Stop still belong to it;
    a turn whose human message starts at or past `upto` does not. With no
    boundary, the last turn."""
    arcs: list = []
    if arcs_file:
        try:
            got = json.loads(Path(arcs_file).read_text(encoding="utf-8"))
            arcs = got if isinstance(got, list) else []
        except (OSError, ValueError):
            arcs = []
        try:
            Path(arcs_file).unlink()
        except OSError:
            pass
    try:
        turns = read_turns(transcript, load_meta(session).get("scan"), upto)
    except (OSError, ValueError) as exc:
        _log(f"extract {session[:8]}: cannot read transcript: {type(exc).__name__}")
        return 0
    if not turns:
        return 0
    last, prev = turns[-1], (turns[-2] if len(turns) > 1 else None)
    if not _claim_turn(session, f"{last.get('n')}:{last.get('uuid', '')}"):
        return 0
    cwd = cwd or last.get("cwd") or ""
    repo = repo_of(cwd)
    stats = hx.new_stats()
    trace = hx.Trace(str(hx.log_path("extract.log")))
    try:
        hx.extract_turn(last, prev, session=session, cwd=cwd, repo=repo,
                        env_name=env_name(), stats=stats, trace=trace,
                        out_path=moments_path(session), arcs=arcs)
    finally:
        trace.close()
    scan = ({"offset": prev.get("offset"), "before": int(prev.get("n") or 1) - 1,
             "uuid": prev.get("uuid")} if prev and prev.get("uuid") else None)
    save_meta(session, advance_turn=int(last.get("n") or 0), repo=repo, cwd=cwd,
              last_stop_at=time.time(), scan=scan)
    _log(f"extract {session[:8]} t{last.get('n')}: sent={stats['turns_sent']} "
         f"moment={stats['moments']} reason={stats['reason'] or '-'} arcs={len(arcs)}")
    return 0


# ------------------------------------------------------------- prompt lane
def proposal_scope(moment: dict, fallback_repo: str = "") -> list[str]:
    """The repositories a proposal is scoped to: every one the turn's actions
    worked in, else the stamp's repo, else the session's."""
    state = moment.get("state") or {}
    touched = [r for r in (state.get("touched_repos") or []) if isinstance(r, str) and r]
    if touched:
        return touched
    repo = state.get("repo") or fallback_repo
    return [repo] if repo else []


BLOCK_PREFIX = "MemHub harness: before you stop"


def block_reason(session: str, moment: dict, repo: str = "") -> str:
    """What the blocked agent reads. One job: point at the skill, with the
    provenance only this line has. `repo` is the session's, used only when the
    moment's own stamp names none.

    The TEST it states is "not already a RULE", not "not already written
    down". Those were collapsed, and the collapse cost real lessons: a live
    e2e hit a trap documented at CONTRIBUTING.md:123 and declined to file it —
    yet the documentation is what had just failed to prevent the trap. Prose
    nobody reads has no enforcement and no fire count; that is what a rule
    adds, and `source='claude_md_import'` on 18 of the book's rules is the
    same conversion done by hand. What still disqualifies a moment is being a
    twin of an existing rule, or being narration of docs that nothing tripped
    over — the `TEAM_DIRECTIVE_CAPTURE` failure of 2026-09-14.

    HOW to file a rule — the rulebook question, the twin check, the engine
    shapes, advise vs gate, the proof — lives in `skills/create-rule/SKILL.md`
    and is not restated here. The line that restated it drew five of six Codex
    findings on #222: whatever it did not copy the harness path silently
    dropped, and whatever it did copy drifted from the skill.

    The STAMP is NOT carried any more. `reason` is documented as feedback for
    the model, but the host also renders it to the person behind a "Stop hook
    feedback:" prefix, and ~400 characters of `state={...}` JSON is the bulk of
    what they were reading. The stamp lives in the session's moments file,
    which the skill reads; this line names the file instead of quoting it.
    The server still refuses a `session_draft` without `repo`, `session_id`,
    `turn`, `hook_version` and `at` — the skill supplies them from there."""
    turn = moment.get("turn")
    kind = moment.get("kind") or "a signal"
    scope = proposal_scope(moment, repo)
    narrow = (f" The turn worked in {len(scope)} repositories: keep in scope_repos only "
              f"the ones the lesson is about.") if len(scope) > 1 else ""
    hint = f" (router: {moment['hint']})" if moment.get("hint") else ""
    derivable = (" The classifier thinks it may already be written down, so check "
                 "first.") if moment.get("derivable") else ""
    ref = moment.get("source_ref") or session
    return (
        f"{BLOCK_PREFIX}: turn {turn} was flagged as {kind}{hint}.{derivable} Run the "
        f"memhub create-rule skill on source_ref=\"{ref}\", scope_repos="
        f"{json.dumps(scope)} — its Harness-draft section holds the test for whether "
        f"this is a lesson, where the stamp comes from, and how to file it.{narrow} "
        f"If it is not a lesson, or you cannot file it, say nothing to the person "
        f"about this turn."
    )


def _moment_key(moment: dict) -> str:
    return str(moment.get("source_ref") or f"turn-{moment.get('turn')}")


def _repo_of(moment: dict) -> str:
    return str((moment.get("state") or {}).get("repo") or "")


def _stamped_at(moment: dict) -> float:
    """Wall-clock seconds from the moment's state stamp, 0.0 when unreadable.

    Turn numbers are per-session, so they can neither order nor age a moment
    that came out of another session's file; the stamp is the only comparable
    clock, and every moment already carries one."""
    raw = str((moment.get("state") or {}).get("at") or "")
    if not raw:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def pending(session: str, repo: str, now: float) -> list[tuple[Path, dict]]:
    """Every un-drained moment for THIS repo, across every session's file.

    Reading one file — its own — is what made the bounds above into deletions,
    and it is also the only reason a session's LAST moment was unreachable:
    that moment's classifier child is still running when its Stop fires, so no
    Stop of that session can ever take it. Whatever else changes, selection has
    to be able to see another session's file."""
    out: list[tuple[Path, dict]] = []
    try:
        files = sorted(moments_path(session).parent.glob("*.moments.jsonl"))
    except OSError:
        return out
    for path in files:
        try:
            rows = hx.read_jsonl(path)
        except OSError:
            continue
        done = {str(r["handed"]) for r in rows if r.get("handed")}
        expired = 0
        for m in rows:
            if m.get("handed") or not isinstance(m.get("turn"), int):
                continue
            if _moment_key(m) in done or _repo_of(m) != repo:
                continue
            stamped = _stamped_at(m)
            if stamped and now - stamped > MOMENT_TTL_S:
                expired += 1
                continue
            out.append((path, m))
        if expired:
            _log(f"drain {session[:8]}: {expired} moment(s) past the TTL in "
                 f"{path.name[:8]}")
    # Newest stamp first, then the later turn: two moments from one session
    # share a stamp often enough that sorting on the stamp alone would leave
    # the order to however the file happened to list them.
    out.sort(key=lambda pm: (_stamped_at(pm[1]), int(pm[1].get("turn") or 0)),
             reverse=True)
    return out


def take_drain_claim(session: str, now: float):
    """`<session>.drain.claim`, O_EXCL — one drain per session at a time.

    The same shape the per-turn claim already uses. A holder that died leaves
    the file behind, so a claim nobody has touched for DRAIN_CLAIM_STALE_S is
    breakable; otherwise a single crash would stop this session draining ever
    again."""
    path = hx.session_file(session, ".drain.claim")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and now - path.stat().st_mtime > DRAIN_CLAIM_STALE_S:
            path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        return path
    except FileExistsError:
        return None
    except OSError:
        return None


def hand_off(session: str) -> int:
    """At Stop: spawn a detached child to author what is waiting, and RETURN.

    This used to block the stop and make the live agent do the authoring. The
    block was chosen over a line injected at the next prompt for a measured
    reason — that lane reached the agent 19 times in five sessions and produced
    0 `create_rule` calls, because a line stapled to the person's live request
    loses to the request. Detaching keeps what the block bought (nothing
    competes for the agent's attention, because it is a different agent) and
    drops what it cost (the person's turn, and their context window).

    Only appends here: the extract child writes to these files at any time and
    a read-then-replace would delete a moment landing in between."""
    meta = load_meta(session)
    repo = str(meta.get("repo") or "")
    if not repo:
        # Meta is written by the extract CHILD, so a session's first Stop does
        # not know its repo yet and its second does. Deriving it here means
        # running `git` inside a synchronous hook whose budget is two file
        # reads and a spawn.
        return 0
    now = time.time()
    waiting = pending(session, repo, now)
    if not waiting:
        return 0
    claim = take_drain_claim(session, now)
    if claim is None:
        # A drain is already running for this session. It reads the files when
        # it gets there, so anything parked meanwhile is its problem, not this
        # Stop's — nothing is dropped by declining here.
        return 0
    batch = waiting[:FILING_BUDGET_PER_PASS]
    refs = [_moment_key(m) for _, m in batch]
    hx.spawn_detached(["author", "--session", session, "--claim", str(claim),
                       "--refs", ",".join(refs)],
                      script=Path(__file__).resolve(), log_name="author.log")
    _log(f"drain {session[:8]}: spawned for {len(batch)} of {len(waiting)} waiting")
    return 0


# ------------------------------------------------------------- author lane
MCP_SERVER_NAME = "memhub"
#: What the child may call. Anything not named here is refused outright in a
#: headless run, with nobody to grant it — which reads exactly like "no access"
#: and cost a whole debugging round to tell apart.
CHILD_TOOLS = ("Bash", "Read", "Glob", "Grep", "Skill", "Agent",
               f"mcp__{MCP_SERVER_NAME}__create_rule",
               f"mcp__{MCP_SERVER_NAME}__list_rules",
               f"mcp__{MCP_SERVER_NAME}__list_rulebooks",
               # `list_orgs` is not optional. Without it the first live run
               # could not tell "this org has no rulebook" from "I was refused",
               # and reported the refusal as the reason — a diagnosis the person
               # then cannot act on.
               f"mcp__{MCP_SERVER_NAME}__list_orgs",
               # Step 0 rule 3 falls back to the repo's own book and creates it
               # when absent. Without these the child is refused at exactly the
               # step the skill sends it to — the same way a missing `list_orgs`
               # turned "this org has no rulebook" into "I was refused".
               # `list_teammates` is how it finds its own id, which an ORG ADMIN
               # must pass to `create_rulebook` or the new book binds nobody.
               f"mcp__{MCP_SERVER_NAME}__create_rulebook",
               f"mcp__{MCP_SERVER_NAME}__list_teammates")
RESULT_PREFIX = "HARNESS-RESULT:"
AUTHOR_TIMEOUT_S = 900


def claude_bin() -> str:
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def write_child_mcp_config(directory: Path) -> tuple[Path, str]:
    """The plugin's OWN server and credential, written where the child reads it.

    A headless child does NOT inherit the plugin's MCP server; it inherits the
    person's claude.ai connectors. Measured 2026-09-18: those pointed at
    PRODUCTION while the plugin's own credential is staging's, so a drain that
    simply used what it found would file team rules into an environment nobody
    named. `--strict-mcp-config` alongside this is what stops that happening by
    accident rather than by policy."""
    from _memhub_auth import resolve_url_and_auth  # noqa: PLC0415
    url, headers, _ = resolve_url_and_auth(interactive=False)
    cfg = {"mcpServers": {MCP_SERVER_NAME: {
        "type": "http", "url": url,
        "headers": {"Authorization": headers["Authorization"]}}}}
    path = directory / "mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path, url


def author_prompt(session: str, moment: dict, repo: str) -> str:
    """What the child is asked.

    The body is `block_reason` — the same handoff the blocking Stop spent five
    review rounds getting right, carrying `source_ref`, `scope_repos`, the
    pointer to the skill's Harness-draft section and the derivable hint. This
    lane adds only what is true of a DETACHED reader: nobody can answer a
    question, and the result has to come back on a line a program can read."""
    return (
        block_reason(session, moment, repo)
        + "\n\nYou are a detached pass: there is no one to answer a question, so "
        "follow the Harness-draft section's arithmetic and ask nothing. If it is "
        "not a lesson, file nothing.\n\n"
        f"End your reply with one line and nothing after it:\n"
        f"  {RESULT_PREFIX} filed <rule_id> | <the rule's title> | <what it catches, "
        "in one short clause — the trigger in the words a person would use, e.g. "
        "'a `claude -p` without --strict-mcp-config'>\n"
        f"  {RESULT_PREFIX} none <one short reason>\n"
        f"  {RESULT_PREFIX} failed <one short reason>   (use this only if you COULD "
        "NOT file — no rulebook server, a refused tool, an expired credential — "
        "and never for 'there was no lesson')")


def parse_result(stdout: str) -> tuple[str, dict]:
    """(outcome, fields) from the child's last contract line, or ('failed', …).

    A filed rule carries `rule_id`, `title` and `catches`, pipe-separated —
    because a bare id tells the person nothing about what they are being asked
    to review, and the hook that shows it has no budget to look one up.

    An unparseable answer is a FAILURE, never a quiet 'none'. Reading it as
    'nothing here' is the defect this whole lane exists to stop: a broken
    pipeline that looks exactly like a quiet one."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(RESULT_PREFIX):
            rest = line[len(RESULT_PREFIX):].strip().split(None, 1)
            head = (rest[0] if rest else "").lower()
            detail = rest[1].strip() if len(rest) > 1 else ""
            if head == "filed":
                parts = [x.strip() for x in detail.split("|")]
                # A title is what makes the line readable, but a child that
                # gives only an id is still a FILING — degrade the rendering,
                # never the outcome.
                return head, {"rule_id": parts[0] if parts else "",
                              "title": parts[1] if len(parts) > 1 else "",
                              "catches": parts[2] if len(parts) > 2 else ""}
            if head in ("none", "failed"):
                return head, {"detail": detail}
            break
    return "failed", {"detail": "the child gave no result line"}


def run_author(session: str, moment: dict, repo: str, mcp_cfg: Path) -> tuple[str, dict]:
    owner = str((moment.get("state") or {}).get("session_id") or "")
    if not owner:
        return "failed", {"detail": "the moment carries no session id to resume"}
    with tempfile.TemporaryDirectory(prefix="memhub-drain-") as scratch:
        env = dict(os.environ,
                   # Its capture and harness lanes stay silent: the forked
                   # transcript is a copy of the person's, and must neither
                   # ship as a second conversation nor be sensed again. The
                   # two lanes read this, not EXTRACT below, because a
                   # settings.json `env` overrides what the child inherits.
                   # Its rulebook hook still runs, for the forward test.
                   MEMHUB_HARNESS_CHILD="1",
                   MEMHUB_HARNESS_EXTRACT="0",
                   # its forward test arms a candidate in ITS OWN base
                   MEMHUB_RULEBOOK_BASE=os.path.join(scratch, "rulebook"))
        argv = [claude_bin(), "-p", author_prompt(session, moment, repo),
                "--resume", owner, "--fork-session",
                "--mcp-config", str(mcp_cfg), "--strict-mcp-config",
                "--permission-mode", "acceptEdits",
                "--allowedTools", ",".join(CHILD_TOOLS)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=AUTHOR_TIMEOUT_S, env=env,
                                  stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return "failed", {"detail": "no claude CLI on PATH"}
        except subprocess.TimeoutExpired:
            return "failed", {"detail": f"the child did not finish in {AUTHOR_TIMEOUT_S}s"}
        except OSError as exc:
            return "failed", {"detail": f"the child would not start ({exc.__class__.__name__})"}
    if proc.returncode != 0:
        return "failed", {"detail": f"the child exited {proc.returncode}"}
    return parse_result(proc.stdout)


def cmd_author(session: str, claim: str, refs: list[str]) -> int:
    """The detached pass. One `claude -p` per moment, then the outcome written
    back beside the moment it came from.

    `handed` is appended ONLY for a moment that was actually decided. A failed
    pass leaves it untouched so a later drain retries it: a watermark that
    advances past work nobody did is the silent, unrecoverable version of this
    lane's bug."""
    try:
        meta = load_meta(session)
        repo = str(meta.get("repo") or "")
        wanted = [r for r in refs if r]
        now = time.time()
        waiting = {_moment_key(m): (path, m) for path, m in pending(session, repo, now)}
        try:
            with tempfile.TemporaryDirectory(prefix="memhub-mcp-") as cfg_dir:
                mcp_cfg, url = write_child_mcp_config(Path(cfg_dir))
                env = env_name()
                _log(f"author {session[:8]}: {len(wanted)} moment(s) against {url} ({env})")
                for ref in wanted:
                    got = waiting.get(ref)
                    if got is None:            # drained by someone else meanwhile
                        continue
                    path, moment = got
                    outcome, fields = run_author(session, moment, repo, mcp_cfg)
                    row = {"outcome": outcome, "ref": ref, **fields,
                           # WHICH MemHub. The first live run filed against
                           # production because `resolve_url_and_auth` hands
                           # back the plugin's default and nothing said so out
                           # loud; a failure the person cannot place is a
                           # failure they cannot fix.
                           "env": env, "by": session, "at": time.time()}
                    if outcome != "failed":
                        row["handed"] = ref     # decided; never retried
                    hx.append_jsonl(path, row)
                    _log(f"author {session[:8]}: {ref} -> {outcome} ({detail[:60]})")
        except Exception as exc:               # noqa: BLE001
            # Never a quiet nothing: the pass could not run, and the next Stop
            # says so on the health channel.
            hx.append_jsonl(moments_path(session),
                            {"outcome": "failed", "detail": f"drain could not start ({exc})",
                             "by": session, "at": time.time()})
            _log(f"author {session[:8]}: could not start — {exc}")
    finally:
        try:
            Path(claim).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def unreported(session: str) -> list[dict]:
    """Outcomes this session has not yet shown the person."""
    rows = hx.read_jsonl(moments_path(session)) if moments_path(session).is_file() else []
    said = {str(r["reported"]) for r in rows if r.get("reported")}
    return [r for r in rows
            if r.get("outcome") and f"{r.get('ref')}@{r.get('at')}" not in said]


def report_outcomes(session: str) -> str:
    """The person's lines, and the ONLY thing this lane says to them.

    A filed rule is named and its trigger said in a person's words, because a
    bare id asks someone to review something without telling them what it is.
    What is NOT said: any claim that the rule helped. It has fired for nobody —
    a proposed rule is served to no agent — and whether a rule helps is the
    fire ledger's question, answered later and per rule, not here.

    A pass that could not RUN is named too. A pass that ran and found no lesson
    says nothing: that is the quiet the design is for, and it stays readable in
    the moments file for anyone who asks."""
    lines = []
    for row in unreported(session):
        env = row.get("env") or "an unnamed environment"
        if row.get("outcome") == "filed":
            title = (row.get("title") or "").strip()
            head = f'"{title}"' if title else f"rule {row.get('rule_id') or '?'}"
            lines.append(f"MemHub filed a rule for review — {head}")
            if row.get("catches"):
                lines.append(f"   catches: {row['catches']}")
            if title and row.get("rule_id"):
                lines.append(f"   {row['rule_id']} in {env}")
            lines.append("   it fires for nobody until someone activates it")
        elif row.get("outcome") == "failed":
            lines.append("MemHub: could not review a flagged moment against "
                         f"{env} — {row.get('detail')}")
        hx.append_jsonl(moments_path(session),
                        {"reported": f"{row.get('ref')}@{row.get('at')}"})
    return "\n".join(lines)


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mode", choices=("stop", "extract", "author"))
    p.add_argument("--claim", default="")
    p.add_argument("--refs", default="")
    p.add_argument("--session", default="")
    p.add_argument("--transcript", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--arcs", default="")
    p.add_argument("--upto", type=int, default=-1)
    p.add_argument("--ref", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not hx.extract_enabled():
        if args.mode == "stop":
            try:
                sys.stdin.read()          # drain the hook payload, say nothing
            except Exception:
                pass
        return 0
    if args.mode == "stop":
        return cmd_stop(_read_payload())
    if args.mode == "extract" and args.session:
        return cmd_extract(args.session, args.transcript, args.cwd, args.arcs, args.upto)
    if args.mode == "author" and args.session:
        return cmd_author(args.session, args.claim,
                          [r for r in args.refs.split(",") if r])
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:                 # noqa: BLE001 — silent, exit 0
        if os.environ.get("MEMHUB_HARNESS_DEBUG"):
            import traceback
            traceback.print_exc()
        rc = 0
    sys.exit(rc or 0)
