#!/usr/bin/env python3
"""Harness-tied memory, the client half: which moments of a session deserve
the coding agent's attention.

Nothing here writes a rule. At the Stop of turn N+1 — only with
`MEMHUB_HARNESS_EXTRACT=1`; off by default — `harness_stop.py` runs turn N
through this pipeline, in the hook, synchronously:

    transcript   the session's .jsonl → turns. Turn N is rebuilt from the file
                 the hook payload names; nothing is read from local state.
    window       the moment as text: the previous user message with that
                 turn's last actions and words, this user message, this turn's
                 first actions, its errors and closed error arcs, its final
                 words, and one state line. REDACTED before it leaves the
                 machine.
    classifier   ONE bounded POST to MemHub
                 `/v1/team/rulebook/harness/classify`: is this moment worth a
                 rule, and of what kind.

On a signal the Stop blocks once and the agent launches a background fork of
itself that files the rule (harness-tied-memory-spec §4.3). Turn N is judged
one turn late so the fork already holds turn N+1, the person's reaction to the
correction.

Everything is bounded and fails open: one attempt at the server, no retry, and
every failure is "no signal" with a reason. Stdlib only, like every other
script in this plugin; the server call rides the credential `/memhub:login`
already minted (`_memhub_auth` + `mcp_http`).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

FLAG = "MEMHUB_HARNESS_EXTRACT"
CHILD_FLAG = "MEMHUB_HARNESS_CHILD"   # set by harness_stop.run_author
_ON = ("1", "on", "true", "yes")

CLASSIFY_PATH = "/v1/team/rulebook/harness/classify"
# The server bounds its judge at 20 s. One attempt, and this is the whole wait:
# a moment the classifier never answered for is simply not handed over.
# The call runs inside the Stop hook (10 s budget); staging p99 is ~9 s, so
# about one turn in 75 goes unjudged at this bound (spec §9.8).
CLASSIFY_TIMEOUT_S = float(os.environ.get("MEMHUB_HARNESS_CLASSIFY_TIMEOUT", "7"))
WINDOW_MAX_CHARS = 24576      # the server refuses a longer body
# The client failing to ask, counted apart from the server's own verdicts, so
# an outage is never read as "nothing here".
CLIENT_REASONS = ("no_credential", "transport_error", "bad_reply")


def extract_enabled(environ=None) -> bool:
    """The one switch for the whole sensor. Default OFF.

    Opt-in because of what on costs: a classifier call per human turn, and a
    background fork per flagged turn on the PERSON'S OWN model quota, filing
    proposals into a shared team rulebook. Unset, blank and unrecognised values
    are all off."""
    env = os.environ if environ is None else environ
    if str(env.get(CHILD_FLAG, "")).strip().lower() in _ON:
        # A `claude` process the plugin's own tooling starts (the case judge,
        # harness/judge/judge.py) is never sensed, whatever FLAG says: Claude
        # Code applies a settings.json `env` over the environment a process
        # inherits, and settings is where an install opts in with FLAG=1.
        return False
    return str(env.get(FLAG, "")).strip().lower() in _ON


# ------------------------------------------------------------------- files
def harness_dir() -> Path:
    return Path(os.environ.get("MEMHUB_HARNESS_DIR")
                or (Path.home() / ".config" / "memhub-plugin" / "harness"))


def log_path(name: str) -> Path:
    return harness_dir() / name


# --------------------------------------------------------------- transcript
# Text the harness generated, not the person. A loop wakeup, a skill body or a
# compaction summary arrives in the user role; treating one as a correction
# would flag the harness talking to itself.
_SYS_BLOCK = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<task-notification>.*?</task-notification>"
    r"|<command-name>.*?</command-name>"
    r"|<local-command-stdout>.*?</local-command-stdout>",
    re.S,
)
_HARNESS_PREFIX = (
    # An author child's first prompt (harness_stop.BLOCK_PREFIX). Were such a
    # session ever sensed, its one turn is the create-rule flow — the text most
    # likely to be flagged — and flagging it is how one child spawned the next.
    "MemHub harness: before you stop",
    # The fork author's completion report (harness_stop.FORK_MARK), delivered
    # to the parent as a task notification: the lane's own output. Usually it
    # arrives inside a <task-notification> wrapper _SYS_BLOCK strips, but the
    # text alone must be enough — classified, a batch could flag the batch
    # that ran before it.
    "MemHub harness fork",
    # A subagent's hand-back, delivered to the parent as a user-role record.
    # Live 2026-09-27: one was flagged, matured, and cost a fork batch a
    # 365k-token read to conclude "none".
    "Another Claude session sent a message:",
    "Base directory for this skill",
    "Continue from where you left off",
    "Caveat: The messages below",
    "Skill /",
    "This session is being continued from a previous conversation",
    "[Request interrupted by user",
    "[Your previous response had no visible output",
)
# A blocked Stop's reason, recorded as an `isMeta` user record (verified on
# Claude Code 2.1.270). Matched on the flag AND the text, never the text alone:
# a person can type a prompt that starts with these words (Codex, #230). The
# harness now hands off through `additionalContext` (an attachment record, read
# below); this stays for transcripts written before that, and for other
# plugins' blocks.
STOP_FEEDBACK_PREFIX = "Stop hook feedback:"
# Named wrappers only. A person's prompt can begin with pasted HTML or a
# Markdown heading, and dropping it would attribute that turn's actions to the
# turn before.
_HARNESS_TAG = re.compile(
    r"<(?:local-command-caveat|command-message|command-args|bash-input"
    r"|bash-stdout|bash-stderr|user-prompt-submit-hook)\b")


def is_harness_text(txt: str) -> bool:
    if not txt:
        return True
    return txt.startswith(_HARNESS_PREFIX) or bool(_HARNESS_TAG.match(txt))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in (content or [])
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _brief(name: str, tool_input: dict) -> str:
    """One line naming the action, short enough to put several in a window."""
    i = tool_input or {}
    if name == "Bash":
        return f"Bash: {str(i.get('command', ''))[:200]}"
    if name in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return f"{name}: {i.get('file_path', '')}"
    return f"{name}: {json.dumps(i, default=str)[:100]}"


def _target_of(name: str, tool_input: dict) -> str:
    """What the action addressed: a command, or a path."""
    i = tool_input or {}
    if name == "Bash":
        return str(i.get("command", ""))
    return str(i.get("file_path", "") or "")


def turns_from_transcript(path, start: int = 0, before: int = 0) -> list[dict]:
    """The person's turns in a transcript; see `read_transcript`."""
    return read_transcript(path, start, before)[0]


