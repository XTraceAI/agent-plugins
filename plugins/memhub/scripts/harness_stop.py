#!/usr/bin/env python3
"""The harness-tied memory sensor. FLAGGED OFF by default.

Nothing in this file runs unless `MEMHUB_HARNESS_EXTRACT` is on (1/on/true/yes).
With it on, every Stop judges the PREVIOUS human turn, synchronously, and on a
signal asks the agent to launch one background fork that files the rule:

  Stop(turn N+1)  `stop`   rebuilds turn N from the transcript the hook payload
                           names, asks the classifier (harness_extract), and on
                           a signal continues the agent ONCE with one short
                           line, written for the person, naming the `handoff`
                           command.
  SessionStart    `session` gives the agent the standing rule, unseen by the
                           person: the `handoff` command to run for that line,
                           and to say nothing unless a rule is filed.
  agent           `handoff` prints the instruction to launch a fork with
                           `fork_launch_prompt`, for the moment the line named.
  fork            `stamp`  the fork runs this read-only command for the state
                           stamp `create_rule` requires, then files through the
                           create-rule skill's Harness-draft path.

Why one turn late (harness-tied-memory-spec §4.3.1): the classifier's window
ends with the agent's response to the person's message; it cannot hold the
person's NEXT message, which is what says whether a correction held. Judged at
the Stop of N+1, the fork launched there already has it in context. Measured
2026-09-29 on 148 flags: the next message reversed 2% and refined 8%. The cost
is the session's last turn, which no Stop follows — about 7% of flags.

Nothing is stored. The transcript the host keeps is the only state: no moments
file, no cursor, no claims, no launch ledger. A Stop that is not the end of a
person's turn (a fork launch's continuation, a background task's notice, a loop
wakeup) judges nothing, so no turn is judged twice.

What the person sees: one short line per flagged turn ("MemHub: possible team
rule spotted in turn N, drafting it in the background."), the collapsed
`handoff` and fork tool calls, and
the fork's `filed <title>` line when a rule was filed. Claude Code prints
whatever a Stop hook uses to continue the agent — a `decision: block` reason
under "Stop hook error", `additionalContext` as "Stop hook feedback" — so the
hook says as little as it can and the fork prompt travels in a command's
output. A `none` or `failed` result stays in the transcript and `stop.log`.

Nothing here fires or activates a rule: a filing lands `proposed` and a person
activates it. Every path fails open and silent — a broken sensor must never
touch the session. `stop.log` under $MEMHUB_HARNESS_DIR (default
~/.config/memhub-plugin/harness) is the one file, one line per Stop, never
prompt text. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness_extract as hx  # noqa: E402

#: What the fork's prompt and its last line begin with; the transcript reader
#: skips a turn that is only this fork's completion report.
FORK_MARK = "MemHub harness fork"
#: What `handoff` prints for the continued agent. Never in the Stop hook's own
#: output, which the person sees.
BLOCK_PREFIX = "MemHub harness: before you stop"
#: What the Stop hook's one line begins with — the only harness text shown,
#: and written for the person. `session_rule` names it, so the two move together.
HANDOFF_PREFIX = "MemHub: possible team rule spotted in turn"


def session_rule(session: str) -> str:
    """The standing rule the agent gets at SessionStart, a channel Claude Code
    does not show the person. It carries everything the Stop's line leaves out:
    the command to run for a flagged turn, and the instruction to keep quiet."""
    script = str(Path(__file__).resolve())
    return (
        f"MemHub harness: when a Stop hook line says \"{HANDOFF_PREFIX} N\", run "
        f"`python3 \"{script}\" handoff --moment {session}#N` with that N and follow its "
        f"output. That line has already told the person, so say nothing more about it, and "
        f"nothing about a fork that files no rule; if a fork files a rule, tell the person "
        f"that one line."
    )


_MOMENT_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}#\d{1,6}$")
_KIND = re.compile(r"^[a-z_]{1,40}$")


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


def env_name() -> str:
    """Which MemHub the stamp's `env` names, derived from the plugin's own
    backend URL rather than configured a second time."""
    try:
        from _memhub_auth import default_url  # noqa: PLC0415
        host = default_url()
    except Exception:
        return "unknown"
    return "staging" if "staging" in host else "production"


def rule_url(rule_id: str, env: str) -> str:
    """The Studio page that opens this rule (`/studio/rulebook?open=<id>`), or ""
    when the plugin's backend is not the one the rule was filed against — a
    link into the other environment would open a rule that is not there."""
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415
        from _memhub_auth import default_url  # noqa: PLC0415
        # The API -> web app pairing login already keys its guide link on.
        from plugin_onboarding import _ORIGINS  # noqa: PLC0415
        parsed = urlsplit(default_url())
    except Exception:
        return ""
    origin = _ORIGINS.get(f"{parsed.scheme}://{parsed.netloc}")
    if not origin or not rule_id or env_name() != env:
        return ""
    return f"{origin}/studio/rulebook?open={rule_id}"


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
    the person's — the harness fork's own Stop is one of these."""
    return bool(str(payload.get("agent_id") or "").strip())


