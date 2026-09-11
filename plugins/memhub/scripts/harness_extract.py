#!/usr/bin/env python3
"""Harness-tied memory, the client half: which moments of a session deserve
the coding agent's attention.

Nothing here writes a rule. At each turn's Stop, `harness_stop.py` runs that
turn through this pipeline in a detached child:

    router       deterministic regexes over the turn, no model and no cost.
                 It labels a moment (a correction, a retraction, a reuse
                 complaint, a gate override, a closed error arc) and spares
                 the call on a turn whose only hit is an unbacked claim. A
                 label is a hint for the classifier, never a decision.
    window       the moment as text: the previous user message with that
                 turn's last actions and words, this user message, this turn's
                 first actions, its errors and closed error arcs, its final
                 words, and one state line. REDACTED before it leaves the
                 machine.
    classifier   ONE bounded POST to MemHub
                 `/v1/team/rulebook/harness/classify`: is this moment worth
                 handing to the agent, and of what kind.
    moment       on a signal, the turn, kind, router hint and state stamp,
                 appended to a local file for the prompt lane to hand over.

The agent that lived the turn writes the lesson, if there is one, at the next
prompt (`harness_stop.py prompt`), through the memhub `create_rule` tool.

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
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

FLAG = "MEMHUB_HARNESS_EXTRACT"
_ON = ("1", "on", "true", "yes")

CLASSIFY_PATH = "/v1/team/rulebook/harness/classify"
# The server bounds its judge at 20 s. One attempt, and this is the whole wait:
# a moment the classifier never answered for is simply not handed over.
CLASSIFY_TIMEOUT_S = float(os.environ.get("MEMHUB_HARNESS_CLASSIFY_TIMEOUT", "30"))
WINDOW_MAX_CHARS = 24576      # the server refuses a longer body
# The client failing to ask, counted apart from the server's own verdicts, so
# an outage is never read as "nothing here".
CLIENT_REASONS = ("no_credential", "transport_error", "bad_reply")


def extract_enabled(environ=None) -> bool:
    """The one switch for the whole sensor. Default OFF."""
    env = os.environ if environ is None else environ
    return str(env.get(FLAG, "")).strip().lower() in _ON


# ------------------------------------------------------------------- files
def harness_dir() -> Path:
    return Path(os.environ.get("MEMHUB_HARNESS_DIR")
                or (Path.home() / ".config" / "memhub-plugin" / "harness"))


def session_file(session: str, suffix: str) -> Path:
    """A per-session file. The session id is a filename component and nothing
    else."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:80] or "nosession"
    return harness_dir() / f"{safe}{suffix}"


def log_path(name: str) -> Path:
    return harness_dir() / name


def append_jsonl(path: Path, row: dict) -> None:
    """Append one row, creating the file private (0600): a moment carries a
    redacted slice of the session, which is still nobody else's business on a
    shared machine."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Every row in a JSONL file; a broken line is skipped, never fatal."""
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


class Trace:
    """One line per step, to a private log. Never prompt text: the log sits
    outside the transcript and outlives the session."""

    def __init__(self, path: str = ""):
        self.fh = None
        if path:
            try:
                p = Path(path)
                p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self.fh = os.fdopen(fd, "a", encoding="utf-8")
            except OSError:
                self.fh = None

    def __call__(self, msg: str) -> None:
        if self.fh:
            self.fh.write(msg + "\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()


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
    "Base directory for this skill",
    "Continue from where you left off",
    "Caveat: The messages below",
    "Skill /",
    "This session is being continued from a previous conversation",
    "[Request interrupted by user",
)
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


def turns_from_transcript(path) -> list[dict]:
    """A Claude Code .jsonl → turns. A turn is one human message plus
    everything the agent did before the next one; tool results arrive as
    `user` records and belong to the turn in progress."""
    turns: list[dict] = []
    cur: dict | None = None
    names: dict[str, str] = {}
    inputs: dict[str, dict] = {}
    with open(path, errors="replace", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            content = (rec.get("message") or {}).get("content")
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
                            "tool": names.get(tid, "?"),
                            "target": _target_of(names.get(tid, ""), inputs.get(tid, {})),
                            "error": bool(b.get("is_error")),
                            "text": (body or "")[:600],
                        })
                    continue
                txt = _SYS_BLOCK.sub("", _text_of(content)).strip()
                if is_harness_text(txt):
                    continue
                cur = {"n": len(turns) + 1, "user": txt, "tools": [], "results": [],
                       "asst": "", "ts": rec.get("timestamp", ""),
                       "cwd": rec.get("cwd", ""), "uuid": rec.get("uuid", "")}
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
                                             "target": _target_of(nm, inp)})
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["asst"] = b["text"]
    return turns


