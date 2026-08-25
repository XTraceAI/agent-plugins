"""Self-test for the rulebook hook's three delivery lanes.

Covers the properties that make this safe to ship in everyone's harness:

* every failure path is SILENT and exit-0 — a broken hook must never touch a
  tool call or a session (missing rulebook, corrupt JSON, garbage stdin,
  non-git cwd);
* the session lane serves posture rules in full and everything else as one
  index line — never the whole book;
* the pre lane matches the pre-heredoc segment only, honors not_rx, and
  dedupes per fire_scope=session (the habituation guard);
* the post lane fires on failing result text, gated by cmd_rx;
* repo_scope filters rules to the repo the session is in;
* MEMHUB_RULEBOOK relocates the book AND its state/ledger together, so tests
  never touch the developer's real rulebook.

Run: python3 rulebook_hook_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(__file__), "..",
                    "plugins", "memhub", "scripts", "rulebook_hook.py")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run(mode: str, payload: dict, env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ, **env_extra)
    p = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


def ctx(out: str) -> str:
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "xmem")           # fake git repo named xmem
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/test-branch\n")
        other = os.path.join(td, "otherrepo")
        os.makedirs(os.path.join(other, ".git"))

        book = os.path.join(td, "rulebook.json")
        rules = {"version": 1, "rules": [
            {"id": "posture-one", "on": "session", "fire_scope": "session",
             "repo_scope": "xmem", "text": "Posture text", "why": "posture why"},
            {"id": "bash-rule", "on": "bash", "rx": r"forbidden-cmd",
             "not_rx": r"allowed-context", "fire_scope": "session",
             "repo_scope": "any", "text": "Bash advisory", "why": "w"},
            {"id": "xmem-only", "on": "bash", "rx": r"xmem-only-cmd",
             "fire_scope": "session", "repo_scope": "xmem",
             "text": "Xmem advisory", "why": "w"},
            {"id": "post-rule", "on": "result", "rx": r"BOOM-ERROR",
             "cmd_rx": r"pytest", "fire_scope": "session", "repo_scope": "any",
             "text": "Post advisory", "why": "w"},
        ]}
        with open(book, "w", encoding="utf-8") as f:
            json.dump(rules, f)
        env = {"MEMHUB_RULEBOOK": book}

        # --- fail-open properties -----------------------------------------
        rc, out = run("pre", {"cwd": "/", "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}}, env)
        check("non-git cwd is silent", rc == 0 and out.strip() == "")

        rc, out = run("session", {"cwd": repo},
                      {"MEMHUB_RULEBOOK": os.path.join(td, "missing.json")})
        check("missing rulebook is silent exit-0", rc == 0 and out.strip() == "")

        bad = os.path.join(td, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        rc, out = run("pre", {"cwd": repo, "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}},
                      {"MEMHUB_RULEBOOK": bad})
        check("corrupt rulebook is silent exit-0", rc == 0 and out.strip() == "")

        p = subprocess.run([sys.executable, HOOK, "pre"], input="}}garbage",
                           capture_output=True, text=True,
                           env=dict(os.environ, **env), timeout=30)
        check("garbage stdin is silent exit-0",
              p.returncode == 0 and p.stdout.strip() == "")

        # --- session lane --------------------------------------------------
        rc, out = run("session", {"cwd": repo, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: posture rule served in full", "Posture text" in c)
        check("session: active rules -> index line, not full text",
              "3 rules armed" in c and "Bash advisory" not in c, c)

        rc, out = run("session", {"cwd": other, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: repo_scope filters posture + count",
              "Posture text" not in c and "2 rules armed" in c, c)

        # --- pre lane ------------------------------------------------------
        base = {"cwd": repo, "session_id": "s2", "tool_name": "Bash"}
        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: bash rule fires", "[bash-rule]" in ctx(out))

        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: fire_scope=session dedupes the second call", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s3",
                                  tool_input={"command": "forbidden-cmd in allowed-context"}), env)
        check("pre: not_rx exempts", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s4",
                                  tool_input={"command": "cat > f <<'EOF'\nforbidden-cmd\nEOF"}), env)
        check("pre: heredoc body is not matched by default", out.strip() == "")

        rc, out = run("pre", {"cwd": other, "session_id": "s5", "tool_name": "Bash",
                              "tool_input": {"command": "xmem-only-cmd"}}, env)
        check("pre: repo_scope=xmem stays silent in another repo", out.strip() == "")

        # --- post lane -----------------------------------------------------
        rc, out = run("post", dict(base, session_id="s6",
                                   tool_input={"command": "uv run pytest tests/"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: result rule fires on failing output", "[post-rule]" in ctx(out))

        rc, out = run("post", dict(base, session_id="s7",
                                   tool_input={"command": "ls"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: cmd_rx gates the result rule", out.strip() == "")

        # --- ledger --------------------------------------------------------
        ledger = os.path.join(td, "ledger", "fires.jsonl")
        check("ledger written beside the relocated rulebook", os.path.isfile(ledger))
        if os.path.isfile(ledger):
            with open(ledger, encoding="utf-8") as f:
                fired = [json.loads(l)["rules"] for l in f if l.strip()]
            check("ledger rows carry rule ids",
                  any("bash-rule" in r for r in fired) and
                  any("post-rule" in r for r in fired))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook hook checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