def read_transcript(path, start: int = 0, before: int = 0) -> tuple[list[dict], str]:
    """A Claude Code .jsonl → turns. A turn is one human message plus
    everything the agent did before the next one; tool results arrive as
    `user` records and belong to the turn in progress. Each turn carries
    `offset`, the byte its human message starts at, so a caller holding a
    byte boundary can pick the turn that was in progress at it.

    `start` seeks to a byte where a human message begins and `before` is how
    many turns precede it, so a reader resuming from a cursor numbers turns
    exactly as a full read would.

    Also returns the uuid of the LAST prompt of any kind: the person's, a loop
    wakeup, a task notification. A Stop is the end of the turn that prompt
    started, so the caller judges only when it is the last person turn's
    uuid; otherwise a Stop after a background fork's notice would judge the
    person's previous turn a second time."""
    turns: list[dict] = []
    last_prompt = ""
    cur: dict | None = None
    names: dict[str, str] = {}
    inputs: dict[str, dict] = {}
    with open(path, "rb") as fh:
        if start > 0:
            fh.seek(start)
        pos = max(start, 0)
        for raw in fh:
            start, pos = pos, pos + len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            content = (rec.get("message") or {}).get("content")
            att = rec.get("attachment") if kind == "attachment" else None
            if (isinstance(att, dict) and att.get("type") == "hook_additional_context"
                    and att.get("hookEvent") == "Stop"):
                # A Stop hook's `additionalContext` — ours is the fork hand-off
                # — recorded as an attachment, not a user record (probed on
                # 2.1.286). Like a block's reason, it ends the stopped turn:
                # what follows is the continuation it asked for, not the
                # person's. Only Stop: other events' context lands mid-turn.
                cur = None
                continue
            if kind == "user":
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content):
                    if cur is None:
                        continue
                    for b in content:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        body = b.get("content")
                        body = body if isinstance(body, str) else _text_of(body)
                        tid = b.get("tool_use_id")
                        cur["results"].append({
                            "id": tid or "",
                            "tool": names.get(tid, "?"),
                            "target": _target_of(names.get(tid, ""), inputs.get(tid, {})),
                            "error": bool(b.get("is_error")),
                            "text": (body or "")[:600],
                        })
                    continue
                txt = _SYS_BLOCK.sub("", _text_of(content)).strip()
                if rec.get("isMeta") and txt.startswith(STOP_FEEDBACK_PREFIX):
                    # The stopped turn ENDS here. What follows is the blocked
                    # continuation (the create-rule flow, the verdict), not the
                    # person's turn; left attached, a late extractor would hand
                    # the classifier the harness's own output (Codex, #230).
                    cur = None
                    continue
                if rec.get("isMeta"):
                    # Text the harness injected mid-turn: a skill body after
                    # a Skill call, a command's expansion. Not a prompt and
                    # not the person — in the 2026-09-29 replay, skill bodies
                    # read as the person's message were 3% of all flags.
                    continue
                last_prompt = rec.get("uuid", "")
                if is_harness_text(txt):
                    continue
                cur = {"n": before + len(turns) + 1, "user": txt, "tools": [], "results": [],
                       "asst": "", "ts": rec.get("timestamp", ""),
                       "cwd": rec.get("cwd", ""), "uuid": rec.get("uuid", ""),
                       "offset": start}
                turns.append(cur)
            elif kind == "assistant" and cur is not None and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        nm = b.get("name", "")
                        inp = b.get("input") or {}
                        names[b.get("id")] = nm
                        inputs[b.get("id")] = inp
                        cur["tools"].append({"tool": nm, "brief": _brief(nm, inp),
                                             "target": _target_of(nm, inp),
                                             "id": b.get("id") or ""})
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["asst"] = b["text"]
    return turns, last_prompt


