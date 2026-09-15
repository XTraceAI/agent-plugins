#!/usr/bin/env python3
"""Prove the pinned host CLI runs the package's SessionStart hook and receives
the upgrade notice. No model, no credentials, no environment gate.

What the real-agent session (check-agent-session.py) asks — "did the model tell
the user?" — depends on what an LLM chooses to echo, and on run 35000491842
that answer flipped on byte-identical packages. The question a release gate
must answer deterministically is the one underneath it: does the host CLI at
its pinned version start the hook and take its output? That is host behaviour,
observable from the host's own records, and it needs no prompt:

* Claude runs SessionStart hooks at startup, before any prompt. A stream-json
  session with stdin held open and nothing sent runs them and reports each
  one as a ``hook_response`` event whose ``output`` is the hook's stdout.
* Codex has no prompt-less mode, so it gets a one-word prompt and a model
  endpoint that cannot answer (the loopback policy server). SessionStart runs
  at session creation; the turn then fails without contacting any provider.
  Codex 0.153's ``--json`` stream does not carry hook events, so the evidence
  is the hook's own per-session record: ``show_upgrade`` writes a
  ``.notice-<session>`` marker only after it emitted the notice, and the
  policy server logs the fetch that produced the 426.

Same disposable home, same native install and same synthetic 426 server as the
other release checks. Only booleans and counts are reported.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from release_check_lib import Report, compat, host_version, isolated_env, json_lines, require, run

for _name, _filename in (("install", "check-host-install.py"), ("policy", "check-upgrade-contract.py")):
    _spec = importlib.util.spec_from_file_location(_name, Path(__file__).with_name(_filename))
    globals()[_name] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(globals()[_name])

REPO = "memhub-release-upgrade"
HOSTS = ("codex", "claude")
STARTUP_BUDGET_S = 45
SHUTDOWN_BUDGET_S = 30


def notice_delivered(text):
    """The three facts the notice must carry, exactly as check-upgrade-contract asserts them."""
    return isinstance(text, str) and "PLUGIN_UPGRADE_REQUIRED" in text \
        and "999.0.0" in text and "restart" in text.lower()


def prepare_workspace(root, env):
    ws = root / REPO
    ws.mkdir()
    run(["git", "init", "-q", str(ws)], env=env, cwd=root)
    run(["git", "-C", str(ws), "remote", "add", "origin", f"https://github.com/XTraceAI/{REPO}.git"], env=env, cwd=root)
    return ws


def prepare_host(root, env, package, host, executable):
    installed = install.native_install(root, env, package, host, executable)
    if host == "codex":
        run([sys.executable, str(installed / "scripts/setup_codex_hooks.py"), "install"], env=env, cwd=root)
    require(compat.package_digest(installed) == compat.package_digest(package), "host package bytes changed")
    return installed


# ── Claude: the host reports each hook's output ────────────────────────────

def claude_events(executable, env, workspace):
    cmd = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--include-hook-events"]
    events = []
    with subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, start_new_session=True) as proc:
        def read():
            for line in proc.stdout:
                events.extend(json_lines(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + STARTUP_BUDGET_S
        while time.monotonic() < deadline and proc.poll() is None:
            snapshot = list(events)
            started = sum(e.get("subtype") == "hook_started" and e.get("hook_event") == "SessionStart" for e in snapshot)
            ended = sum(e.get("subtype") == "hook_response" and e.get("hook_event") == "SessionStart" for e in snapshot)
            if started and ended >= started:
                break
            time.sleep(.25)
        proc.stdin.close()          # no prompt was ever sent; the host exits cleanly
        try:
            proc.wait(timeout=SHUTDOWN_BUDGET_S)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise compat.GateError("Claude did not exit after its SessionStart hooks") from None
        reader.join(timeout=5)
    return events


def claude_evidence(events, server):
    responses = [e for e in events if e.get("subtype") == "hook_response" and e.get("hook_event") == "SessionStart"]
    evidence = {"hook_responses": len(responses),
                "all_succeeded": bool(responses) and all(e.get("outcome") == "success" for e in responses),
                "notice_in_host_record": False,
                "server_fetched": any("hook_version=" in p for p in server.requests)}
    for event in responses:
        try:
            doc = json.loads(event.get("output") or event.get("stdout") or "")
        except (TypeError, ValueError):
            continue
        if notice_delivered(doc.get("hookSpecificOutput", {}).get("additionalContext")):
            evidence["notice_in_host_record"] = True
    return evidence


# ── Codex: the hook's own per-session record ───────────────────────────────

def codex_events(executable, env, workspace, server):
    # A credential that satisfies the login check without being one, and a
    # model endpoint that cannot answer, so the turn fails locally.
    env = dict(env, CODEX_API_KEY="release-check-no-model", OPENAI_BASE_URL=server.url + "/v1")
    cmd = [executable, "exec", "--json", "--sandbox", "read-only", "--dangerously-bypass-hook-trust",
           "-c", "approval_policy=\"never\"", "--model", "gpt-5.3-codex", "-"]
    proc = subprocess.run(cmd, input="release session-start check", cwd=workspace, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          timeout=STARTUP_BUDGET_S + SHUTDOWN_BUDGET_S, check=False)
    return json_lines(proc.stdout)


def codex_evidence(events, root, installed, env, server):
    thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    book = run([sys.executable, str(installed / "scripts/rulebook_hook.py"), "book-path", REPO],
               env=env, cwd=root).strip()
    markers = sorted(Path(book).parent.glob(Path(book).name + ".notice-*")) if book else []
    mine = Path(book + ".notice-" + hashlib.sha256(str(thread).encode()).hexdigest()[:16]) if book and thread else None
    text = json.dumps(events)
    return {"session_started": thread is not None,
            "server_fetched": any("hook_version=" in p for p in server.requests),
            "notice_markers": len(markers),
            "notice_emitted_for_session": bool(mine and mine.is_file()),
            "notice_in_host_stream": notice_delivered(text)}


def exercise(args, report):
    with tempfile.TemporaryDirectory(prefix="memhub-session-start-", ignore_cleanup_errors=True) as raw:
        root = Path(raw)
        env = isolated_env(root)
        report.data["host_version"] = report.check("host_cli", lambda: host_version(args.executable, env, root))
        ws = prepare_workspace(root, env)
        installed = report.check("native_package_load", lambda: prepare_host(root, env, args.plugin_root, args.host, args.executable))
        if installed is None:
            report.blocked(["session_start_notice"], "native plugin setup failed")
            return
        env["MEMHUB_TOKEN"] = "synthetic-release-test-token"
        server = policy.PolicyServer()
        server.reject = True
        env["MEMHUB_MCP_BASE_URL"] = server.url + "/mcp-server/mcp"
        try:
            def check():
                if args.host == "claude":
                    evidence = claude_evidence(claude_events(args.executable, env, ws), server)
                    report.data["session_start_evidence"] = evidence
                    require(evidence["hook_responses"], "Claude ran no SessionStart hook")
                    require(evidence["all_succeeded"], "a SessionStart hook failed or was cancelled")
                    require(evidence["server_fetched"], "SessionStart did not fetch the rulebook with its version")
                    require(evidence["notice_in_host_record"], "Claude did not record the upgrade notice from its SessionStart hook")
                else:
                    evidence = codex_evidence(codex_events(args.executable, env, ws, server), root, installed, env, server)
                    report.data["session_start_evidence"] = evidence
                    require(evidence["session_started"], "Codex did not start a session")
                    require(evidence["server_fetched"], "SessionStart did not fetch the rulebook with its version")
                    require(evidence["notice_emitted_for_session"], "the SessionStart hook did not emit the upgrade notice for this session")
            report.check("session_start_notice", check)
        finally:
            server.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", choices=HOSTS, required=True)
    ap.add_argument("--executable", required=True)
    ap.add_argument("--plugin-root", type=Path, required=True)
    ap.add_argument("--source-sha", required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()
    args.plugin_root = args.plugin_root.resolve()
    report = Report(args.host, args.plugin_root, args.source_sha)
    exercise(args, report)
    return report.finish(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
