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
import uuid
from datetime import datetime, timezone

BASE = os.path.dirname(os.environ.get("MEMHUB_RULEBOOK", "")) or \
    os.path.expanduser("~/.claude/scripts/rulebook")
RULEBOOK = os.environ.get("MEMHUB_RULEBOOK") or os.path.join(BASE, "rulebook.json")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 3         # full-text rules at session start — context guard
LOCK_WAIT_S = 0.05      # ordering state lock: fail open past this
LEDGER_SCHEMA = 2       # ledger/fires.jsonl row shape (spec §3.2); v1 = per-tool-call rows
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
_HD_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")   # delimiter must be a word, so `x << 2` is a shift


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


def last_segment(shell):
    """The final command segment of a shell string (split on ;, &&, ||, newline)."""
    parts = [x.strip() for x in re.split(r"&&|\|\||;|\n", shell) if x.strip()]
    return parts[-1] if parts else ""


# ── matcher engine: pure ────────────────────────────────────────────────────
def evaluate(rule, *, hook_phase, tool, cmd="", file_path="", body="", result_text=""):
    """True if `rule` fires on this event. No I/O, no dedup — the backtest
    replays this exact function. Ordering rules are not matchers (see
    OrderingEngine)."""
    on = rule.get("on")
    try:
        if hook_phase == "pre" and on == "bash" and tool == "Bash" and cmd:
            # Rules ABOUT payloads (`body_rx`): rx still names the shell shape
            # (`python - <<`), body_rx says what the payload must be about — so
            # a spec file that merely *contains* "python3 - <<" never fires.
            # Legacy `match_heredoc_body` without body_rx matches the whole string.
            shell = shell_only(cmd)
            target = cmd if (rule.get("match_heredoc_body") and not rule.get("body_rx")) else shell
            if not re.search(rule["rx"], target, re.I | re.M):
                return False
            if rule.get("not_rx") and re.search(rule["not_rx"], target, re.I):
                return False
            if rule.get("body_rx"):
                kept = set(shell.split("\n"))
                body_only = "\n".join(l for l in cmd.split("\n") if l not in kept)
                return bool(re.search(rule["body_rx"], body_only, re.I | re.M))
            return True
        if hook_phase == "pre" and on == "edit" and tool in EDIT_TOOLS:
            if re.search(rule["path_rx"], file_path) and not (
                    rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)):
                return "content_rx" not in rule or bool(re.search(rule["content_rx"], body, re.M))
            return False
        if hook_phase == "pre" and on == "write_stdlib" and tool == "Write" \
                and file_path.endswith(".py") and "scratchpad" not in file_path \
                and not (rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)) \
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
    """State file per worktree root; inside it {"*": {rule_id: {count, last_edit}}}.
    Keyed by WORKTREE, not branch: a working tree carries uncommitted edits
    across `git checkout -b`, so a branch-keyed obligation would vanish on a
    branch switch before the push. Sibling branches share it (over-gates
    slightly — the safe direction).
    Every read-modify-write holds an exclusive flock on a sidecar lock (bounded
    LOCK_WAIT_S; past that the hook fails open) and replaces the file atomically.
    An arm and a discharge from two sessions must never overwrite each other —
    those are the two outcomes a gate exists to prevent."""

    def __init__(self, worktree_root, branch):
        os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
        key = hashlib.sha1(worktree_root.encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(BASE, "state", f"wt-{key}.json")
        self.branch = "*"            # branch is recorded on fires, not used as a key

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

    def mark_fired(self, rule_id, fire_id):
        """Remember the open fire in WORKTREE state so a later discharge from
        any session in this checkout converts it."""
        lock = self._locked()
        if lock is None:
            return
        try:
            st = self._read()
            st.setdefault(self.branch, {}).setdefault(
                rule_id, {"count": 0, "last_edit": None})["open_fire"] = fire_id
            self._write(st)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

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
        # A Bash call reports ONE exit status. It is the receipt's own status
        # only when the receipt is the final segment and not piped (`pytest |
        # tail` returns tail's status). Earlier segments / pipelines never
        # discharge — under-counting is the safe direction.
        last = last_segment(seg) if seg else ""
        is_receipt = hook_phase == "post" and tool == "Bash" and seg and \
            re.search(spec["required_command_rx"], last) and \
            "|" not in last and not last.rstrip().endswith("&")   # piped / backgrounded: status isn't the suite's
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
                    # conversion is (worktree, branch)-scoped: a subagent's or
                    # sibling session's receipt converts whichever fire is open
                    rule["_converted_fire"] = s.pop("open_fire", None)
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


def bash_ok(resp, *, strict=False):
    """Did the Bash call succeed? Uses exit_code when the harness supplies it.
    Without one, the text proxy (same as the transcript replayer) is a guess a
    command's own output could forge — so `strict=True` (used for GATE-mode
    receipts) returns False unless an explicit exit_code says 0."""
    if resp is None:                      # no result at all is never a receipt
        return False
    if isinstance(resp, dict):
        if isinstance(resp.get("exit_code"), int):
            return resp["exit_code"] == 0
        if resp.get("is_error") or resp.get("isError"):
            return False
    if strict:
        return False
    txt = result_text(resp)
    # text proxy, anchored to pytest/traceback vocabulary — a green run whose
    # output merely mentions "error:" must not be mistaken for red
    return not re.search(
        r"(^|\n)(FAILED|ERROR)\b|\b\d+ (failed|errors?)\b|\nTraceback \(most recent call last\)"
        r"|(^|\n)npm ERR!|(^|\n)error(\[E\d+\])?:", txt)


# ── plumbing ────────────────────────────────────────────────────────────────
def load_rules():
    with open(RULEBOOK, encoding="utf-8") as f:
        book = json.load(f)
    return book["rules"], str(book.get("version", ""))


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
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or ""))[:80] or "nosession"
    return os.path.join(sdir, f"{safe}.json")


