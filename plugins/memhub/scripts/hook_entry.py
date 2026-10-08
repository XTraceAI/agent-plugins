#!/usr/bin/env python3
"""The one command every Claude Code hook in ``hooks/claude-hooks.json`` runs.

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_entry.py" <event> <name>

The Claude plugin directory accepts a hook command only when each path in it
is written in full from ``${CLAUDE_PLUGIN_ROOT}``, with no other variable,
command substitution, wildcard or inline program. Each handler used to be a
small shell program (``IN=$(cat)``, ``case`` globs, the Cursor guard, a
prefilter, then the script); this file is that program, once, in stdlib
Python 3.9, keyed by ``(event, name)`` in ``ROUTES``.

For every route it keeps the shell's exact semantics:

* stdin is read once and handed on with trailing newlines removed, as
  ``IN=$(cat)`` then ``printf %s "$IN"`` did;
* a payload glob is a shell ``case`` pattern (quotes in the old
  ``*"gh pr"*`` were quoting); it is matched against the field the
  handler's own gate reads, never the whole payload (see "Payload globs");
* the harness lanes run by default, and stop when ``MEMHUB_HARNESS_EXTRACT``,
  trimmed of ASCII whitespace only, is set to anything but blank or
  ``1``/``on``/``true``/``yes`` in any case, and stop
  whatever it says when the ``harnessDrafting`` userConfig option
  (``CLAUDE_PLUGIN_OPTION_HARNESSDRAFTING``) is ``0``/``off``/``false``/``no``;
* a Rulebook lane the plugin's mod has claimed (``mod_lanes.py``: this
  install's ``MEMHUB_MOD_LANES_STAGING`` / ``_PROD`` lists it under a live
  lease, stamped with this process's ``CLAUDE_PID`` and the payload's own
  ``session_id``) exits 0 with no output before its guard or script runs — the mod serves that lane
  in-process. Only the four routes with a ``claim`` can be skipped this way;
  the fire flush, capture and harness never are;
* an empty ``CLAUDE_PLUGIN_ROOT`` skips the handler (the flush lane says so
  on stderr);
* ``claude_hook_guard`` gates every lane, then an optional prefilter must
  exit 0, then the script runs with this interpreter and writes to this
  process's stdout and stderr, so a PreToolUse block's exit code and JSON
  reach Claude unchanged;
* the exit status is the last lane's, or 0 when it was skipped — SessionEnd
  runs its two lanes in order and the first one's failure never stops the
  second.

Scripts are found next to this file, never through the environment.

In-process or isolated
----------------------
A lane runs its script IN THIS PROCESS (``runpy.run_path`` as ``__main__``,
``sys.argv`` set to the script and its arguments, ``sys.stdin`` a text stream
over the payload bytes with the encoding, errors and newline mode this
interpreter gives its own stdin) unless the lane is ``isolated``, in which
case it runs as ``sys.executable <script>`` exactly as every lane used to.
Starting a second interpreter cost every hook 20-40 ms; the synchronous hooks
are the ones Claude waits on, on every tool call. An in-process script's exit
status is what the interpreter would have exited with: ``SystemExit(None)``
is 0, an int is that int (masked as the OS masks it), anything else is
printed to stderr and is 1; an uncaught exception prints the traceback from
the script's own frame and is 1. ``sys.argv``, ``sys.stdin`` and ``sys.path``
are put back afterwards, and each run gets fresh globals.

What one in-process run can leave behind is ``sys.modules`` (and anything a
module caches). So at most ONE script per hook runs in-process — a prefilter
whose handler is isolated counts as that one — which a test pins. Prefilters
always run in-process: both are small, stdlib-only and decide from the
payload. A lane is isolated when its hook is ``async`` in claude-hooks.json:
Claude does not wait for those, so the second interpreter costs no latency,
and they are the long-running network lanes (the flushes, the brief refresh,
the fire flush) where a fresh process is the cheapest isolation there is.
SessionEnd's two lanes are both isolated, so its first lane's state cannot
reach its second.

Payload globs
-------------
A glob only decides whether a handler STARTS; the handler's own gate decides
what it does. Each glob route matches the text that gate reads, and the globs
are a superset of the gate on that text (pinned by a test against the real
gates):

* ``flush_session``: ``tool_input.command`` (``flush_prefilter`` reads only
  that), globs ``*git*commit*`` / ``*gh*pr*``;
* ``pr_babysit_trigger``: ``tool_input.command`` (``is_pr_create``);
* ``pr_link_trigger``: the tool name and ``tool_input.command`` (or
  ``.cmd``; ``pr_link.touches_github`` reads only those), each also case-folded and
  with shell quotes and backslashes removed, because pr_link matches API URLs
  case-insensitively and on shell words.

They used to match the whole raw payload, tool output included, so a ``cat``
or a Read whose output mentioned GitHub or ``git commit`` started a handler
that then declined.

One difference from the shell is accepted, not fixable here: the empty-root
skip above only runs once this file has been found. If the host leaves
``CLAUDE_PLUGIN_ROOT`` unset, the command is ``python3 "/scripts/hook_entry.py"``
and Python itself exits 2 ("can't open file"), which PreToolUse,
UserPromptSubmit and the synchronous Stop lane read as a block. The directory
forbids the shell fallback that used to absorb this. Claude Code always sets
the variable for plugin hooks; only a host that imports ``claude-hooks.json``
without it is affected.
"""
from __future__ import annotations

