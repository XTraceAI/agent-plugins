#!/usr/bin/env python3
"""Exercise the selected package against a synthetic 426, never change prod policy."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

from release_check_lib import Report, isolated_env, require, run


class PolicyServer:
    def __init__(self, minimum_version="999.0.0"):
        # The contract test keeps the constant; the real-agent session passes a
        # per-run nonce so the only way an answer can contain the version is
        # by having read the notice.
        self.reject = False
        self.requests = []
        self.minimum_version = minimum_version
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                status = 426 if owner.reject else 200
                if owner.reject:
                    body = {"code": 426, "msg": "PLUGIN_UPGRADE_REQUIRED: Update MemHub and restart your agent session.",
                            "data": {"error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": owner.minimum_version,
                                     "current_version": "0.0.0", "policy_revision": "rulebook-v1:" + owner.minimum_version,
                                     "scope": "rulebook_fetch", "retryable": False}}
                else:
                    body = {"code": 0, "data": {"rules": [{
                        "rule_id": "11111111-1111-4111-8111-111111111111",
                        "title": "Synthetic cached gate", "statement": "Synthetic cached gate",
                        "delivery": "agent_hook", "mode": "gate", "version": 1,
                        "scope_repos": ["memhub-release-upgrade"],
                        "matcher": {"event": "bash", "command_rx": "memhub-synthetic-command"}}]}}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("ETag", '"synthetic-policy"')
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def exercise(package, report):
    with tempfile.TemporaryDirectory(prefix="memhub-policy-") as raw:
        root = Path(raw)
        env = isolated_env(root)
        env.update(MEMHUB_TOKEN="synthetic-release-test-token", MEMHUB_RULEBOOK_RECALL="0")
        workspace = root / "memhub-release-upgrade"
        workspace.mkdir()
        run(["git", "init", "-q", str(workspace)], env=env, cwd=root)
        run(["git", "-C", str(workspace), "remote", "add", "origin",
             "https://github.com/XTraceAI/memhub-release-upgrade.git"], env=env, cwd=root)
        payload = json.dumps({"cwd": str(workspace), "session_id": "release-policy-test",
                              "tool_name": "Bash", "tool_input": {"command": "memhub-synthetic-command"}})
        hook = [sys.executable, str(package / "scripts/rulebook_hook.py")]
        server = PolicyServer()
        env["MEMHUB_MCP_BASE_URL"] = server.url + "/mcp-server/mcp"
        try:
            def check():
                run(hook + ["fetch", "memhub-release-upgrade"], env=env, cwd=workspace)
                before = json.loads(run(hook + ["pre"], env=env, cwd=workspace, stdin=payload))
                require(before.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                        "synthetic gate did not block before version rejection")
                server.reject = True
                run(hook + ["fetch", "memhub-release-upgrade"], env=env, cwd=workspace)
                # Use a fresh session so per-session gate dedup cannot fake suspension.
                rejected_payload = payload.replace("release-policy-test", "release-policy-rejected")
                after = json.loads(run(hook + ["pre"], env=env, cwd=workspace, stdin=rejected_payload) or "{}")
                output = after.get("hookSpecificOutput", {})
                context = output.get("additionalContext", "")
                require("PLUGIN_UPGRADE_REQUIRED" in context and "999.0.0" in context
                        and "restart" in context.lower(),
                        "426 did not produce an actionable agent-visible upgrade notice")
                require(output.get("permissionDecision") != "deny", "unsupported cached rules still block commands")
                require(len(server.requests) >= 2 and all("hook_version=" in p for p in server.requests),
                        "the package did not report its version to the server")
                server.reject = False
                run(hook + ["fetch", "memhub-release-upgrade"], env=env, cwd=workspace)
                recovered = json.loads(run(hook + ["pre"], env=env, cwd=workspace,
                                          stdin=payload.replace("release-policy-test", "release-policy-recovered")) or "{}")
                require(recovered.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                        "rules did not recover after the compatibility policy was rolled back")
            report.check("upgrade_notice_and_cache_recovery", check)
        finally:
            server.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plugin-root", required=True, type=Path)
    ap.add_argument("--source-sha", required=True)
    ap.add_argument("--report", required=True, type=Path)
    args = ap.parse_args()
    report = Report("hook-contract", args.plugin_root.resolve(), args.source_sha)
    exercise(args.plugin_root.resolve(), report)
    return report.finish(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