def load_state(p):
    st = {"fired": [], "counts": {}, "raw": {}, "open": {}}
    try:
        with open(p, encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


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


def _ledger_dir():
    d = os.path.join(BASE, "ledger")
    os.makedirs(d, exist_ok=True)
    sv = os.path.join(d, "schema_version")
    # stamp v2 only on a FRESH ledger; an unstamped ledger with rows is v1 and
    # must go through rulebook_ledger_migrate.py — never silently relabel it
    if not os.path.exists(sv) and not os.path.exists(os.path.join(d, "fires.jsonl")):
        with open(sv, "w", encoding="utf-8") as f:
            f.write(f"{LEDGER_SCHEMA}\n")
    return d


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def agent_id_of(data):
    """Subagent transcripts live at <session>/subagents/agent-<id>.jsonl;
    the main agent's do not. NULL = main agent."""
    tp = str(data.get("transcript_path") or "")
    if "/subagents/" in tp:
        return os.path.basename(tp).rsplit(".", 1)[0]
    return None


def log_fires(ctx, rules, *, hook_phase, mode, excerpt, raw_counts=None, dedup_keys=None):
    """One ledger row per (rule, fire) — spec §3.2. Identifiers, not payloads:
    `excerpt` stays in this LOCAL file and never crosses the wire without
    org opt-in. Returns {rule_id: fire_id} so conversions can point back."""
    ids = {}
    try:
        path = os.path.join(_ledger_dir(), "fires.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for r in rules:
                fid = str(uuid.uuid4())
                ids[r["id"]] = fid
                f.write(json.dumps({
                    "fire_id": fid, "rule_id": r["id"],
                    "rule_version": ctx["rule_version"],
                    "session_id": ctx["session"], "agent_id": ctx["agent_id"],
                    "repo": ctx["repo"], "branch": ctx["branch"], "tool": ctx["tool"],
                    "hook_phase": hook_phase, "mode": mode,
                    "dedup_key": (dedup_keys or {}).get(r["id"]),
                    "raw_matches_before_fire": (raw_counts or {}).get(r["id"]),
                    "fired_at": _now(),
                    "converted": None, "converted_at": None,
                    "excerpt": excerpt[:160],
                }) + "\n")
    except Exception:
        pass
    return ids


def log_conversion(fire_id, how):
    """Append-only sidecar (the fires file is shared across sessions, so it
    is never rewritten in place). A reader merges by fire_id."""
    try:
        with open(os.path.join(_ledger_dir(), "conversions.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"fire_id": fire_id, "converted": True,
                                "converted_at": _now(), "how": how}) + "\n")
    except Exception:
        pass


def session_digest(rules, repo, gitdir, ctx):
    in_scope = [r for r in rules if scope_ok(r, repo, gitdir) and r.get("status", "active") == "active"]
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
        log_fires(ctx, posture, hook_phase="session", mode="advise", excerpt="")


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
    rules, rule_version = load_rules()
    tool = data.get("tool_name", "")
    ctx = {"session": session, "agent_id": agent_id_of(data), "repo": repo,
           "branch": branch, "tool": tool, "rule_version": rule_version}

    if mode == "session":
        session_digest(rules, repo, gitdir, ctx)
        return 0

    inp = data.get("tool_input") or {}
    sp = state_path(session)
    st = load_state(sp)
    fired_now = []

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", "")) + \
        "\n".join(str(e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict))
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    resp = data.get("tool_response") if (mode == "post" and tool == "Bash") else None
    ordering = None
    dedup_keys = {}
    by_id = {r["id"]: r for r in rules}

    # Conversions: did this call perform the action an earlier fire asked for?
    # Deterministic, under-counts, never over-counts (spec §5.1).
    for rid, fid in list(st["open"].items()):
        r = by_id.get(rid)
        if not r:
            continue
        crx = r.get("converted_rx")
        if mode == "post" and tool == "Bash" and crx and cmd \
                and re.search(crx, shell_only(cmd), re.I | re.M):
            log_conversion(fid, "converted_rx")
            del st["open"][rid]
            st.get("open_file", {}).pop(rid, None)
        elif mode == "pre" and r.get("on") == "edit" and "content_rx" in r \
                and tool in EDIT_TOOLS and fp == st.get("open_file", {}).get(rid) \
                and not evaluate(r, hook_phase="pre", tool=tool, file_path=fp, body=body):
            log_conversion(fid, "re-edit-clears")
            del st["open"][rid]
            st.get("open_file", {}).pop(rid, None)

    for r in rules:
        if r.get("on") == "session" or not scope_ok(r, repo, gitdir) \
                or r.get("status", "active") != "active":   # draft = not armed (§6)
            continue
        rid = r["id"]

        if r.get("on") == "ordering":
            try:
                ordering = ordering or OrderingEngine(root, branch)
                ok = bash_ok(resp, strict=r.get("mode") == "gate") if resp is not None else None
                outcome = ordering.feed(r, hook_phase=mode, tool=tool, cmd=cmd,
                                        file_path=fp, ok=ok)
            except Exception:
                outcome = None
            if outcome == "discharged" and r.get("_converted_fire"):
                log_conversion(r["_converted_fire"], "discharged")
            elif outcome == "fired":
                dedup_keys[rid] = f"{rid}@{root}:{branch}"
                fired_now.append(r)
            continue

        scope = r.get("fire_scope", "session")
        key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
        if scope != "call" and not scope.startswith("counter") and key in st["fired"]:
            if evaluate(r, hook_phase=mode, tool=tool, cmd=cmd, file_path=fp,
                        body=body, result_text=rtext):
                st["raw"][rid] = st["raw"].get(rid, 0) + 1   # what dedup swallowed
            continue
        if not evaluate(r, hook_phase=mode, tool=tool, cmd=cmd, file_path=fp,
                        body=body, result_text=rtext):
            continue
        st["raw"][rid] = st["raw"].get(rid, 0) + 1
        if scope.startswith("counter"):
            try:
                threshold = int(scope.split(":", 1)[1])
            except (IndexError, ValueError):
                threshold = 1               # a malformed scope must not silence the whole call
            st["counts"][rid] = st["counts"].get(rid, 0) + 1
            if st["counts"][rid] != threshold:   # fire exactly once, at the Nth hit
                continue
        st["fired"].append(key)
        dedup_keys[rid] = key
        fired_now.append(r)

    if not fired_now:
        save_state(sp, st)
        return 0

    shown, cut = fired_now[:MAX_ADVISE], fired_now[MAX_ADVISE:]
    lines = ["## 📏 Rulebook (team rules — advisory, not blocking)"]
    for r in shown:
        detail = f" — {r['_gate_msg']}" if r.get("_gate_msg") else ""
        lines.append(f"- **[{r['id']}]** {r['text']}{detail}  _(why: {r['why']})_")
    try:
        emit("PreToolUse" if mode == "pre" else "PostToolUse", "\n".join(lines))
    except Exception:
        pass
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired_now}
    ids = log_fires(ctx, shown, hook_phase=mode, mode="advise", excerpt=cmd or fp or "",
                    raw_counts=raw, dedup_keys=dedup_keys)
    if cut:   # the per-call cap has a cost; make it visible, never silent
        log_fires(ctx, cut, hook_phase=mode, mode="suppressed", excerpt=cmd or fp or "",
                  raw_counts=raw, dedup_keys=dedup_keys)
    for r in shown:
        st["raw"][r["id"]] = 0
        if r.get("on") == "ordering" and ordering and ids.get(r["id"]):
            ordering.mark_fired(r["id"], ids[r["id"]])
        elif r.get("converted_rx") or (r.get("on") == "edit" and "content_rx" in r):
            st["open"][r["id"]] = ids.get(r["id"])
            if r.get("on") == "edit":
                st.setdefault("open_file", {})[r["id"]] = fp
    save_state(sp, st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        sys.exit(0)
