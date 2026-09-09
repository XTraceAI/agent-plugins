"""pr_link_trigger.py end to end, as a subprocess, against a fake server.

The hook is run the way a host runs it — a JSON payload on stdin, JSON or
nothing on stdout — because that is the contract, and an in-process call would
not catch a script that only works when its module is already imported.

The server is a stdlib http.server serving canned `/v1/team/pr-links/check`
replies, reached by pointing the child's MEMHUB_MCP_BASE_URL at it and giving it a
MEMHUB_TOKEN. `$HOME` is a tmpdir per case, so the negative-answer cache and
the breadcrumb never touch the real one.

Run: python3 tests/pr_link_trigger_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRIGGER = ROOT / "plugins" / "memhub" / "scripts" / "pr_link_trigger.py"

PR = "https://github.com/o/r/pull/7"
CONNECTED = {"enabled": True, "github_connected": True, "repo_in_install": True,
             "connect_url": "https://app.example.test/i",
             "pr": {"known": True, "repo_full_name": "o/r", "pr_number": 7,
                    "state": "open"},
             "linked_sessions": []}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class Fake:
    """Canned `/v1/team/pr-links/check`. ``mode`` ∈ ok | 500 | dead."""

    def __init__(self):
        self.reply = dict(CONNECTED)
        self.mode = "ok"
        self.requests: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                if fake.mode == "500":
                    body = json.dumps({"code": 1, "msg": "boom"}).encode()
                    self.send_response(500)
                else:
                    body = json.dumps({"code": 0, "msg": "ok",
                                       "data": fake.reply}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


FAKE = Fake()


def run(payload: dict, *, home: str, args: tuple = (), token: str = "mhk_test",
        port: int | None = None) -> tuple[int, str]:
    env = {**os.environ,
           "HOME": home, "USERPROFILE": home, "PYTHONUTF8": "1",
           "MEMHUB_TOKEN": token,
           # require_secure exempts loopback; a real host would be https.
           "MEMHUB_MCP_BASE_URL": f"http://127.0.0.1:{port or FAKE.port}"}
    proc = subprocess.run([sys.executable, str(TRIGGER), *args],
                          input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=30)
    return proc.returncode, proc.stdout


def context(out: str) -> str:
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def payload(command: str | None = None, *, tool="Bash", stdout=PR, session="s1",
            tool_input=None, response=None):
    body = {"session_id": session, "tool_name": tool,
            "tool_input": tool_input if tool_input is not None
            else ({"command": command} if command is not None else {})}
    body["tool_response"] = response if response is not None else {"stdout": stdout,
                                                                   "stderr": ""}
    return body


def _home():
    return tempfile.mkdtemp(prefix="memhub-prlink-hook-")


def test_an_http_post_to_the_pulls_collection_is_B2_not_B1():
    """B1 is `gh pr create` and the MCP create tool only.

    Recognising a POST across curl/wget/httpie/xh meant parsing four option
    grammars to find a method and a body, and getting it wrong makes an
    authorship claim nobody can withdraw. Over 15,134 real tool calls that
    layer decided nothing: every real creation was a `gh pr create`. So this
    still reaches the hook — it addresses GitHub and names one pull request —
    but it lands in B2, where the model judges whether it wrote the code.
    """
    FAKE.reply = dict(CONNECTED)
    body = payload("curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
                   response={"stdout": json.dumps({
                       "html_url": PR,
                       "issue_url": "https://api.github.com/repos/o/r/issues/7"}),
                       "stderr": ""})
    rc, out = run(body, home=_home())
    ctx = context(out)
    check("a curl POST still reaches the hook", rc == 0 and PR in ctx, out)
    # B1 and B2 both name `link_source="session_self"` — B2 only inside its
    # conditional. What separates them is the opener and the conditional
    # itself, so assert on those rather than on a substring both share.
    check("…but never claims this session opened it",
          not ctx.startswith("MemHub: you just opened"), ctx)
    check("…it asks the model to judge instead",
          ctx.startswith("MemHub: a pull request is in play")
          and "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" in ctx
          and "IF IT WAS NOT" in ctx, ctx)


def test_a_create_gets_the_unconditional_self_link():
    FAKE.reply = dict(CONNECTED)
    for label, body in (
        ("gh pr create", payload("gh pr create --fill")),
        ("a chained create", payload("cd .. && gh pr create")),
        ("a GitHub MCP create tool",
         payload(tool="mcp__github__create_pull_request",
                 tool_input={"title": "x", "head": "f", "base": "main"},
                 response={"content": [{"type": "text",
                                        "text": json.dumps({"html_url": PR})}]})),
    ):
        rc, out = run(body, home=_home())
        ctx = context(out)
        check(f"{label} → B1", rc == 0 and "without asking" in ctx
              and 'session_ids=["s1"]' in ctx
              and 'link_source="session_self"' in ctx, out)
        check(f"{label} → B1 carries no authorship conditional",
              "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" not in ctx,
              ctx)
        check(f"{label} → names the PR", PR in ctx, ctx)


def test_any_other_github_call_leaves_the_judgment_to_the_model():
    FAKE.reply = dict(CONNECTED)
    for label, body in (
        ("gh pr view", payload("gh pr view 7")),
        ("a GitHub MCP read tool",
         payload(tool="mcp__github__get_pull_request", tool_input={"number": 7},
                 response={"content": [{"type": "text",
                                        "text": json.dumps({"html_url": PR})}]})),
    ):
        rc, out = run(body, home=_home())
        ctx = context(out)
        check(f"{label} → B2", rc == 0
              and "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" in ctx
              and "IF IT WAS NOT" in ctx, out)
        check(f"{label} → no unconditional link instruction",
              "without asking" not in ctx or "Do it without asking" in ctx, ctx)
        check(f"{label} → keeps a babysit loop quiet",
              ctx.rstrip().endswith("say nothing at all."), ctx)


def test_the_gate_is_acting_on_github_not_mentioning_a_pr():
    FAKE.reply = dict(CONNECTED)
    before = len(FAKE.requests)
    rc, out = run(payload("cat CHANGELOG.md"), home=_home())
    check("a `cat` whose output names a PR emits nothing",
          rc == 0 and out.strip() == "", out)
    check("…and asks the server nothing", len(FAKE.requests) == before)


def test_the_quiet_cases_are_quiet():
    FAKE.reply = dict(CONNECTED)
    quiet = [
        ("a non-GitHub command", payload("npm test")),
        ("a `gh pr list` with several URLs",
         payload("gh pr list", stdout="\n".join(
             f"https://github.com/o/r/pull/{n}" for n in (1, 2, 3)))),
        ("a failed `gh pr create`",
         payload("gh pr create", response={"stdout": "", "stderr": "error: no commits"})),
        ("a `gh pr checkout` that printed only a branch",
         payload("gh pr checkout 7", stdout="Switched to branch 'feat/x'")),
    ]
    for label, body in quiet:
        rc, out = run(body, home=_home())
        check(f"{label} → empty stdout, exit 0", rc == 0 and out.strip() == "", out)


def test_the_org_level_answers():
    home = _home()
    FAKE.reply = {"enabled": True, "github_connected": False,
                  "connect_url": "https://app.example.test/i"}
    rc, out = run(payload("gh pr view 7"), home=home)
    ctx = context(out)
    check("github_connected:false → advisory A with the connect URL",
          rc == 0 and "https://app.example.test/i" in ctx
          and "ONCE per session" in ctx, out)
    check("…and never tells the agent to link", "link_pr" not in ctx, ctx)

    before = len(FAKE.requests)
    rc, out = run(payload("gh pr view 7"), home=home)
    check("…and the negative answer is cached, so the second call asks nothing",
          rc == 0 and len(FAKE.requests) == before and context(out) != "", out)

    FAKE.reply = {"enabled": True, "github_connected": True, "repo_in_install": False,
                  "connect_url": "https://app.example.test/i",
                  "pr": {"repo_full_name": "o/r", "pr_number": 7}}
    rc, out = run(payload("gh pr view 7"), home=_home())
    check("repo_in_install:false → the repo variant of A",
          rc == 0 and "o/r isn't part of the install" in context(out), out)

    FAKE.reply = {"enabled": False}
    rc, out = run(payload("gh pr view 7"), home=_home())
    check("enabled:false → total silence, exit 0", rc == 0 and out.strip() == "", out)


def test_an_unreachable_server_is_silence_with_a_breadcrumb():
    FAKE.mode = "500"
    home = _home()
    try:
        rc, out = run(payload("gh pr view 7"), home=home)
    finally:
        FAKE.mode = "ok"
    check("a 500 → empty stdout, exit 0", rc == 0 and out.strip() == "", out)
    crumb = Path(home) / ".config" / "memhub-plugin" / "prlink" / "breadcrumb"
    check("…and a breadcrumb says why this machine went quiet",
          crumb.exists() and "check" in crumb.read_text(encoding="utf-8"),
          crumb.read_text(encoding="utf-8") if crumb.exists() else "no file")

    # Nothing listening at all: the connection is refused, not merely slow.
    home = _home()
    rc, out = run(payload("gh pr view 7"), home=home, port=1)
    check("a dead server → empty stdout, exit 0", rc == 0 and out.strip() == "", out)


def test_no_credential_is_silence():
    rc, out = run(payload("gh pr view 7"), home=_home(), token="")
    check("no bearer → empty stdout, exit 0", rc == 0 and out.strip() == "", out)


def test_malformed_input_never_produces_a_traceback():
    env = {**os.environ, "HOME": _home(), "PYTHONUTF8": "1", "MEMHUB_TOKEN": "",
           "MEMHUB_MCP_BASE_URL": f"http://127.0.0.1:{FAKE.port}"}
    for label, raw in (("not JSON", "{ not json"), ("a list", "[1,2]"),
                       ("empty", ""), ("a bare string", '"hi"')):
        proc = subprocess.run([sys.executable, str(TRIGGER)], input=raw,
                              capture_output=True, text=True, env=env, timeout=30)
        check(f"{label} → exit 0, no output, no traceback",
              proc.returncode == 0 and proc.stdout.strip() == ""
              and "Traceback" not in proc.stderr,
              proc.stdout + proc.stderr)


def test_the_host_flag_namespaces_the_session_id():
    FAKE.reply = dict(CONNECTED)
    for host, want in (("codex", '["codex-s1"]'), ("cursor", '["cursor-s1"]'),
                       ("claude", '["s1"]')):
        rc, out = run(payload("gh pr create"), home=_home(),
                      args=("--host", host))
        check(f"--host {host} → {want}", rc == 0 and want in context(out), out)
    rc, out = run(payload("gh pr create"), home=_home(), args=("--host", "zed"))
    check("an unknown --host falls back to the bare id",
          rc == 0 and '["s1"]' in context(out), out)


def test_the_hook_is_stateless():
    FAKE.reply = dict(CONNECTED)
    home = _home()
    body = payload("gh pr view 7")
    first = context(run(body, home=home)[1])
    second = context(run(body, home=home)[1])
    check("the same payload twice produces the same output",
          first == second and first != "", first[:80])


if __name__ == "__main__":
    print("pr_link_trigger")
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                print(f"\n{name}")
                fn()
    finally:
        FAKE.srv.shutdown()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all pr_link_trigger checks passed")
    sys.exit(1 if failures else 0)
