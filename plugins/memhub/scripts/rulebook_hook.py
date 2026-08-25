#!/usr/bin/env python3
"""Rulebook hook — three delivery lanes for team engineering rules. ADVISE-ONLY.

Lanes (the mode argument):
  session  SessionStart: posture rules (on="session") in full, everything else
           as ONE compact index line. Session start is the weakest attention
           slot (measured 4% vs 88% for in-flight), so it carries worldview,
           never enforcement.
  pre      PreToolUse: proactive advisories at the violation moment (on="bash",
           "edit", "write_stdlib").
  post     PostToolUse: reactive advisories on failing/erroring results
           (on="result").

Usage (wired in hooks.json): printf %s "$IN" | python3 rulebook_hook.py {session|pre|post}

Rulebook location: $MEMHUB_RULEBOOK, else ~/.claude/scripts/rulebook/rulebook.json.
State + fire ledger live beside the rulebook so the local pilot and the plugin
share one history. Stdlib only; every failure path exits 0 with no output — a
broken hook must never touch the tool call or the session.
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.environ.get("MEMHUB_RULEBOOK", "")) or \
    os.path.expanduser("~/.claude/scripts/rulebook")
RULEBOOK = os.environ.get("MEMHUB_RULEBOOK") or os.path.join(BASE, "rulebook.json")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 3         # full-text rules at session start — context guard
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "csv", "dataclasses", "datetime", "enum", "functools", "glob", "hashlib",
    "io", "itertools", "json", "logging", "math", "os", "pathlib", "re",
    "shutil", "signal", "socket", "sqlite3", "string", "subprocess", "sys",
    "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unittest", "urllib", "uuid", "warnings",
}
LOCAL_PKGS = {"xmem", "evaluation", "tests", "app", "scripts"}


def load_rules():
    with open(RULEBOOK, encoding="utf-8") as f:
        return json.load(f)["rules"]


def repo_info(cwd):
    """(repo_basename, gitdir_path, branch) via file reads only — no subprocess."""
    d = cwd
    while d and d != "/":
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return os.path.basename(d), g, _branch(os.path.join(g, "HEAD"))
        if os.path.isfile(g):   # worktree: "gitdir: /path/to/main/.git/worktrees/x"
            try:
                gitdir = open(g, encoding="utf-8").read().split(":", 1)[1].strip()
            except Exception:
                gitdir = ""
            return os.path.basename(d), gitdir, _branch(os.path.join(gitdir, "HEAD"))
        d = os.path.dirname(d)
    return "", "", ""


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


def log_fire(session, repo, mode, tool, rules, excerpt):
    try:
        os.makedirs(os.path.join(BASE, "ledger"), exist_ok=True)
        with open(os.path.join(BASE, "ledger", "fires.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "session": session[:8], "repo": repo,
                                "mode": mode, "tool": tool,
                                "rules": [r["id"] for r in rules],
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
    repo, gitdir, branch = repo_info(cwd)
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

    for r in rules:
        if r.get("on") == "session" or not scope_ok(r, repo, gitdir):
            continue
        rid = r["id"]
        scope = r.get("fire_scope", "session")
        key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
        if scope != "call" and not scope.startswith("counter") and key in st["fired"]:
            continue

        hit = False
        try:
            if mode == "pre" and r["on"] == "bash" and tool == "Bash" and cmd:
                # Rules match the pre-heredoc segment only — heredoc bodies are
                # data (python source, commit messages) and were the pilot's
                # whole false-fire class. A rule targeting heredocs opts in.
                target = cmd if r.get("match_heredoc_body") else cmd.split("<<", 1)[0]
                if re.search(r["rx"], target, re.I | re.M) and not (
                        r.get("not_rx") and re.search(r["not_rx"], target, re.I)):
                    hit = True
            elif mode == "pre" and r["on"] == "edit" and tool in ("Edit", "Write", "MultiEdit"):
                if re.search(r["path_rx"], fp) and not (
                        r.get("path_not_rx") and re.search(r["path_not_rx"], fp)):
                    if "content_rx" not in r or re.search(r["content_rx"], body, re.M):
                        hit = True
            elif mode == "pre" and r["on"] == "write_stdlib" and tool == "Write" \
                    and fp.endswith(".py") and "scratchpad" not in fp \
                    and len(body) >= r.get("min_chars", 800):
                mods = set(re.findall(r"^(?:import|from)\s+([A-Za-z_]\w*)", body, re.M))
                if mods and not {m for m in mods if m not in STDLIB and m not in LOCAL_PKGS}:
                    hit = True
            elif mode == "post" and r["on"] == "result" and rtext:
                if r.get("cmd_rx") and not re.search(r["cmd_rx"], cmd, re.I):
                    continue
                m = re.search(r["rx"], rtext[-8000:], re.M)
                if m and not (r.get("exclude_rx") and re.search(r["exclude_rx"], m.group(0))):
                    hit = True
        except Exception:
            hit = False
        if not hit:
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
        lines.append(f"- **[{r['id']}]** {r['text']}  _(why: {r['why']})_")
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
