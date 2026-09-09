"""pr_post_context.py — ONE additional context per GitHub-addressing call.

Two PostToolUse groups that each return `hookSpecificOutput.additionalContext`
do not both reach the model: the earlier registration wins and the later one is
dropped with no error anywhere. `gh pr create` was the only command where both
PR-lane hooks fired, so the instruction that lost was the B1 self-link on the
one call that links unconditionally — while the babysit instruction that won
sent the model into a loop where it never revisited the question.

So the shape under test is not "does each lane still work" (their own suites
cover that) but: exactly ONE document comes out, it carries BOTH instructions
with the LINK first, a lane that fails loses only its own voice, and the
manifest reaches the lanes through nothing but this entry point.

Run as a subprocess, like the other hook tests, against a stdlib fake serving
canned `/v1/team/pr-links/check` replies; `$HOME` is a tmpdir per case so the
negative-answer cache and the breadcrumb never touch the real one. The
lane-isolation cases run in-process, because a lane that raises is something
only a monkeypatch can arrange.

Run: python3 tests/pr_post_context_test.py   (stdlib only)
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
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
ENTRY = SCRIPTS / "pr_post_context.py"
HOOKS = ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json"

PR = "https://github.com/o/r/pull/7"
CONNECTED = {"enabled": True, "github_connected": True, "repo_in_install": True,
             "connect_url": "https://app.example.test/i",
             "pr": {"known": True, "repo_full_name": "o/r", "pr_number": 7,
                    "state": "open"},
             "linked_sessions": []}
# The sentence each lane opens with — enough to tell them apart in one string,
# and short enough that rewording the instruction does not break the test.
LINK_OPENER = "MemHub: you just opened"
BABYSIT_OPENER = "A pull request was just created"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class Fake:
    """Canned `/v1/team/pr-links/check`."""

    def __init__(self):
        self.reply = dict(CONNECTED)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
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


def run(payload, *, home: str, args: tuple = (),
        token: str = "mhk_test") -> tuple[int, str, str]:
    env = {**os.environ,
           "HOME": home, "USERPROFILE": home, "PYTHONUTF8": "1",
           "MEMHUB_TOKEN": token,
           # require_secure exempts loopback; a real host would be https.
           "MEMHUB_MCP_BASE_URL": f"http://127.0.0.1:{FAKE.port}"}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run([sys.executable, str(ENTRY), *args],
                          input=text, capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr


def one_document(out: str) -> str:
    """The single additionalContext, asserting the output IS a single document."""
    if not out.strip():
        return ""
    parsed = json.loads(out)            # raises if two documents were printed
    assert list(parsed) == ["hookSpecificOutput"], parsed
    inner = parsed["hookSpecificOutput"]
    assert inner["hookEventName"] == "PostToolUse", inner
    return inner["additionalContext"]


def payload(command: str | None = None, *, tool="Bash", stdout=PR, session="s1",
            tool_input=None, response=None):
    body = {"session_id": session, "tool_name": tool,
            "tool_input": tool_input if tool_input is not None
            else ({"command": command} if command is not None else {})}
    body["tool_response"] = response if response is not None else {"stdout": stdout,
                                                                   "stderr": ""}
    return body


def _home():
    return tempfile.mkdtemp(prefix="memhub-prctx-")


def _module():
    """pr_post_context, imported with the plugin scripts on the path."""
    sys.path.insert(0, str(SCRIPTS))
    import pr_post_context
    return pr_post_context


def test_a_create_carries_both_instructions_with_the_link_first():
    """The regression. Before the merge, only the babysit half arrived."""
    FAKE.reply = dict(CONNECTED)
    rc, out, _ = run(payload("gh pr create --fill"), home=_home())
    ctx = one_document(out)
    check("exit 0", rc == 0, str(rc))
    check("the link instruction is there", LINK_OPENER in ctx, ctx[:160])
    check("the babysit instruction is there", BABYSIT_OPENER in ctx, ctx[:160])
    check("…and the LINK comes first, before the loop is armed",
          -1 < ctx.find(LINK_OPENER) < ctx.find(BABYSIT_OPENER), ctx[:160])
    check("the two are separated by a blank line", "\n\n" in ctx)
    check("…and the PR is named", PR in ctx, ctx)


def test_a_read_yields_the_link_alone():
    FAKE.reply = dict(CONNECTED)
    rc, out, _ = run(payload("gh pr view 7 --json url -q .url"), home=_home())
    ctx = one_document(out)
    check("exit 0", rc == 0, str(rc))
    check("the link lane speaks", PR in ctx and BABYSIT_OPENER not in ctx, ctx[:160])


def test_a_github_mcp_create_never_arms_a_babysit_loop():
    """The merged matcher covers MCP tools; the babysit lane must not.

    Widening the registration from `Bash` to
    `^(Bash|mcp__.*[Gg]it[Hh]ub.*__.*)$` must not widen what arms a loop —
    that is a behaviour change, and this change is a bug fix.
    """
    FAKE.reply = dict(CONNECTED)
    rc, out, _ = run(payload(
        tool="mcp__github__create_pull_request",
        tool_input={"title": "x", "head": "f", "base": "main"},
        response={"content": [{"type": "text",
                               "text": json.dumps({"html_url": PR})}]}),
        home=_home())
    ctx = one_document(out)
    check("the link lane still self-links an MCP create",
          rc == 0 and LINK_OPENER in ctx, ctx[:160])
    check("…and no babysit loop is armed", BABYSIT_OPENER not in ctx, ctx[:160])


def test_a_failing_lane_loses_only_its_own_voice():
    """A raising lane must not take the other lane's instruction with it."""
    mod = _module()

    def boom(*_a, **_k):
        raise RuntimeError("lane exploded")

    link, babysit = mod.pr_link_trigger.context_for, mod.pr_babysit_trigger.context_for
    body = payload("gh pr create --fill")
    try:
        mod.pr_link_trigger.context_for = boom
        mod.pr_babysit_trigger.context_for = lambda _p: "BABYSIT"
        check("a raising link lane still lets babysit speak",
              mod.contexts_for(body) == ["BABYSIT"])

        mod.pr_link_trigger.context_for = lambda _p, **_k: "LINK"
        mod.pr_babysit_trigger.context_for = boom
        check("a raising babysit lane still lets link speak",
              mod.contexts_for(body) == ["LINK"])

        mod.pr_link_trigger.context_for = boom
        mod.pr_babysit_trigger.context_for = boom
        check("both raising is silence, not a traceback",
              mod.contexts_for(body) == [])
    finally:
        mod.pr_link_trigger.context_for = link
        mod.pr_babysit_trigger.context_for = babysit


