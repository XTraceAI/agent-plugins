#!/usr/bin/env python3
"""Rulebook hook — three delivery lanes for team engineering rules. ADVISE-ONLY.

Lanes (the mode argument):
  session  SessionStart: posture rules (on="session") in full, everything else
           as ONE compact index line. Session start is the weakest attention
           slot (measured 4% vs 88% for in-flight), so it carries worldview,
           never enforcement.
  pre      PreToolUse: proactive advisories at the violation moment (on="bash",
           "edit", "write_stdlib") and the ordering-rule GATE (on="ordering").
  post     PostToolUse: reactive advisories on failing/erroring results
           (on="result"); ordering-rule ARM (edit-family) and RECEIPT (bash).

Usage (wired in hooks.json): printf %s "$IN" | python3 rulebook_hook.py {session|pre|post}

Rulebook location: $MEMHUB_RULEBOOK, else ~/.claude/scripts/rulebook/rulebook.json.
State + fire ledger live beside the rulebook so the local pilot and the plugin
share one history. Stdlib only; every failure path exits 0 with no output — a
broken hook must never touch the tool call or the session.

Two engines, one evaluate():
  * matcher rules — `evaluate()` is a pure function of (rule, event); the
    backtest imports it so the replay exercises the code that runs live.
  * ordering rules — "run X after the last edit, before Y": an obligation
    state machine keyed by (worktree_root, branch, rule), never by session,
    so receipts from subagents and sibling sessions in the same checkout count.
"""
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time

BASE = os.path.dirname(os.environ.get("MEMHUB_RULEBOOK", "")) or \
    os.path.expanduser("~/.claude/scripts/rulebook")
RULEBOOK = os.environ.get("MEMHUB_RULEBOOK") or os.path.join(BASE, "rulebook.json")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 3         # full-text rules at session start — context guard
LOCK_WAIT_S = 0.05      # ordering state lock: fail open past this
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "csv", "dataclasses", "datetime", "enum", "functools", "glob", "hashlib",
    "io", "itertools", "json", "logging", "math", "os", "pathlib", "re",
    "shutil", "signal", "socket", "sqlite3", "string", "subprocess", "sys",
    "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unittest", "urllib", "uuid", "warnings",
}
LOCAL_PKGS = {"xmem", "evaluation", "tests", "app", "scripts"}


# ── shell-only segment ──────────────────────────────────────────────────────
_HD_OPEN = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def shell_only(cmd):
    """A command string is two languages in one: the shell that executes and
    the data it carries. Drop heredoc BODY lines; keep every shell line,
    including commands after a terminator. Measured on 57 real transcripts:
    first-`<<` truncation hid 44% of real pushes (`commit -F - <<'MSG' … &&
    git push`); full-string matching made ~half of all fires ghosts.
    Known edge: a bit-shift in a multi-line command can arm a bogus skip."""
    out, skip_until = [], None
    for line in cmd.split("\n"):
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        out.append(line)
        m = _HD_OPEN.search(line)
        if m:
            skip_until = m.group(2)
    return "\n".join(out)


# ── matcher engine: pure ────────────────────────────────────────────────────
def evaluate(rule, *, hook_phase, tool, cmd="", file_path="", body="", result_text=""):
    """True if `rule` fires on this event. No I/O, no dedup — the backtest
    replays this exact function. Ordering rules are not matchers (see
    OrderingEngine)."""
    on = rule.get("on")
    try:
        if hook_phase == "pre" and on == "bash" and tool == "Bash" and cmd:
            target = cmd if rule.get("match_heredoc_body") else shell_only(cmd)
            return bool(re.search(rule["rx"], target, re.I | re.M)) and not (
                rule.get("not_rx") and re.search(rule["not_rx"], target, re.I))
        if hook_phase == "pre" and on == "edit" and tool in EDIT_TOOLS:
            if re.search(rule["path_rx"], file_path) and not (
                    rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)):
                return "content_rx" not in rule or bool(re.search(rule["content_rx"], body, re.M))
            return False
        if hook_phase == "pre" and on == "write_stdlib" and tool == "Write" \
                and file_path.endswith(".py") and "scratchpad" not in file_path \
                and len(body) >= rule.get("min_chars", 800):
            mods = set(re.findall(r"^(?:import|from)\s+([A-Za-z_]\w*)", body, re.M))
            return bool(mods) and not {m for m in mods if m not in STDLIB and m not in LOCAL_PKGS}
        if hook_phase == "post" and on == "result" and result_text:
            if rule.get("cmd_rx") and not re.search(rule["cmd_rx"], cmd, re.I):
                return False
            m = re.search(rule["rx"], result_text[-8000:], re.M)
            return bool(m) and not (
                rule.get("exclude_rx") and re.search(rule["exclude_rx"], m.group(0)))
    except Exception:
        return False
    return False


