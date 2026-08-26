"""Self-test for the rulebook hook's three delivery lanes.

Covers the properties that make this safe to ship in everyone's harness:

* every failure path is SILENT and exit-0 — a broken hook must never touch a
  tool call or a session (missing rulebook, corrupt JSON, garbage stdin,
  non-git cwd);
* the session lane serves posture rules in full and everything else as one
  index line — never the whole book;
* the pre lane matches the shell-only segment (heredoc bodies stripped,
  shell after terminators kept), honors not_rx, and dedupes per
  fire_scope=session (the habituation guard);
* `shell_only` + `evaluate()` are pure and importable — the backtest replays
  them, so what is tested is what runs;
* ordering rules arm on edits, discharge on a GREEN receipt only, gate the
  push, and keep state per (worktree, branch) — shared by sibling sessions and
  subagents, never leaking across branches;
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

        # --- shell_only + evaluate(): the pure engine the backtest imports ---
        sys.path.insert(0, os.path.dirname(HOOK))
        import rulebook_hook as H  # noqa: E402
        so = H.shell_only
        check("shell_only: plain command unchanged", so("git push origin main") == "git push origin main")
        check("shell_only: heredoc body dropped",
              "forbidden" not in so("cat > f <<'EOF'\nforbidden\nEOF"))
        chained = "git commit -F - <<'MSG'\nfix: forbidden\nMSG\ngit push -u origin fm"
        check("shell_only: shell AFTER a heredoc terminator is kept (the 44% FN class)",
              "git push -u origin fm" in so(chained) and "fix: forbidden" not in so(chained))
        check("shell_only: unquoted and <<- delimiters", "secret" not in so("cat <<-EOF\nsecret\nEOF\nls"))
        push = {"on": "bash", "rx": r"git\s+push"}
        check("evaluate: push after heredoc fires",
              H.evaluate(push, hook_phase="pre", tool="Bash", cmd=chained))
        check("evaluate: push only inside a body does not fire",
              not H.evaluate(push, hook_phase="pre", tool="Bash",
                             cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        check("evaluate: match_heredoc_body opts in",
              H.evaluate(dict(push, match_heredoc_body=True), hook_phase="pre",
                         tool="Bash", cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        check("evaluate: broken regex is False, never raises",
              H.evaluate({"on": "bash", "rx": "("}, hook_phase="pre", tool="Bash", cmd="x") is False)

        # --- ordering engine: arm / receipt / gate, keyed by worktree ---------
        obook = os.path.join(td, "ordering.json")
        with open(obook, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rules": [
                {"id": "audit-before-push", "on": "ordering", "repo_scope": "any",
                 "ordering": {"required_command_rx": r"pytest\s+\S*tests/architecture",
                              "gated_command_rx": r"git\s+push",
                              "armed_by_events": ["edit", "write"],
                              "min_edits": 1, "display_name": "the architecture suite"},
                 "text": "Run the architecture suite before pushing", "why": "w"}]}, f)
        oenv = {"MEMHUB_RULEBOOK": obook}
        wt = os.path.join(td, "wtrepo")
        os.makedirs(os.path.join(wt, ".git"))
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/feat\n")
        edit_ev = {"cwd": wt, "session_id": "o1", "tool_name": "Edit",
                   "tool_input": {"file_path": os.path.join(wt, "pkg", "gate.py")}}
        suite = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                 "tool_input": {"command": "uv run pytest tests/architecture -q"}}
        pushev = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                  "tool_input": {"command": "git push -u origin feat"}}

        rc, out = run("pre", pushev, oenv)
        check("ordering: push with nothing armed → silent", out.strip() == "")
        run("post", edit_ev, oenv)                                   # arm
        rc, out = run("pre", pushev, oenv)
        check("ordering: push while armed → fires and names the file",
              "[audit-before-push]" in ctx(out) and "gate.py" in ctx(out), ctx(out))
        run("post", dict(suite, tool_response={"stdout": "1 failed", "exit_code": 1}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: RED suite run is not a receipt", "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: green run discharges → push allowed", out.strip() == "")
        run("post", edit_ev, oenv)                                   # re-arm
        rc, out = run("pre", dict(pushev, session_id="o2-sibling"), oenv)
        check("ordering: state is per worktree, not per session (sibling sees the arm)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, session_id="subagent-9",
                         tool_response={"stdout": "ok", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a SUBAGENT's green receipt discharges the parent's obligation",
              out.strip() == "")
        run("post", edit_ev, oenv)
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/other\n")
        rc, out = run("pre", pushev, oenv)
        check("ordering: state does not leak across branches", out.strip() == "")
        oledger = os.path.join(td, "ledger", "fires.jsonl")
        with open(oledger, encoding="utf-8") as f:
            outcomes = [json.loads(l).get("outcome") for l in f if l.strip()]
        check("ordering: discharges are logged (the conversion signal)",
              outcomes.count("discharged") == 2, str(outcomes))
        statefiles = [n for n in os.listdir(os.path.join(td, "state")) if n.startswith("wt-") and n.endswith(".json")]
        check("ordering: one state file per worktree, atomic (no temp leftovers)",
              len(statefiles) == 1 and not any(n.startswith(".wt-") for n in os.listdir(os.path.join(td, "state"))))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook hook checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