def test_silence_is_total():
    """Nothing on stdout — not an empty document, which would still be context."""
    FAKE.reply = dict(CONNECTED)
    for label, body in (
        ("an ordinary command", payload("ls -la")),
        ("a create that returned no URL",
         payload("gh pr create --fill", response={"stdout": "", "stderr": "boom"})),
        ("a tool with no input at all", {"session_id": "s1", "tool_name": "Bash"}),
    ):
        rc, out, _ = run(body, home=_home())
        check(f"{label} → exit 0 and no stdout", rc == 0 and out.strip() == "",
              repr(out[:120]))


def test_malformed_input_never_produces_a_traceback():
    for label, raw in (("not json", "{nope"), ("a bare list", "[1, 2]"),
                       ("a bare string", '"hello"'), ("empty", "")):
        rc, out, err = run(raw, home=_home())
        check(f"{label} → exit 0, no stdout", rc == 0 and out.strip() == "",
              repr(out[:120]))
        check(f"{label} → no traceback", "Traceback" not in err, err[-200:])


def test_the_manifest_reaches_the_lanes_through_this_entry_point_only():
    """The invariant the fix establishes, asserted against what ships.

    Re-registering either lane as its own PostToolUse group reintroduces the
    bug exactly, and nothing else in the repo would notice.
    """
    post = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    commands = [hook.get("command", "") for group in post
                for hook in group["hooks"]]
    entries = [c for c in commands if "pr_post_context.py" in c]
    check("the PR lane ships as exactly one handler", len(entries) == 1,
          str(len(entries)))
    for lane in ("pr_link_trigger.py", "pr_babysit_trigger.py"):
        check(f"{lane} has no PostToolUse registration of its own",
              not any(lane in c for c in commands))
    group = next(g for g in post
                 if any("pr_post_context.py" in h.get("command", "")
                        for h in g["hooks"]))
    check("the matcher covers Bash and the GitHub MCP tools",
          group["matcher"] == "^(Bash|mcp__.*[Gg]it[Hh]ub.*__.*)$",
          group["matcher"])
    hook = group["hooks"][0]
    check("the budget is the link lane's 15s, not the babysit lane's 30s",
          hook.get("timeout") == 15, str(hook.get("timeout")))
    check("it is synchronous — additionalContext from an async hook is not delivered",
          not hook.get("async"))
    check("the guard still runs first", "claude_hook_guard.py" in hook["command"])
    # Index 3, not 6: while the harness delivers the earliest context, a
    # rulebook fire at [5] must not be able to displace the PR context.
    check("…and it sits ahead of the rulebook post handler",
          post.index(group) < next(i for i, g in enumerate(post)
                                   if "rulebook_hook.py" in g["hooks"][0]["command"]))


if __name__ == "__main__":
    print("pr_post_context")
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
        print("all pr_post_context checks passed")
    sys.exit(1 if failures else 0)