# ── ordering engine: obligation state machine ───────────────────────────────
class OrderingEngine:
    """State file per worktree root; inside it {branch: {rule_id: {count, last_edit}}}.
    Every read-modify-write holds an exclusive flock on a sidecar lock (bounded
    LOCK_WAIT_S; past that the hook fails open) and replaces the file atomically.
    An arm and a discharge from two sessions must never overwrite each other —
    those are the two outcomes a gate exists to prevent."""

    def __init__(self, worktree_root, branch):
        os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
        key = hashlib.sha1(worktree_root.encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(BASE, "state", f"wt-{key}.json")
        self.branch = branch or "detached"

    def _locked(self):
        lock = open(self.path + ".lock", "a+", encoding="utf-8")
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except OSError:
                if time.monotonic() >= deadline:
                    lock.close()
                    return None
                time.sleep(0.005)

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, st):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".wt-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, self.path)

    def feed(self, rule, *, hook_phase, tool, cmd="", file_path="", ok=None):
        """Returns "fired" | "allowed" | "discharged" | None. Mutates state
        under lock; None on lock timeout (fail open)."""
        spec = rule["ordering"]
        armed_by = tuple(spec.get("armed_by_events", ("edit", "write")))
        is_edit = tool in EDIT_TOOLS and hook_phase == "post"
        if is_edit and not any(k in armed_by for k in ("edit", "write")):
            return None
        if is_edit and spec.get("path_rx") and not re.search(spec["path_rx"], file_path):
            return None
        seg = shell_only(cmd) if cmd else ""
        is_receipt = hook_phase == "post" and tool == "Bash" and seg and \
            re.search(spec["required_command_rx"], seg)
        is_gate = hook_phase == "pre" and tool == "Bash" and seg and \
            re.search(spec["gated_command_rx"], seg)
        if not (is_edit or is_receipt or is_gate):
            return None

        lock = self._locked()
        if lock is None:
            return None
        try:
            st = self._read()
            s = st.setdefault(self.branch, {}).setdefault(
                rule["id"], {"count": 0, "last_edit": None})
            if is_edit:                                   # handler 1: mutation arms
                s["count"] += 1
                s["last_edit"] = file_path
                self._write(st)
                return None
            if is_receipt:                                # handler 2: green receipt
                if ok is True:                            # a red run never discharges
                    s["count"] = 0
                    self._write(st)
                    return "discharged"
                return None
            # handler 3: the gate — read-only
            if s["count"] >= int(spec.get("min_edits", 1)):
                rule["_gate_msg"] = (
                    f"{s['count']} edit(s) since the last passing "
                    f"'{spec.get('display_name', rule['id'])}' "
                    f"(last: {s['last_edit']}). Run it first.")
                return "fired"
            return "allowed"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def bash_ok(resp):
    """Did the Bash call succeed? Uses exit_code when the harness supplies it;
    otherwise the same proxy the transcript replayer uses (error flag, then
    pytest/traceback vocabulary in the head of the output)."""
    if isinstance(resp, dict):
        if isinstance(resp.get("exit_code"), int):
            return resp["exit_code"] == 0
        if resp.get("is_error") or resp.get("isError"):
            return False
    txt = result_text(resp)[:400]
    return not re.search(r"\b(\d+ )?(failed|error(s)?:)|Traceback", txt)


# ── plumbing ────────────────────────────────────────────────────────────────
def load_rules():
    with open(RULEBOOK, encoding="utf-8") as f:
        return json.load(f)["rules"]


def repo_info(cwd):
    """(repo_basename, worktree_root, gitdir_path, branch) via file reads only."""
    d = cwd
    while d and d != "/":
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return os.path.basename(d), d, g, _branch(os.path.join(g, "HEAD"))
        if os.path.isfile(g):   # worktree: "gitdir: /path/to/main/.git/worktrees/x"
            try:
                gitdir = open(g, encoding="utf-8").read().split(":", 1)[1].strip()
            except Exception:
                gitdir = ""
            return os.path.basename(d), d, gitdir, _branch(os.path.join(gitdir, "HEAD"))
        d = os.path.dirname(d)
    return "", "", "", ""


def _branch(head_path):
    try:
        h = open(head_path, encoding="utf-8").read().strip()
        return h.rsplit("/", 1)[-1] if h.startswith("ref:") else "detached"
    except Exception:
        return ""