import fnmatch
import io
import json
import locale
import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent

# The bytes the old shell trim removed: space, \t \n \v \f \r and \x1c-\x1f.
# Exactly what str.strip() drops from ASCII; non-ASCII padding reads as off.
_FLAG_WS = " \t\n\v\f\r\x1c\x1d\x1e\x1f"
_FLAG_ON = {"1", "on", "true", "yes"}
_FLAG_OFF = {"0", "off", "false", "no"}


def _payload(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _command(payload: object) -> str:
    """``tool_input.command`` as ``flush_prefilter`` reads it ('' if none)."""
    try:
        command = (payload.get("tool_input") or {}).get("command", "")  # type: ignore[union-attr]
    except Exception:
        return ""
    return command if isinstance(command, str) else str(command)


def _command_text(payload: object) -> Tuple[str, ...]:
    return (_command(payload),)


def _github_text(payload: object) -> Tuple[str, ...]:
    name = payload.get("tool_name") if isinstance(payload, dict) else None
    texts = (name if isinstance(name, str) else "", _command(payload))
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if isinstance(tool_input, dict) and "cmd" in tool_input:   # pr_link reads it too
        texts += (str(tool_input["cmd"]),)
    # pr_link reads shell WORDS (quotes and escapes gone) and matches API URLs
    # case-insensitively; the glob must see what it sees.
    bare = tuple(t.replace('"', "").replace("'", "").replace("\\", "") for t in texts)
    return texts + tuple(t.lower() for t in texts + bare)


class Lane(NamedTuple):
    guard: str                      # claude_hook_guard action: ignore | capture
    run: Tuple[str, ...]            # script, then its arguments
    prefilter: Optional[str] = None  # must exit 0 before ``run`` starts
    isolated: bool = False          # run ``run`` in its own interpreter


class Route(NamedTuple):
    lanes: Tuple[Lane, ...]
    globs: Tuple[str, ...] = ()     # any one must match one of ``subject``'s texts
    subject: Optional[Callable[[object], Tuple[str, ...]]] = None
    harness: bool = False           # MEMHUB_HARNESS_EXTRACT can turn it off
    loud_root: bool = False         # report an unset CLAUDE_PLUGIN_ROOT
    claim: Optional[str] = None     # mod lane that, when claimed, skips this route


def _one(guard: str, *run: str, prefilter: Optional[str] = None,
         isolated: bool = False, **route: object) -> Route:
    return Route(lanes=(Lane(guard, tuple(run), prefilter, isolated),), **route)


# Every lane not marked ``isolated`` runs in-process (see the docstring); the
# isolated ones are exactly the hooks claude-hooks.json marks ``async``.
ROUTES = {
    ("PreToolUse", "rulebook_hook"): _one(
        "ignore", "rulebook_hook.py", "pre", claim="pre"),
    ("PreToolUse", "add_memory_gate"): _one("ignore", "add_memory_gate.py"),
    ("PreToolUse", "create_rule_origin"): _one("ignore", "create_rule_origin.py"),

    # Stage 1 is a cheap test of tool_input.command; flush_prefilter.py is the
    # precise stage 2 on that same field. `*gh*pr*`, not `*gh pr*`: stage 2
    # takes any whitespace between them, and stage 1 must not be narrower.
    ("PostToolUse", "flush_session"): _one(
        "ignore", "flush_session.py", prefilter="flush_prefilter.py",
        isolated=True, globs=("*git*commit*", "*gh*pr*"),
        subject=_command_text, loud_root=True),
    ("PostToolUse", "artifact_sync_reminder"): _one(
        "ignore", "artifact_sync_reminder.py"),
    ("PostToolUse", "pr_babysit_trigger"): _one(
        "ignore", "pr_babysit_trigger.py", globs=("*gh*pr*create*",),
        subject=_command_text),
    ("PostToolUse", "md_capture"): _one("ignore", "md_capture.py"),
    ("PostToolUse", "rulebook_hook"): _one(
        "ignore", "rulebook_hook.py", "post", claim="post"),
    # All four are needed: see docs/specs/pr-linking-plugin-spec.md §5.1.
    ("PostToolUse", "pr_link_trigger"): _one(
        "ignore", "pr_link_trigger.py",
        globs=("*gh*pr*", "*[Gg]it[Hh]ub*", "*api/v3*", "*repos/*pulls*"),
        subject=_github_text),

    ("SessionStart", "capture_health"): _one("ignore", "capture_health.py"),
    ("SessionStart", "brain_brief"): _one("ignore", "brain_brief.py", "brief"),
    ("SessionStart", "rulebook_hook"): _one(
        "ignore", "rulebook_hook.py", "session", claim="session"),
    ("SessionStart", "harness_stop"): _one(
        "ignore", "harness_stop.py", "session", harness=True),

    ("UserPromptSubmit", "brain_brief"): _one("ignore", "brain_brief.py", "prompt"),
    ("UserPromptSubmit", "rulebook_hook"): _one(
        "ignore", "rulebook_hook.py", "prompt", claim="prompt"),

    ("Stop", "flush_turn"): _one(
        "capture", "flush_turn.py", prefilter="turn_flush_prefilter.py",
        isolated=True),
    ("Stop", "brain_brief"): _one(
        "ignore", "brain_brief.py", "refresh", isolated=True),
    # md_capture_prefilter.py skips a Stop when nothing is dirty and the last
    # git sweep came back idle with no watched tool call since.
    ("Stop", "md_capture_flush"): _one(
        "ignore", "md_capture_flush.py", prefilter="md_capture_prefilter.py",
        isolated=True),
    ("Stop", "rulebook_hook"): _one(
        "ignore", "rulebook_hook.py", "flush", isolated=True),
    ("Stop", "harness_stop"): _one("ignore", "harness_stop.py", "stop", harness=True),

    # One handler, two lanes, in order: Claude Code runs an event's handlers
    # in parallel, and the fire flush can only link rows capture has stored.
    ("SessionEnd", "session_end"): Route(lanes=(
        Lane("capture", ("flush_session.py",), isolated=True),
        Lane("ignore", ("rulebook_hook.py", "flush", "final"), isolated=True),
    )),
}


def harness_enabled(environ=None) -> bool:
    value = (os.environ if environ is None else environ).get(
        "MEMHUB_HARNESS_EXTRACT", "").strip(_FLAG_WS)
    opt = (os.environ if environ is None else environ).get(
        "CLAUDE_PLUGIN_OPTION_HARNESSDRAFTING", "").strip(_FLAG_WS)
    if opt.isascii() and opt.lower() in _FLAG_OFF:
        return False   # the `harnessDrafting` userConfig option, set false
    # On by default: unset or blank is on; so is an on spelling. An off
    # spelling or anything unrecognised is off (harness_extract.extract_enabled).
    return value == "" or (value.isascii() and value.lower() in _FLAG_ON)


def payload_matches(texts: Tuple[str, ...], globs: Tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(text, glob) for glob in globs for text in texts)


def _guard(action: str, event: str, raw: bytes, payload: object) -> bool:
    """claude_hook_guard's verdict; any failure skips, as its exit 1 did."""
    try:
        import claude_hook_guard
        return bool(claude_hook_guard.route(action, event, payload, raw))
    except Exception:
        return False


def _claimed(lane: Optional[str], payload: object, environ) -> bool:
    """mod_lanes' verdict for this install; any failure serves the lane."""
    if not lane:
        return False
    try:
        import mod_lanes
        return bool(mod_lanes.claimed(lane, payload, environ, root=str(SCRIPTS.parent)))
    except Exception:
        return False


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _stdin_over(raw: bytes) -> io.TextIOWrapper:
    """``raw`` as the text stdin a child interpreter would have been given."""
    real = sys.__stdin__
    return io.TextIOWrapper(
        io.BytesIO(raw),
        encoding=getattr(real, "encoding", None) or locale.getpreferredencoding(False),
        errors=getattr(real, "errors", None) or "strict",
        # CPython's own stdin: no newline translation, except on Windows.
        newline=None if os.name == "nt" else "\n")


def _exit_status(code: object) -> int:
    """The status the interpreter exits with for ``SystemExit(code)``."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code & (0xFFFFFFFF if os.name == "nt" else 0xFF)
    try:
        print(code, file=sys.stderr)
    except Exception:
        pass
    return 1


def _exec(command: Tuple[str, ...], raw: bytes) -> int:
    """Run one script in this process, as ``_spawn`` would have run it."""
    script = str(SCRIPTS / command[0])
    saved = (sys.argv, sys.stdin, sys.path[:])
    _flush()
    sys.argv = [script, *command[1:]]
    sys.stdin = _stdin_over(raw)
    try:
        runpy.run_path(script, run_name="__main__")
        status = 0
    except SystemExit as exc:
        status = _exit_status(exc.code)
    except BaseException as exc:  # noqa: BLE001 — the interpreter's own fallback
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != script:
            tb = tb.tb_next
        try:
            traceback.print_exception(type(exc), exc, tb)
        except Exception:
            pass
        # An uncaught Ctrl-C kills the interpreter by SIGINT: 128 + 2.
        status = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        _flush()
        sys.argv, sys.stdin = saved[0], saved[1]
        sys.path[:] = saved[2]
    return status


def _run(command: Tuple[str, ...], raw: bytes, isolated: bool = False) -> int:
    return _spawn(command, raw) if isolated else _exec(command, raw)


def _spawn(command: Tuple[str, ...], raw: bytes) -> int:
    _flush()
    try:
        code = subprocess.run(
            [sys.executable, str(SCRIPTS / command[0]), *command[1:]],
            input=raw).returncode
    except OSError as exc:
        print(f"[memhub-hook] could not start {command[0]}: {exc}",
              file=sys.stderr)
        return 1
    return 128 - code if code < 0 else code  # a signal, as the shell reports it


def dispatch(event: str, name: str, raw: bytes, environ=None) -> int:
    env = os.environ if environ is None else environ
    route = ROUTES[(event, name)]
    if route.harness and not harness_enabled(env):
        return 0
    raw = raw.rstrip(b"\n")
    payload = _payload(raw)
    if _claimed(route.claim, payload, env):
        return 0
    if route.globs and not payload_matches(
            route.subject(payload) if route.subject else (), route.globs):
        return 0
    if not env.get("CLAUDE_PLUGIN_ROOT"):
        if route.loud_root:
            print("[memhub-flush] CLAUDE_PLUGIN_ROOT unset; cannot locate "
                  "flush scripts, skipping", file=sys.stderr)
        return 0
    status = 0
    for lane in route.lanes:
        status = 0
        if not _guard(lane.guard, event, raw, payload):
            continue
        if lane.prefilter and _run((lane.prefilter,), raw) != 0:
            continue
        status = _run(lane.run, raw, lane.isolated)
    return status


# Routes a released claude-hooks.json named and this checkout no longer serves.
# Claude Code reads the hooks config once, at session start, while the scripts
# run live from the install, so a session started on an older release keeps
# calling these until it restarts. They exit 0 with no output: the person sees
# nothing, and the model is told nothing.
#   ("UserPromptSubmit", "harness_stop"): 0.118.0-0.120.3 told the model the
#   harness hand-off rule at its first prompt; SessionStart tells it again.
RETIRED = frozenset({("UserPromptSubmit", "harness_stop")})


def main(argv=None) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if args in RETIRED:
        return 0
    if args not in ROUTES:
        # Exit 0 like a retired route: Claude Code shows a non-zero hook's
        # stderr to the person as a hook error. The line reaches the debug log.
        print(f"[memhub-hook] unknown hook {' '.join(args)!r}", file=sys.stderr)
        return 0
    return dispatch(args[0], args[1], sys.stdin.buffer.read())


if __name__ == "__main__":
    raise SystemExit(main())