def turn_repos(state: dict, fallback_repo: str = "") -> list[str]:
    """The repositories the turn's actions worked in, else the stamp's repo,
    else the session's.

    Evidence for the author, NOT the rule's scope. Handed over as `scope_repos`
    verbatim, it scoped lessons to wherever the session happened to be rather
    than to what they are about: of 17 rules filed by 2026-09-23, five were
    mis-scoped that way. Only the author has read the lesson, so the author
    chooses (create-rule SKILL.md, Harness-draft)."""
    touched = [r for r in (state.get("touched_repos") or []) if isinstance(r, str) and r]
    if touched:
        return touched
    repo = state.get("repo") or fallback_repo
    return [repo] if repo else []


def _stamp(session: str, turn: dict, cwd: str) -> dict:
    return hx.stamp_state(session=session, turn=turn, cwd=turn.get("cwd") or cwd,
                          hook_version=hx.plugin_version(), env_name=env_name(),
                          default_repo=repo_of(cwd))


# ------------------------------------------------------------------- judge
def judge_previous_turn(session: str, transcript: str, cwd: str) -> dict | None:
    """Turn N at the Stop of turn N+1: the moment on a signal, else None.

    Judges only when this Stop ends a PERSON'S turn, i.e. the transcript's last
    prompt is the last person turn. After a fork launch's continuation, a
    background task's notice or a loop wakeup, the last prompt is the
    harness's, and judging would take turn N a second time. An interrupted
    turn resent verbatim is judged once, as the resend: this Stop skips turn N
    when turn N+1 repeats it, and the next Stop takes N+1."""
    turns, last_prompt = hx.read_transcript(transcript)
    if len(turns) < 2 or turns[-1].get("uuid") != last_prompt:
        return None
    turn, after = turns[-2], turns[-1]
    n = turn.get("n")
    if (after.get("user") or "").strip() == (turn.get("user") or "").strip():
        _log(f"judge {session[:8]} t{n}: skipped, resent as t{after.get('n')}")
        return None
    prev = turns[-3] if len(turns) >= 3 else None
    state = _stamp(session, turn, cwd)
    window = hx.redact_window(hx.build_window(turn, prev, state, earlier=turns[-5:-3],
                                              following=after.get("user") or ""))
    reply, secs = hx.server_classify(window, repo=state.get("repo", ""))
    _log(f"judge {session[:8]} t{n}: signal={bool(reply.get('signal'))} "
         f"reason={reply.get('reason')} kind={reply.get('kind')} {secs}s "
         f"window={len(window)}c results={window.count(chr(10) + '    -> ')} "
         f"next={'yes' if (after.get('user') or '').strip() else 'no'}")
    if not reply.get("signal"):
        return None
    return {"source_ref": f"{session}#{n}", "turn": n, "kind": reply.get("kind"),
            "derivable": bool(reply.get("derivable")), "state": state,
            "transcript": transcript, "cwd": cwd}


# ------------------------------------------------------------------ launch
def fork_launch_prompt(moment: dict, env: str, repo: str = "") -> str:
    """The Agent tool input the blocked agent hands its fork.

    The auto-mode permission classifier judges the person's messages, CLAUDE.md
    and the TOOL INPUTS the agent sends — never a hook's output. A bare "read
    <file> and follow it" was, to it, an order to obey an unseen file
    ('Auto-Mode Bypass'; probed 2026-09-28). So this input says who launched
    the fork, on whose install, for which turn, what it may do and what it must
    not, in full. How to file lives in the create-rule skill and is not
    restated: the line that restated it drew five of six Codex findings on
    #222. The stamp is not inlined either — the fork prints it with the
    read-only `stamp` command, so no JSON reaches the person's screen."""
    turn = moment["turn"]
    ref = moment["source_ref"]
    kind = moment.get("kind") or "a signal"
    derivable = (" The classifier thinks it may already be written down, so check that "
                 "first.") if moment.get("derivable") else ""
    script = str(Path(__file__).resolve())
    stamp_cmd = (f'python3 "{script}" stamp --transcript "{moment["transcript"]}" '
                 f'--turn {turn} --cwd "{moment["cwd"]}"')
    worked_in = json.dumps(turn_repos(moment.get("state") or {}, repo))
    return (
        f"{FORK_MARK}, moment {ref}: you were launched by the MemHub plugin's Stop hook, "
        f"installed by the person running this session, to write at most one team rule "
        f"from turn {turn} of this session, which its classifier flagged as {kind}."
        f"{derivable} The person's next message, turn {turn + 1}, is what happened after "
        f"it: if that message reversed or abandoned the correction, file nothing. "
        f"Otherwise run the memhub create-rule skill's Harness-draft path on "
        f"source_ref=\"{ref}\", with the state stamp this read-only command prints: "
        f"{stamp_cmd}. The turn worked in {worked_in}; choose scope_repos from the lesson, "
        f"not from that. Step 4b (the live forward test) cannot run inside a fork, so "
        f"treat its precondition as missing exactly as Step 4b.6 says. File the rule as "
        f"`proposed` to the {env} rulebook; activate nothing, ask nothing, touch no "
        f"settings or credentials. End with exactly one line: \"{FORK_MARK}: filed "
        f"<title>\", \"{FORK_MARK}: none, <one short reason>\" or \"{FORK_MARK}: failed, "
        f"<why it could not file>\"."
    )