# -------------------------------------------------------------- error arcs
def error_arcs(turn: dict) -> list[dict]:
    """Closed error arcs: a tool error on target T, then a later success on
    the same T in the same turn. The pair is the content; a failure that never
    closed is just a failure."""
    arcs = []
    failed: dict[str, tuple[dict, int]] = {}
    for i, r in enumerate(turn.get("results", [])):
        tgt = (r.get("target") or "")[:200]
        if not tgt:
            continue
        if r.get("error"):
            failed.setdefault(tgt, (r, i))
        elif tgt in failed:
            first, at = failed.pop(tgt)
            arcs.append({"signature": first["text"][:200], "target": tgt,
                         "fix": tgt, "cost": i - at})
    return arcs


# ------------------------------------------------------------------ window
def _actions(turn: dict, last: bool) -> list[str]:
    """Up to 12 of the turn's tool calls, each with whether it succeeded and
    the first lines of what it returned: the judge needs the result to tell a
    doubted claim the agent had checked from one it had not (2026-09-30 bench:
    this and the following message cut false fires ~30% at the same recall)."""
    results = {r.get("id"): r for r in turn.get("results", []) if r.get("id")}
    out = []
    for c in turn.get("tools", []):
        line = "  - " + (c.get("brief") or "")
        r = results.get(c.get("id")) if c.get("id") else None
        if r is not None:
            body = "\n".join((r.get("text") or "").strip().splitlines()[:3])[:240]
            line += f"\n    -> {'ERROR' if r.get('error') else 'ok'}: " + body.replace("\n", "\n       ")
        out.append(line)
    out = out[-12:] if last else out[:12]
    return out or ["  (none)"]


