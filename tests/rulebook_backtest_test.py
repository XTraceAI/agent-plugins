"""Self-test for the rulebook backtest — the arming gate for every rule.

Covers what the skill relies on:

* `--rule` takes the SAME JSON the agent sends to `create_rule` (a full call
  body with `matcher`, or a bare matcher dict) and converts it through the
  hook's own to_hook_rule — so what is backtested is what the server stores;
* hook-shaped rows (`on`/`rx`/…) still pass through;
* repo scope honours `scope_repos` for ANY repo name, not a literal "xmem";
* a server `write` rule (and hook on=write) replays against Write calls;
* a broken regex in ANY hook regex key fails fast, never reads as zero-fire;
* the output carries a ready-to-paste `backtest` block for create_rule.

Run: python3 rulebook_backtest_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "plugins", "memhub", "scripts")
BT = os.path.join(SCRIPTS, "rulebook_backtest.py")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def tool_use(cwd, name, inp, ts="2026-08-20T10:00:00Z"):
    return json.dumps({"type": "assistant", "cwd": cwd, "timestamp": ts,
                       "message": {"content": [{"type": "tool_use", "id": "t1",
                                                "name": name, "input": inp}]}})


def write_session(projects, sid, lines):
    d = os.path.join(projects, "-w-proj")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run(projects, rule, extra=()):
    p = subprocess.run([sys.executable, BT, "--projects", projects, "--days", "3650",
                        "--rule", json.dumps(rule), *extra],
                       capture_output=True, text=True, timeout=60)
    out = p.stdout
    summary = None
    if p.returncode == 0:
        # two JSON documents on stdout: the summary, then the backtest block
        dec = json.JSONDecoder()
        idx = out.index("{")
        summary, end = dec.raw_decode(out, idx)
        block, _ = dec.raw_decode(out, out.index("{", end))
        return p.returncode, summary, block, p.stderr
    return p.returncode, None, None, p.stderr + out


def main() -> int:
    sys.path.insert(0, SCRIPTS)
    import rulebook_hook as H  # noqa: E402

    # --- #9: to_hook_rule maps a server write rule onto the edit family -----
    w = H.to_hook_rule({"rule_id": "w1", "statement": "s",
                        "matcher": {"event": "write", "path_rx": r"\.py$"}})
    check("to_hook_rule: event=write → on=edit (EDIT_TOOLS covers Write)",
          w is not None and w["on"] == "edit" and w["path_rx"] == r"\.py$", str(w))
    check("evaluate: a server write rule fires on a Write call",
          bool(H.evaluate(w, hook_phase="pre", tool="Write", file_path="/r/a.py", body="x")))

    with tempfile.TemporaryDirectory() as td:
        projects = os.path.join(td, "projects")
        write_session(projects, "alpha-session", [
            tool_use("/w/alpha", "Bash", {"command": "git push origin main"}),
            tool_use("/w/alpha", "Write", {"file_path": "/w/alpha/pkg/mod.py", "content": "import os\n"}),
        ])
        write_session(projects, "beta-session", [
            tool_use("/w/beta", "Bash", {"command": "git push --force origin main"}),
            tool_use("/w/beta", "Edit", {"file_path": "/w/beta/README.md", "new_string": "hi"}),
        ])

        # --- #4: bare matcher dict, exactly what create_rule takes ---------
        rc, s, block, err = run(projects, {"event": "bash", "command_rx": r"git\s+push"})
        check("--rule accepts a bare server matcher dict",
              rc == 0 and s and s["sessions_with_hit"] == 2 and s["raw_hits"] == 2, err[-400:])
        # --- #3: the ready-to-paste backtest block ---------------------------
        check("output carries a create_rule backtest block (sessions/hits/days/rule_version, judged_* = 0)",
              bool(block) and block["backtest"]["sessions"] == 2 and block["backtest"]["hits"] == 2
              and block["backtest"]["judged_tp"] == 0
              and block["backtest"]["judged_fp"] == 0 and block["backtest"]["days"] == 3650, str(block))

        # --- #4: full create_rule call body, with scope_repos ---------------
        rc, s, _, err = run(projects, {"title": "no force push", "delivery": "agent_hook",
                                       "matcher": {"event": "bash", "command_rx": r"git\s+push",
                                                   "command_not_rx": r"--force"},
                                       "scope_repos": []})
        check("--rule accepts the full create_rule body; command_not_rx honoured",
              rc == 0 and s and s["sessions_with_hit"] == 1, err[-400:])

        # --- #8: scope_repos with an arbitrary repo name -------------------
        rc, s, _, err = run(projects, {"delivery": "agent_hook",
                                       "matcher": {"event": "bash", "command_rx": r"git\s+push"},
                                       "scope_repos": ["beta"]})
        check("scope_repos=[beta] scans only beta sessions (generic, not literal xmem)",
              rc == 0 and s and s["sessions_with_hit"] == 1
              and s["excerpts"][0]["repo"] == "beta", err[-400:])
        rc, s, _, err = run(projects, {"delivery": "agent_hook",
                                       "matcher": {"event": "bash", "command_rx": r"git\s+push"},
                                       "scope_repos": ["gamma"]})
        check("scope_repos naming no scanned repo → zero fires",
              rc == 0 and s and s["raw_hits"] == 0, err[-400:])

        # --- #9: write event, server shape AND hook shape --------------------
        rc, s, _, err = run(projects, {"event": "write", "path_rx": r"\.py$"})
        check("server event=write replays against Write calls",
              rc == 0 and s and s["raw_hits"] == 1 and s["excerpts"][0]["repo"] == "alpha", err[-400:])
        rc, s, _, err = run(projects, {"id": "hw", "on": "write", "path_rx": r"\.py$", "text": "t"})
        check("hook-shaped on=write is accepted and replays the same",
              rc == 0 and s and s["raw_hits"] == 1, err[-400:])

        # --- hook shape still passes through ---------------------------------
        rc, s, _, err = run(projects, {"id": "hb", "on": "bash", "rx": r"--force", "text": "t"})
        check("hook-shaped on=bash still works", rc == 0 and s and s["raw_hits"] == 1, err[-400:])

        # --- #10: broken regex in a non-primary key fails fast ---------------
        rc, s, _, err = run(projects, {"id": "bad", "on": "bash", "rx": "git push",
                                       "body_rx": "(unclosed", "text": "t"})
        check("a broken body_rx exits non-zero instead of reading as zero-fire",
              rc != 0 and "body_rx" in err, err[-400:])
        rc, s, _, err = run(projects, {"id": "bad2", "on": "result", "rx": "FAILED",
                                       "exclude_rx": "[", "text": "t"})
        check("a broken exclude_rx exits non-zero", rc != 0 and "exclude_rx" in err, err[-400:])
        rc, s, _, err = run(projects, {"event": "bash", "command_rx": "(a+)+$"})
        check("a server matcher the hook's loader would drop is refused, not zero-fired",
              rc != 0 and "to_hook_rule" in err, err[-400:])

        # --- unsupported shapes are refused with a pointer -----------------
        rc, s, _, err = run(projects, {"delivery": "session_context", "title": "x"})
        check("no matcher / no on → refused", rc != 0, err[-400:])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook backtest checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