def handoff_line(moment: dict) -> str | None:
    """The Stop hook's `additionalContext`: ONE line, written for the person and
    nothing else, because Claude Code prints a Stop hook's context to them in
    full ("Stop hook feedback", seen on 2.1.286 — the hooks reference says
    otherwise, and a headless probe cannot show it).

    What the agent does with it is `session_rule`, delivered at SessionStart
    where the person does not see it: the command, and the instruction to keep
    quiet. Measured 2026-09-30, 8 hand-offs each, forks launched 8 of 8 every
    time — the agent told the person about the launch 8 times with the rule
    only in `handoff`'s output, 8 times with the single word "Silently" here,
    and 0 times with the rule given at SessionStart.

    None when the ref is not one `handoff` would accept."""
    if not _MOMENT_REF.match(str(moment.get("source_ref") or "")):
        return None
    return f"{HANDOFF_PREFIX} {moment['turn']}, drafting it in the background."


def fork_reason(moment: dict, env: str, repo: str = "") -> str:
    """What the continued agent reads from the `handoff` command. It carries
    the launch prompt verbatim (the classifier reads THAT, in the Agent tool
    input).

    The fork's completion notice reaches this agent as a new turn the host
    starts on its own; the plugin cannot suppress it. So this also says what
    to do then: relay a `filed` result in one line, and let `none` or `failed`
    pass in silence — a rule that did not get written is not news the person
    asked for, and the full report stays in the transcript."""
    launch = fork_launch_prompt(moment, env, repo)
    return (
        f"{BLOCK_PREFIX}: turn {moment['turn']} was flagged. Launch ONE background fork "
        f"of yourself with the Agent tool (subagent_type \"fork\") and give it exactly "
        f"this prompt, verbatim: <<<{launch}>>> Say nothing to the person about it and do "
        f"nothing else this turn. When the fork's completion notice arrives: if its last "
        f"line says \"filed\", tell the person that one line; if it says \"none\" or "
        f"\"failed\", say nothing about it and end that turn with no text."
    )


# ---------------------------------------------------------------- commands
def cmd_stop(payload: dict) -> int:
    session = str(payload.get("session_id") or "")
    transcript = str(payload.get("transcript_path") or "")
    cwd = str(payload.get("cwd") or "")
    if not session or not transcript or not os.path.isfile(transcript):
        return 0
    if _is_subagent(payload) or payload.get("stop_hook_active"):
        # A subagent's Stop is not the person's; `stop_hook_active` is the
        # continuation this hook asked for, i.e. the fork launch itself.
        return 0
    moment = judge_previous_turn(session, transcript, cwd)
    if moment is None:
        return 0
    line = handoff_line(moment)
    if line is None:
        _log(f"launch {session[:8]} t{moment['turn']}: skipped, unusable moment ref")
        return 0
    # `additionalContext`, not `decision: block`: both continue the agent (and
    # both set `stop_hook_active` on the continuation, probed on 2.1.286), and
    # Claude Code prints both to the person in full — so this carries one line,
    # and the fork prompt comes from the `handoff` command it names.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "Stop", "additionalContext": line}}))
    _log(f"launch {session[:8]} t{moment['turn']}: fork requested ({moment.get('kind')}) "
         f"derivable={int(bool(moment.get('derivable')))}")
    return 0


def cmd_session(payload: dict) -> int:
    """SessionStart: hand the agent `session_rule`. Every source — startup,
    resume, clear and compact — so a compacted or resumed session gets it
    again, with its current id. A session that has lost the rule launches no
    fork: the Stop's line alone names no command, and the moment is lost."""
    session = str(payload.get("session_id") or "")
    if _is_subagent(payload) or not _MOMENT_REF.match(f"{session}#1"):
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": session_rule(session)}}))
    return 0