def build_window(turn: dict, prev: dict | None, state: dict,
                 earlier: list[dict] | None = None, following: str | None = None) -> str:
    """The moment as text: the person's message, what the agent did around it
    and what each action returned, and — at the Stop of the turn after — what
    the person said next. Tool output is in it and is untrusted, which is why
    it is redacted before it is sent and why whatever the agent later proposes
    is a proposal a person reads. Worst case about 19k characters; the server
    takes 24,576."""
    L = [f"STATE: {json.dumps(state, default=str)}"]
    if earlier:
        L.append("EARLIER USER MESSAGES (oldest first):")
        L += [f"  - {(e.get('user') or '')[:400]}" for e in earlier[-2:]]
    if prev:
        L.append(f"PREVIOUS USER MESSAGE: {(prev.get('user') or '')[:900]}")
        L.append("AGENT'S ACTIONS IN PREVIOUS TURN (each with its result):")
        L += _actions(prev, last=True)
        L.append(f"AGENT'S REPLY IN PREVIOUS TURN: {(prev.get('asst') or '')[-3000:]}")
    L.append(f"USER'S NEW MESSAGE: {(turn.get('user') or '')[:900]}")
    L.append("AGENT'S ACTIONS IN THIS TURN (each with its result):")
    L += _actions(turn, last=False)
    for arc in error_arcs(turn)[:2]:
        L.append(f"  ~ closed error arc on {arc['target'][:80]!r}: "
                 f"{(arc.get('signature') or '')[:150]}")
    L.append(f"AGENT'S FINAL WORDS THIS TURN: {(turn.get('asst') or '')[-1500:]}")
    if following is not None:
        L.append(f"USER'S FOLLOWING MESSAGE (what the person said next): {following[:900]}")
    return "\n".join(L)


def redact_window(text: str) -> str:
    """Credentials, identities and command-line secrets out, before the window
    leaves the machine. Never raises.

    Tool output is untrusted: an org-members listing or a stack trace can
    carry a teammate's name, e-mail or home directory, and a rule built from it
    would publish that identity to the team. Three existing denylists run in
    order: `redact.py`'s MemHub keys, its identity pass (home directories and
    e-mail addresses), and the rulebook hook's command-line credential shapes.
    A denylist is a floor, not a guarantee; MemHub also refuses a drafted rule
    that carries an identity."""
    if not text:
        return text
    out = text
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import redact  # noqa: PLC0415
        out = redact.redact_text(out)
        out = redact.redact_identities(out)
    except Exception:
        pass
    rh = _hook()
    if rh is not None:
        try:
            out = rh.redact_secrets(out)
        except Exception:
            pass
    return out


# -------------------------------------------------------------- classifier
def _api():
    """(rest_base, bearer, mcp_http) or None. Non-interactive: a detached child
    can only spend a credential /memhub:login already minted."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import mcp_http  # noqa: PLC0415
    import pak  # noqa: PLC0415
    from _memhub_auth import resolve_bearer  # noqa: PLC0415
    url, bearer = resolve_bearer(refresh=False)
    if not bearer:
        return None
    return pak.api_base(url), bearer, mcp_http


def server_classify(window: str, repo: str = "",
                    timeout: float = 0) -> tuple[dict, float]:
    """ONE bounded POST. Returns ({signal, reason, kind?, derivable?}, seconds).

    Every failure is a reply with `signal: false` and a CLIENT_REASONS reason,
    so a caller never has to catch anything. No retry: the caller is a
    detached best-effort child."""
    t0 = time.time()
    body = {"window": window[:WINDOW_MAX_CHARS]}
    if repo:
        body["repo"] = repo[:200]
    try:
        api = _api()
    except Exception as exc:                 # noqa: BLE001 — a hook path
        return ({"signal": False, "reason": "no_credential",
                 "detail": repr(exc)[:200]}, round(time.time() - t0, 1))
    if not api:
        return ({"signal": False, "reason": "no_credential"}, round(time.time() - t0, 1))
    base, bearer, http = api
    try:
        reply = http.rest(f"{base}{CLASSIFY_PATH}", bearer, "POST", body=body,
                          timeout=timeout or CLASSIFY_TIMEOUT_S)
    except Exception as exc:                 # noqa: BLE001 — one attempt
        return ({"signal": False, "reason": "transport_error",
                 "detail": str(exc)[:200]}, round(time.time() - t0, 1))
    dt = round(time.time() - t0, 1)
    data = reply.data
    if not (isinstance(data, dict) and isinstance(data.get("signal"), bool)
            and isinstance(data.get("reason"), str)):
        return ({"signal": False, "reason": "bad_reply",
                 "detail": f"HTTP {reply.status}"}, dt)
    return data, dt


# ------------------------------------------------------------- state stamp
def _git(root: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_HOOK_MODULE: list = []      # [] = not tried, [None] = tried and unavailable


def _hook():
    """rulebook_hook, imported once, lazily, and never fatally."""
    if _HOOK_MODULE:
        return _HOOK_MODULE[0]
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import rulebook_hook  # noqa: PLC0415
        _HOOK_MODULE.append(rulebook_hook)
    except Exception:
        _HOOK_MODULE.append(None)
    return _HOOK_MODULE[0]


_REPO_CACHE: dict = {}
_SEGMENTS = re.compile(r"&&|\|\||;|\|")


def git_c_root(base: str, command: str) -> str:
    """The directory `git -C <path>` points a command at, resolved against
    `base`, or "". `git -h`: `git [-C <path>] [-c <name>=<value>] …`, and
    several `-C` are cumulative. The rulebook hook's `command_root` reads only
    a leading `cd`, so without this `git -C B status` run from repo A is A's."""
    for segment in _SEGMENTS.split(command or ""):
        try:
            words = shlex.split(segment)
        except ValueError:
            return ""
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]            # an environment prefix
        if not words or os.path.basename(words[0]) != "git":
            continue
        path, seen, i = base, False, 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] in ("-C", "-c") and i + 1 < len(words):
                if words[i] == "-C":
                    step = os.path.expanduser(words[i + 1])
                    path = step if os.path.isabs(step) else os.path.join(path or "", step)
                    seen = True
                i += 2
                continue
            i += 1
        if seen:
            return path if path and os.path.isdir(path) else ""
    return ""