def scope_ok(rule, repo, gitdir):
    scope = rule.get("repo_scope", "any")
    if scope == "any":
        return True
    return scope in repo or (gitdir and f"/{scope}/" in gitdir)


def state_path(session_id):
    sdir = os.path.join(BASE, "state")
    os.makedirs(sdir, exist_ok=True)
    return os.path.join(sdir, f"{session_id or 'nosession'}.json")


def load_state(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fired": [], "counts": {}}


def save_state(p, st):
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def result_text(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [v for k in ("stderr", "stdout", "output", "error", "text")
                 if isinstance((v := resp.get(k)), str) and v]
        return "\n".join(parts) if parts else json.dumps(resp, ensure_ascii=False)
    return str(resp)


def emit(event_name, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": text}}))


def log_fire(session, repo, mode, tool, rules, excerpt, outcome="fired"):
    try:
        os.makedirs(os.path.join(BASE, "ledger"), exist_ok=True)
        with open(os.path.join(BASE, "ledger", "fires.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "session": session[:8], "repo": repo,
                                "mode": mode, "tool": tool,
                                "rules": [r["id"] for r in rules],
                                "outcome": outcome,
                                "excerpt": excerpt[:160]}) + "\n")
    except Exception:
        pass


def session_digest(rules, repo, gitdir, session):
    in_scope = [r for r in rules if scope_ok(r, repo, gitdir)]
    if not in_scope:
        return
    posture = [r for r in in_scope if r.get("on") == "session"][:MAX_POSTURE]
    active = [r for r in in_scope if r.get("on") != "session"]
    lines = ["## 📏 Rulebook (team rules — advisory)"]
    for r in posture:
        lines.append(f"- {r['text']}  _(why: {r['why']})_")
    if active:
        lines.append(
            f"- {len(active)} rule{'s' if len(active) != 1 else ''} armed for "
            f"this repo — they fire inline as you work (proactive on tool "
            f"calls, reactive on errors). Treat a fire as a teammate's note, "
            f"not boilerplate.")
    emit("SessionStart", "\n".join(lines))
    if posture:
        log_fire(session, repo, "session", "SessionStart", posture, "")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    session = data.get("session_id", "")
    repo, root, gitdir, branch = repo_info(cwd)
    if not repo:            # not in a git repo → no rules apply
        return 0
    rules = load_rules()

    if mode == "session":
        session_digest(rules, repo, gitdir, session)
        return 0

    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    sp = state_path(session)
    st = load_state(sp)
    fired_now = []

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", ""))
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    ok = bash_ok(data.get("tool_response")) if (mode == "post" and tool == "Bash") else None
    ordering = None

    for r in rules:
        if r.get("on") == "session" or not scope_ok(r, repo, gitdir):
            continue
        rid = r["id"]

        if r.get("on") == "ordering":
            try:
                ordering = ordering or OrderingEngine(root, branch)
                outcome = ordering.feed(r, hook_phase=mode, tool=tool, cmd=cmd,
                                        file_path=fp, ok=ok)
            except Exception:
                outcome = None
            if outcome == "discharged":
                log_fire(session, repo, mode, tool, [r], cmd, outcome="discharged")
            elif outcome == "fired":
                fired_now.append(r)
            continue

        scope = r.get("fire_scope", "session")
        key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
        if scope != "call" and not scope.startswith("counter") and key in st["fired"]:
            continue
        if not evaluate(r, hook_phase=mode, tool=tool, cmd=cmd, file_path=fp,
                        body=body, result_text=rtext):
            continue
        if scope.startswith("counter"):
            threshold = int(scope.split(":")[1])
            st["counts"][rid] = st["counts"].get(rid, 0) + 1
            if st["counts"][rid] != threshold:   # fire exactly once, at the Nth hit
                continue
        st["fired"].append(key)
        fired_now.append(r)

    if not fired_now:
        save_state(sp, st)
        return 0

    fired_now = fired_now[:MAX_ADVISE]
    lines = ["## 📏 Rulebook (team rules — advisory, not blocking)"]
    for r in fired_now:
        detail = f" — {r['_gate_msg']}" if r.get("_gate_msg") else ""
        lines.append(f"- **[{r['id']}]** {r['text']}{detail}  _(why: {r['why']})_")
    try:
        emit("PreToolUse" if mode == "pre" else "PostToolUse", "\n".join(lines))
    except Exception:
        pass
    save_state(sp, st)
    log_fire(session, repo, mode, tool, fired_now, cmd or fp or "")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
