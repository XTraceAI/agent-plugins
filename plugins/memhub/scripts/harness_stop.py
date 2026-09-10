#!/usr/bin/env python3
"""The Stop sensor for harness-tied memory (spec §4.1–§4.2) — FLAGGED OFF.

Nothing in this file runs unless `MEMHUB_HARNESS_EXTRACT` is on. With it on,
the server classifies each turn's moment and the LIVE AGENT mines the lesson:

  Stop(turn N)         `stop`     returns in milliseconds; a detached `extract`
                                  child builds the redacted window, asks the
                                  server classifier, and — on a signal — writes
                                  the MOMENT (turn, kind, router hint, the
                                  nine-field stamp) to <session>.moments.jsonl
  next user prompt     `prompt`   hands the newest un-handed moment to the agent
                                  in ONE injected line: which turn, what kind,
                                  the stamp to pass — and the agent that lived
                                  the turn decides whether there is a lesson,
                                  asks the person if unsure, and files it with
                                  the memhub `create_rule` tool. It lands
                                  `proposed`; a human activates in Studio.

That is the whole loop. There is no post-session miner, no idle waiter and no
sync: a moment nobody handed to the agent (the terminal closed, a host with no
prompt lane) is left in its file, and the row the agent files goes through the
same `create_rule` any authored rule does — the server stamps, twin-checks and
refuses there. Nothing here fires a rule and nothing here activates one.

Files, under $MEMHUB_HARNESS_DRAFTS (default ~/.config/memhub-plugin/drafts):

  <session>.moments.jsonl   classifier-flagged moments; `handed_at` once nudged
  <session>.meta.json       last extracted turn, repo, cwd, nudge count
  <session>.jsonl           what the server's author drafted (kept for the
                            replay's measurements; the live path reads only
                            moments — see `judge_only` in the scorecard)

Every path fails open and silent (§6): a broken sensor must never touch the
tool call or the session. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness_extract as hx  # noqa: E402

NUDGE_MAX_AGE_TURNS = 3      # a moment older than this is left in its file, never nudged stale
NUDGE_CAP_PER_SESSION = 8    # the same bound §4.3 puts on drafts
NUDGES_PER_PROMPT = 1        # one line, the newest moment


# --------------------------------------------------------------- plumbing
def _log(msg: str) -> None:
    try:
        path = hx.log_path("stop.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _base() -> Path:
    return hx.drafts_path("x").parent


def _safe(session: str) -> str:
    return hx.drafts_path(session).stem


def meta_path(session: str) -> Path:
    return _base() / f"{_safe(session)}.meta.json"


def moments_path(session: str) -> Path:
    """Classifier-flagged moments (stamp + redacted window), appended by the
    extract child, handed to the agent by the prompt lane."""
    return _base() / f"{_safe(session)}.moments.jsonl"


def load_meta(session: str) -> dict:
    try:
        got = json.loads(meta_path(session).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _publish(path: Path, text: str) -> None:
    """Atomic, 0600 — `atomic_write.publish` beside this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import atomic_write  # noqa: PLC0415
        atomic_write.publish(path, text)
    except Exception:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def save_meta(session: str, **fields) -> dict:
    meta = load_meta(session)
    meta.update(fields)
    meta["session_id"] = session
    _publish(meta_path(session), json.dumps(meta, indent=1, default=str))
    return meta