def resolve_repo(tool: str, target: str, cwd: str) -> tuple[str, str]:
    """(repo_name, worktree_root) for an action: the repository a command
    addresses (its leading `cd`) or a path lives in, else the session's."""
    key = (tool, target, cwd)
    if key not in _REPO_CACHE:
        _REPO_CACHE[key] = _resolve_repo_uncached(tool, target, cwd)
    return _REPO_CACHE[key]


def _resolve_repo_uncached(tool: str, target: str, cwd: str) -> tuple[str, str]:
    rh = _hook()
    if rh is None:
        return "", ""
    try:
        root = ""
        if tool == "Bash" and target:
            root = rh.command_root(cwd, target) or ""
            root = git_c_root(root or cwd, target) or root
        elif target and os.path.isabs(target):
            root = os.path.dirname(target)
        # Never fall through to the current directory: with no cwd the answer
        # is "unknown", not wherever this process happens to be running.
        root = root or cwd
        if not root:
            return "", ""
        name, worktree, _gitdir, _branch = rh.repo_info(root)
        return name or "", worktree or ""
    except Exception:
        return "", ""


def stamp_state(*, session: str, turn: dict, cwd: str, hook_version: str,
                env_name: str, default_repo: str = "") -> dict:
    """The state a proposed rule carries, stamped by the harness and never
    typed: repo, branch and head_sha read now, the environment, the hook
    version, the session and turn, and the time. MemHub refuses a session
    draft without `repo`, `session_id`, `turn`, `hook_version` and `at`."""
    home, home_root = resolve_repo("", "", cwd)
    if not home:
        home, home_root = default_repo, ""
    touched: list[tuple[str, str]] = []
    for action in turn.get("tools", []):
        if not action.get("target"):
            continue        # no command and no path: the action addresses no repository
        name, root = resolve_repo(action.get("tool", ""), action.get("target", ""), cwd)
        if name and name not in [t[0] for t in touched]:
            touched.append((name, root))
    names = [t[0] for t in touched]
    # The repo is the one the turn's actions worked in, so a `cd` into another
    # repository moves the stamp and the proposal's scope with it. A turn that
    # worked in several keeps the session's own when it is one of them.
    if touched and (len(touched) == 1 or home not in names):
        repo, root = touched[0]
    else:
        repo, root = home, home_root
    state = {
        "repo": repo,
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD") if root else "",
        "head_sha": _git(root, "rev-parse", "HEAD")[:12] if root else "",
        "pr_number": None,
        "env": env_name,
        "hook_version": hook_version,
        "session_id": session,
        "turn": turn.get("n"),
        "at": _dt.datetime.now(_dt.timezone.utc)
              .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    # A turn that worked in more than one repository says so, so whoever
    # reviews a rule from it can see the scope is a judgement, not a fact.
    if len(touched) > 1:
        state["touched_repos"] = names
    return state


def plugin_version() -> str:
    try:
        manifest = HERE.parent / ".claude-plugin" / "plugin.json"
        return json.loads(manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"