# ------------------------------------------------------------------ router
RETRACT = re.compile(
    r"\b(i was wrong|my mistake|correction:|turns out|let me correct"
    r"|i re-?checked,? and|actually,? (it|that|the)\b.{0,40}\bnot\b)\b", re.I)
CLAIM = re.compile(
    r"\b(fixed|merged|deployed|verified|all (tests )?pass(ed|ing)?"
    r"|is live|works now|ready to merge)\b", re.I)
RECEIPT = re.compile(
    r"gh pr (view|checks)|pytest|uv run|git (log|status|diff)|curl "
    r"|REAL_EXIT|exit code|npm test|pnpm test", re.I)
RULEREQ = re.compile(
    r"\b(create|add|make) (a |an )?(rule|lesson)\b"
    r"|\bnever\b.{0,60}\balways\b|\balways\b.{0,60}\bnever\b"
    r"|\bfrom now on\b|\bevery time\b.{0,40}\b(you|u)\b", re.I)
REUSE = re.compile(
    r"\b(we already have|already exists|don'?t we already"
    r"|i thought we already|why (are u|are you|did u|did you) (re)?building)\b",
    re.I)
OVERRIDE = re.compile(r"RULEBOOK_OVERRIDE=", re.I)
WRONG_TARGET = re.compile(
    r"\b(i mean|i meant|not (that|the) (repo|branch|env|brain|one)"
    r"|check staging|on staging|it'?s (on |in )?(staging|prod))\b", re.I)

# An error whose cause is a trap in this environment rather than a typo. A trap
# repeats for the next person; a typo does not.
TRAPS = [
    (r"ModuleNotFoundError|No module named", "missing-module"),
    (r"command not found: (timeout|rg|pg_isready|psql|gtimeout)", "missing-binary"),
    (r"unexpected keyword argument", "kwarg-drift"),
    (r"Blocked by the XTrace team rulebook", "gate-block"),
    (r"MissingGreenlet|greenlet_spawn", "async-context"),
    (r"relation \"[^\"]+\" does not exist", "schema-drift"),
    (r"could not translate host name|connection refused", "wrong-endpoint"),
]
# A closed arc that took this many tool calls is routed whatever its error
# said: a trap that cost five calls is a trap even if this list has no name
# for it.
ARC_COST_ROUTES = 5

# Counted, and not sent. A claim with no receipt is the agent's own words, not
# an action a rule could match, so it is not a moment to hand over. This spares
# the classifier call on a turn whose only hit is one.
NOT_AUTHORED = {"claim_no_receipt"}


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


def route(turn: dict, prev: dict | None,
          arcs: list[dict] | None = None) -> list[tuple[str, str]]:
    """Reasons this turn may hold a lesson. Empty means ask the classifier
    with no hint. `arcs` are the closed error arcs the rulebook hook paired
    live; they join the ones the transcript shows."""
    hits: list[tuple[str, str]] = []
    asst = turn.get("asst") or ""
    user = turn.get("user") or ""
    tools = [t.get("brief", "") for t in turn.get("tools", [])]

    m = RETRACT.search(asst)
    if m:
        hits.append(("retraction", m.group(0)))
    m = CLAIM.search(asst)
    if m and not any(RECEIPT.search(t) for t in tools[-8:]):
        hits.append(("claim_no_receipt", m.group(0)))
    if RULEREQ.search(user):
        hits.append(("standing_rule_request", user[:80]))
    if REUSE.search(user):
        hits.append(("reuse_correction", user[:80]))
    if WRONG_TARGET.search(user):
        hits.append(("wrong_target", user[:80]))
    if any(OVERRIDE.search(t) for t in tools):
        hits.append(("gate_override", ""))
    seen = set()
    for arc in error_arcs(turn) + list(arcs or []):
        key = (arc.get("target") or "")[:200]
        if key in seen:
            continue
        name = ""
        for rx, trap in TRAPS:
            if re.search(rx, arc.get("signature") or ""):
                name = trap
                break
        if not name and (arc.get("cost") or 0) >= ARC_COST_ROUTES:
            name = f"cost-{arc.get('cost')}"
        if name:
            seen.add(key)
            hits.append(("error_arc", name))
    return hits


def router_hint(hits: list[tuple[str, str]]) -> str:
    """The label the classifier sees: the first hit that is not a bare claim."""
    for kind, _ in hits:
        if kind not in NOT_AUTHORED:
            return kind
    return ""