def write_rows(path: Path, rows: list[dict]) -> None:
    _publish(path, "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                           for r in rows))


def _spawn(mode: str, *args: str) -> int:
    return hx.spawn_detached([mode, *args], script=Path(__file__).resolve(),
                             log_name="stop.log")


def env_name() -> str:
    """Which MemHub the stamp's `env` names — derived from the plugin's own
    backend URL, never configured separately (the prod/staging split lives in
    the install, and a second setting is a second thing to point wrong)."""
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


def _args(**over) -> argparse.Namespace:
    """The replay's argument shape, for `extract_turn`."""
    base = {"hook_version": "", "env": env_name(), "budget": hx.DEFAULT_BUDGET,
            "no_model": False, "draft_timeout": 0, "out": "", "quiet": True,
            "trace": "", "stats": "", "turn": None, "moments": "", "pace": 0}
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------- stop lane
def cmd_stop(payload: dict) -> int:
    """Millisecond budget: one Popen, no reading of the transcript, no network."""
    session = str(payload.get("session_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not session or not transcript or not os.path.isfile(transcript):
        return 0
    if payload.get("stop_hook_active"):
        # The host re-entered Stop because a Stop hook asked it to continue.
        # The turn is the same turn; the first firing already spawned for it.
        return 0
    if str(payload.get("agent_id") or "").strip():
        # A subagent's Stop. Its turn is not the person's turn, and a moment
        # from it would be nudged into the main agent's prompt.
        return 0
    _spawn("extract", "--session", session, "--transcript", transcript, "--cwd", cwd)
    return 0


def cmd_extract(session: str, transcript: str, cwd: str) -> int:
    """The child. The last turn in the transcript through the same path the
    replay measures; the classifier's verdict lands as a moment."""
    try:
        turns = hx.turns_from_transcript(transcript)
    except (OSError, ValueError) as exc:
        _log(f"extract {session[:8]}: cannot read transcript: {exc!r}")
        return 0
    if not turns:
        return 0
    last, prev = turns[-1], (turns[-2] if len(turns) > 1 else None)
    meta = load_meta(session)
    marker = f"{last.get('n')}:{last.get('uuid', '')}"
    if meta.get("last_extracted") == marker:
        return 0                          # Stop fired twice for one turn
    rh = hx._hook()
    arcs = rh.take_error_arcs(session) if rh is not None \
        and hasattr(rh, "take_error_arcs") else []
    repo = repo_of(cwd or last.get("cwd") or "")
    doc = {"session": session, "cwd": cwd or last.get("cwd") or "",
           "repo": repo, "source": "claude-live", "turns": turns}
    out_path = hx.drafts_path(session)
    kept = hx.read_drafts(out_path)
    stats = hx.new_stats(doc)
    trace = hx.Trace(str(hx.log_path("extract.log")), quiet=True)
    try:
        hx.extract_turn(last, prev, doc=doc, args=_args(moments=str(moments_path(session))),
                        kept=kept, stats=stats, trace=trace, out_path=out_path, arcs=arcs)
    finally:
        trace.close()
    save_meta(session, repo=repo, cwd=doc["cwd"], transcript_path=transcript,
              last_extracted=marker, last_turn=last.get("n"), last_stop_at=time.time())
    _log(f"extract {session[:8]} t{last.get('n')}: sent={stats['turns_sent']} "
         f"moment={stats.get('moments', 0)} refusals={stats['refusals']} arcs={len(arcs)}")
    return 0


# ------------------------------------------------------------- prompt lane
def nudge_line(session: str, moment: dict, repo: str) -> str:
    """The one line the agent reads. It names the turn and the kind, and
    carries the stamp verbatim so the row the agent files is a
    `session_draft` like any other — stamped by the harness, never typed."""
    kind = moment.get("kind") or "a signal"
    hint = f" (router: {moment['hint']})" if moment.get("hint") else ""
    stamp = json.dumps(moment.get("state") or {}, ensure_ascii=False, default=str)
    return (
        f"MemHub harness: your previous turn (turn {moment.get('turn')}) was classified as "
        f"{kind}{hint}. If it carries a lesson that would change what an agent DOES next "
        f"time, is not already in the repo, its docs, CLAUDE.md or the rulebook, is not "
        f"project state, and will still be true next month, propose it now with the memhub "
        f"create_rule tool: title (a short noun phrase naming the trap), statement (one "
        f"when-X-then-Y sentence with the why), exactly one engine — delivery=agent_hook "
        f"with matcher {{event: bash|edit|output|read, …_rx}} or ordering, or "
        f"delivery=anchor_recall with 1-8 concrete identifiers — plus "
        f"source=\"session_draft\", source_ref=\"{moment.get('source_ref') or session}\", "
        f"scope_repos={json.dumps([repo] if repo else [])}, state={stamp}. Never pass "
        f"activate; it lands proposed for a human. Never put a person's name, home "
        f"directory or e-mail in a row. Ask the user first if unsure; if there is no "
        f"lesson, say nothing about this."
    )


def cmd_prompt(payload: dict) -> int:
    """UserPromptSubmit, ≤10 ms: the newest un-handed moment becomes one line
    of context. Marks it handed whether or not the agent files anything —
    the agent had its chance, and a second nudge for one moment is noise."""
    session = str(payload.get("session_id") or "").strip()
    prompt = str(payload.get("prompt") or "")
    if not session or hx.is_harness_text(prompt.strip()):
        return 0
    path = moments_path(session)
    if not path.is_file():
        return 0
    moments = hx.read_drafts(path)
    if not moments:
        return 0
    meta = load_meta(session)
    if int(meta.get("nudges") or 0) >= NUDGE_CAP_PER_SESSION:
        return 0
    last_turn = int(meta.get("last_turn") or moments[-1].get("turn") or 0)
    fresh = [m for m in moments if not m.get("handed_at")
             and isinstance(m.get("turn"), int)
             and last_turn - m["turn"] < NUDGE_MAX_AGE_TURNS]
    if not fresh:
        return 0
    chosen = fresh[-NUDGES_PER_PROMPT:]
    now = time.time()
    for m in chosen:
        m["handed_at"] = now
    write_rows(path, moments)
    save_meta(session, nudges=int(meta.get("nudges") or 0) + len(chosen))
    lines = [nudge_line(session, m, meta.get("repo") or (m.get("state") or {}).get("repo") or "")
             for m in chosen]
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
        return cmd_extract(args.session, args.transcript, args.cwd)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:                 # noqa: BLE001 — §6: silent, exit 0
        if os.environ.get("MEMHUB_HARNESS_DEBUG"):
            import traceback
            traceback.print_exc()
        rc = 0
    sys.exit(rc or 0)