def _find_transcript(session: str) -> str | None:
    """The session's transcript under the host's config dir. The `handoff`
    command takes a moment ref, not a path, so the line the person sees stays
    short; the newest match wins should a session id ever repeat."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    found = sorted(root.glob(f"*/{session}.jsonl"), key=lambda p: p.stat().st_mtime)
    return str(found[-1]) if found else None


_LAUNCH_LINE = re.compile(r" launch (\S+) t(\d+): fork requested \(([a-z_]{1,40}|None)\) derivable=([01])$")


def _logged_verdict(session: str, turn: int) -> tuple[str, bool]:
    """The classifier's label for a moment, from the `launch` line the Stop
    logged for it. The Stop's visible line carries the turn and nothing else,
    and nothing else is stored; without the line the prompt says "a signal"."""
    try:
        lines = hx.log_path("stop.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", False
    for line in reversed(lines[-400:]):
        m = _LAUNCH_LINE.search(line)
        if m and m.group(1) == session[:8] and int(m.group(2)) == turn:
            return ("" if m.group(3) == "None" else m.group(3)), m.group(4) == "1"
    return "", False


def cmd_handoff(ref: str, kind: str, derivable: bool) -> int:
    """Print what the continued agent does for the moment `ref`: launch one fork
    with `fork_launch_prompt`. The agent's own call, like `stamp`: read-only,
    not gated on the flag, and loud when it cannot answer."""
    if not _MOMENT_REF.match(ref or ""):
        print(f"not a moment ref: {ref!r}", file=sys.stderr)
        return 2
    session, _, turn_s = ref.rpartition("#")
    if not kind and not derivable:
        kind, derivable = _logged_verdict(session, int(turn_s))
    transcript = _find_transcript(session)
    if transcript is None:
        print(f"no transcript for session {session}", file=sys.stderr)
        return 2
    for turn in hx.turns_from_transcript(transcript):
        if turn.get("n") == int(turn_s):
            cwd = turn.get("cwd") or os.getcwd()
            moment = {"source_ref": ref, "turn": int(turn_s), "kind": kind or None,
                      "derivable": derivable, "state": _stamp(session, turn, cwd),
                      "transcript": transcript, "cwd": cwd}
            print(fork_reason(moment, env_name(), repo_of(cwd)))
            return 0
    print(f"no turn {turn_s} in {transcript}", file=sys.stderr)
    return 2


def cmd_stamp(transcript: str, turn_n: int, cwd: str) -> int:
    """Print the state stamp for turn `turn_n` of a transcript, for the fork.
    Read-only: the transcript and git, nothing written."""
    session = Path(transcript).stem
    for turn in hx.turns_from_transcript(transcript):
        if turn.get("n") == turn_n:
            print(json.dumps(_stamp(session, turn, cwd)))
            return 0
    print(f"no turn {turn_n} in {transcript}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mode", choices=("stop", "session", "stamp", "handoff"))
    p.add_argument("--transcript", default="")
    p.add_argument("--turn", type=int, default=0)
    p.add_argument("--cwd", default="")
    p.add_argument("--moment", default="", help="handoff: <session_id>#<turn>")
    p.add_argument("--kind", default="", help="handoff: the classifier's label")
    p.add_argument("--derivable", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.mode == "stamp":
        # The fork's own call: it stands whatever the flag says now, so a
        # person who turns the sensor off mid-fork does not strand the filing.
        return cmd_stamp(args.transcript, args.turn, args.cwd)
    if args.mode == "handoff":
        # The continued agent's call, asked for by a Stop that already judged.
        kind = args.kind if _KIND.match(args.kind or "") else ""
        return cmd_handoff(args.moment, kind, args.derivable)
    if not hx.extract_enabled():
        try:
            sys.stdin.read()          # drain the hook payload, say nothing
        except Exception:
            pass
        return 0
    if args.mode == "session":
        return cmd_session(_read_payload())
    return cmd_stop(_read_payload())


if __name__ == "__main__":
    # A hook must never fail the host, so a Stop swallows everything and exits
    # 0. `stamp` and `handoff` are the fork's and the agent's tools, not hooks:
    # their errors are loud.
    loud = len(sys.argv) > 1 and sys.argv[1] in ("stamp", "handoff")
    try:
        rc = main()
    except SystemExit:
        if loud:
            raise
        rc = 0
    except BaseException:                 # noqa: BLE001 — silent, exit 0
        if loud or os.environ.get("MEMHUB_HARNESS_DEBUG"):
            import traceback
            traceback.print_exc()
        rc = 2 if loud else 0
    sys.exit(rc or 0)