# ------------------------------------------------------------------ window
def build_window(turn: dict, prev: dict | None, state: dict,
                 arcs: list[dict] | None = None) -> str:
    """The moment as text. Tool output is in it and is untrusted, which is
    why it is redacted before it is sent and why whatever the agent later
    proposes is a proposal a person reads."""
    L = [f"STATE: {json.dumps(state, default=str)}"]
    if prev:
        L.append(f"PREVIOUS USER MESSAGE: {(prev.get('user') or '')[:600]}")
        acts = [t.get("brief", "") for t in prev.get("tools", [])][-4:]
        L.append("AGENT'S ACTIONS IN PREVIOUS TURN (last 4):")
        L += ["  - " + a for a in acts] or ["  (none)"]
        L.append(f"AGENT'S LAST WORDS IN PREVIOUS TURN: {(prev.get('asst') or '')[-500:]}")
    L.append(f"USER'S NEW MESSAGE: {(turn.get('user') or '')[:900]}")
    acts = [t.get("brief", "") for t in turn.get("tools", [])][:6]
    L.append("AGENT'S ACTIONS IN THIS TURN (first 6):")
    L += ["  - " + a for a in acts] or ["  (none)"]
    for e in [r for r in turn.get("results", []) if r.get("error")][:3]:
        L.append(f"  ! error from {e.get('tool')}: {(e.get('text') or '')[:250]}")
    for arc in (error_arcs(turn) + list(arcs or []))[:2]:
        L.append(f"  ~ closed error arc on {arc['target'][:80]!r}: "
                 f"{(arc.get('signature') or '')[:150]}")
    L.append(f"AGENT'S FINAL WORDS THIS TURN: {(turn.get('asst') or '')[-700:]}")
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


def server_classify(window: str, hint: str = "", repo: str = "",
                    timeout: float = 0) -> tuple[dict, float]:
    """ONE bounded POST. Returns ({signal, reason, kind?, derivable?}, seconds).

    Every failure is a reply with `signal: false` and a CLIENT_REASONS reason,
    so a caller never has to catch anything. No retry: the caller is a
    detached best-effort child."""
    t0 = time.time()
    body = {"window": window[:WINDOW_MAX_CHARS]}
    if hint:
        body["hint"] = hint[:64]
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
    repo, root = resolve_repo("", "", cwd)
    if not repo:
        repo, root = default_repo, ""
    touched: list[str] = []
    for action in turn.get("tools", []):
        name, _ = resolve_repo(action.get("tool", ""), action.get("target", ""), cwd)
        if name and name not in touched:
            touched.append(name)
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
        state["touched_repos"] = touched
    return state


def plugin_version() -> str:
    try:
        manifest = HERE.parent / ".claude-plugin" / "plugin.json"
        return json.loads(manifest.read_text(encoding="utf-8")).get("version", "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


# -------------------------------------------------------------------- turn
def new_stats() -> dict:
    return {"router_hits": 0, "turns_spared": 0, "turns_sent": 0, "moments": 0,
            "reason": "", "transport_errors": 0, "seconds": 0.0}


def extract_turn(turn: dict, prev: dict | None, *, session: str, cwd: str,
                 repo: str, env_name: str, stats: dict, trace, out_path: Path,
                 arcs: list[dict] | None = None, hook_version: str = "",
                 timeout: float = 0) -> dict | None:
    """One turn through router → window → classifier → moment. Returns the
    moment appended to `out_path`, or None."""
    hits = route(turn, prev, arcs)
    stats["router_hits"] += len(hits)
    trace(f"turn {turn.get('n')} | router: {[k for k, _ in hits] or '-'}")
    hint = router_hint(hits)
    if hits and not hint:
        stats["turns_spared"] += 1
        trace("   not sent: the only hit is a claim with no receipt")
        return None

    state = stamp_state(session=session, turn=turn, cwd=cwd,
                        hook_version=hook_version or plugin_version(),
                        env_name=env_name, default_repo=repo)
    window = redact_window(build_window(turn, prev, state, arcs))
    t0 = time.time()
    reply, dt = server_classify(window, hint, repo=state.get("repo", ""), timeout=timeout)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["turns_sent"] += 1
    reason = str(reply.get("reason") or "")
    stats["reason"] = reason
    if reason in CLIENT_REASONS:
        stats["transport_errors"] += 1
    if not reply.get("signal"):
        trace(f"   classifier ({dt}s): no signal [{reason}]")
        return None
    kind = reply.get("kind")
    moment = {"turn": turn.get("n"), "source_ref": f"{session}#{turn.get('n')}",
              "hint": hint, "kind": kind, "derivable": reply.get("derivable"),
              "state": state}
    append_jsonl(out_path, moment)
    stats["moments"] += 1
    trace(f"   classifier ({dt}s): signal [{kind}], moment recorded")
    return moment


# ------------------------------------------------------------------- spawn
def spawn_detached(argv: list[str], script: Path, log_name: str) -> int:
    """Run `script argv` fully detached: the caller is a hook that must return
    at once, and the child must survive the session ending."""
    args = [sys.executable, str(script)] + list(argv)
    log_file = log_path(log_name)
    try:
        log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log = os.fdopen(fd, "a", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    try:
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                  "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)
    except OSError:
        return 0        # fail open and silent, like every hook path
    return 0
