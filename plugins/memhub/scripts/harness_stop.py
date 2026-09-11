#!/usr/bin/env python3
"""The harness-tied memory sensor. FLAGGED OFF by default.

Nothing in this file runs unless `MEMHUB_HARNESS_EXTRACT` is on. With it on,
MemHub classifies each turn's moment and the coding agent that lived the turn
decides whether it holds a lesson:

  Stop(turn N)       `stop`     returns in milliseconds. Takes the turn's
                                closed error arcs from the rulebook hook, then
                                spawns a detached `extract` child, which builds
                                the redacted window, asks the classifier, and
                                on a signal records the MOMENT (turn, kind,
                                router hint, state stamp).
  next user prompt   `prompt`   hands the newest un-handed moment to the agent
                                in ONE injected line: which turn, what kind,
                                and the stamp to pass. The agent decides
                                whether there is a lesson, asks the person if
                                unsure, and files it with the memhub
                                `create_rule` tool. It lands `proposed`, and a
                                person activates it.

A moment the classifier has not answered for by the next prompt is handed on a
later prompt while it is still recent. A moment nobody was handed (the
terminal closed, a host with no prompt hook) stays in its file. Nothing here
fires a rule and nothing here activates one.

Files, under $MEMHUB_HARNESS_DIR (default ~/.config/memhub-plugin/harness),
all created private:

  <session>.moments.jsonl    flagged moments, then a `handed` row per nudge
                             (append-only: two lanes write it at once)
  <session>.meta.json        last extracted turn, repo, cwd, transcript cursor
  <session>.meta.json.lock   serializes the meta file's read-merge-write
  <session>.turn-*.claim     the turn an extract child already took
  stop.log / extract.log     one line per step, never prompt text

Every path fails open and silent: a broken sensor must never touch the tool
call or the session. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness_extract as hx  # noqa: E402

NUDGE_MAX_AGE_TURNS = 3      # an older moment is left in its file, never nudged stale
NUDGE_CAP_PER_SESSION = 8    # at most this many nudges in one session
NUDGES_PER_PROMPT = 1        # one line, the newest moment


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
    """Millisecond budget: one small state read, one spawn, no transcript and
    no network."""
    session = str(payload.get("session_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not session or not transcript or not os.path.isfile(transcript):
        return 0
    if payload.get("stop_hook_active") or _is_subagent(payload):
        return 0
    args = ["extract", "--session", session, "--transcript", transcript, "--cwd", cwd]
    # The transcript's size NOW is the turn boundary. A queued prompt can be
    # appended before the detached child opens the file, and the child must
    # still classify the turn that stopped, not the one that just began.
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
    hx.spawn_detached(args, script=Path(__file__).resolve(), log_name="stop.log")
    return 0


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


def nudge_line(session: str, moment: dict, repo: str = "") -> str:
    """The one line the agent reads. It carries the stamp verbatim, so the rule
    the agent files is stamped by the harness and never typed. `repo` is the
    session's, used only when the moment's own stamp names none."""
    kind = moment.get("kind") or "a signal"
    scope = proposal_scope(moment, repo)
    narrow = (f" The turn worked in {len(scope)} repositories: keep in scope_repos only "
              f"the ones the lesson is about.") if len(scope) > 1 else ""
    hint = f" (router: {moment['hint']})" if moment.get("hint") else ""
    derivable = (" — the classifier thinks it may already be written down in the repo, "
                 "so check before proposing") if moment.get("derivable") else ""
    stamp = json.dumps(moment.get("state") or {}, ensure_ascii=False, default=str)
    return (
        f"MemHub harness: your previous turn (turn {moment.get('turn')}) was classified as "
        f"{kind}{hint}{derivable}. If it carries a lesson that would change what an agent "
        f"DOES next time, is not already in the repo, its docs, CLAUDE.md or the rulebook, "
        f"is not project state, and will still be true next month, propose it now with the "
        f"memhub create_rule tool. Resolve the rulebook first with list_rulebooks: with one, "
        f"pass its rulebook_id; with several, ask the user which; with none, say so and stop "
        f"(no list_rulebooks tool: omit rulebook_id). Then look for a twin: list_rules with no "
        f"rulebook_id, include_retired=true and limit=200, paging with offset while has_more. "
        f"A twin in that rulebook: replace it with supersedes_rule_id, or file nothing; a twin "
        f"in another rulebook: tell the user. Then pass title (a short noun phrase "
        f"naming the trap), statement "
        f"(one when-X-then-Y sentence with the why), exactly one engine — "
        f"delivery=agent_hook with matcher {{event: bash|edit|output|read, …_rx}} or "
        f"ordering, or delivery=anchor_recall with 1-8 concrete identifiers — plus "
        f"source=\"session_draft\", source_ref=\"{moment.get('source_ref') or session}\", "
        f"scope_repos={json.dumps(scope)}, state={stamp}.{narrow} Never pass "
        f"activate; it lands proposed for a person. Never put a person's name, home "
        f"directory or e-mail in a rule. Ask the user first if unsure; if there is no "
        f"lesson, say nothing about this."
    )


def _moment_key(moment: dict) -> str:
    return str(moment.get("source_ref") or f"turn-{moment.get('turn')}")


def cmd_prompt(payload: dict) -> int:
    """UserPromptSubmit, within milliseconds: the newest fresh un-handed moment
    becomes one line of context, and is marked handed whether or not the agent
    files anything.

    This lane only APPENDS (a `handed` row). A detached extract child appends
    moments to the same file at any time, and a read-then-replace here would
    delete a moment appended in between."""
    session = str(payload.get("session_id") or "").strip()
    prompt = str(payload.get("prompt") or "")
    if not session or _is_subagent(payload) or hx.is_harness_text(prompt.strip()):
        return 0
    path = moments_path(session)
    if not path.is_file():
        return 0
    rows = hx.read_jsonl(path)
    handed = {str(r["handed"]) for r in rows if r.get("handed")}
    moments = [r for r in rows if not r.get("handed") and isinstance(r.get("turn"), int)]
    if not moments or len(handed) >= NUDGE_CAP_PER_SESSION:
        return 0
    meta = load_meta(session)
    last_turn = max(int(meta.get("last_turn") or 0), max(m["turn"] for m in moments))
    fresh = [m for m in moments if _moment_key(m) not in handed
             and last_turn - m["turn"] < NUDGE_MAX_AGE_TURNS]
    if not fresh:
        return 0
    # children append in the order their classifiers answered, not turn order
    fresh.sort(key=lambda m: m["turn"])
    chosen = fresh[-NUDGES_PER_PROMPT:]
    now = time.time()
    for m in chosen:
        hx.append_jsonl(path, {"handed": _moment_key(m), "at": now})
    lines = [nudge_line(session, m, meta.get("repo") or "") for m in chosen]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "\n".join(lines)}}))
    _log(f"prompt {session[:8]}: handed turn(s) {[m.get('turn') for m in chosen]}")
    return 0


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mode", choices=("stop", "prompt", "extract"))
    p.add_argument("--session", default="")
    p.add_argument("--transcript", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--arcs", default="")
    p.add_argument("--upto", type=int, default=-1)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not hx.extract_enabled():
        if args.mode in ("stop", "prompt"):
            try:
                sys.stdin.read()          # drain the hook payload, say nothing
            except Exception:
                pass
        return 0
    if args.mode == "stop":
        return cmd_stop(_read_payload())
    if args.mode == "prompt":
        return cmd_prompt(_read_payload())
    if args.mode == "extract" and args.session:
        return cmd_extract(args.session, args.transcript, args.cwd, args.arcs, args.upto)
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
