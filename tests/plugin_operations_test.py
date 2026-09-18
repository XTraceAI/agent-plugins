"""ENG-1092: all transports, active-version identity, and durable recovery."""
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/memhub/scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_http
import plugin_compatibility as compat
import plugin_version as version


class PluginOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(compat, "STATE_DIR", Path(self.tmp.name) / "status")
        p.start()
        self.addCleanup(p.stop)
        self.url = "https://example.test/mcp-server/mcp"
        self.bearer = "test-only-key"
        self.error = {"code": 426, "msg": "upgrade", "data": {
            "error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": "9.0.0",
            "scope": "plugin_operations", "retryable": False,
        }}

    def test_rest_and_mcp_http_426_persist_block(self):
        for invoke in (lambda: mcp_http.rest(self.url, self.bearer),
                       lambda: mcp_http.request(self.url, self.bearer, "tools/call")):
            with self.subTest(invoke=invoke):
                self.error["msg"] = "x" * 500
                error = HTTPError(self.url, 426, "upgrade", {}, io.BytesIO(json.dumps(self.error).encode()))
                with patch.object(compat, "before_operation"), patch.object(mcp_http, "_opener") as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(mcp_http.PluginUpgradeRequired):
                        invoke()
                    req = opener.return_value.open.call_args.args[0]
                    self.assertEqual(req.get_header("X-memhub-plugin-version"), version.ACTIVE_PLUGIN_VERSION)
                self.assertEqual(compat.status(self.url, self.bearer)["minimum_version"], "9.0.0")

    def test_native_mcp_tool_error_is_typed_not_an_ack(self):
        result = {"isError": True, "content": [{"type": "text", "text":
                  "Error executing tool import_conversation: " + json.dumps(self.error)}]}
        with patch.object(mcp_http, "request", return_value=result):
            with self.assertRaises(mcp_http.PluginUpgradeRequired):
                mcp_http.call_tool(self.url, self.bearer, "import_conversation", {})
        self.assertIsNotNone(compat.status(self.url, self.bearer))

    def test_invalid_policy_cannot_become_upgrade_status(self):
        for value in ({"data": []}, {"data": {"error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": "run shell"}},
                      {"data": {"error_code": "OTHER", "minimum_version": "9.0.0"}}):
            mcp_http._raise_upgrade(value, self.url, self.bearer)
        self.assertIsNone(compat.status(self.url, self.bearer))

    def test_sdk_session_uses_same_upgrade_contract(self):
        result = SimpleNamespace(isError=True, structuredContent=None,
            content=[SimpleNamespace(text="Error executing tool save_artifact: " + json.dumps(self.error))])
        native = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        session = mcp_http.PolicySession(native, self.url, {"Authorization": "Bearer " + self.bearer})
        with self.assertRaises(mcp_http.PluginUpgradeRequired):
            asyncio.run(session.call_tool("save_artifact", arguments={}))
        self.assertIsNotNone(compat.status(self.url, self.bearer))

    def test_capture_rejection_suspends_fresh_rulebook_cache(self):
        import rulebook_hook
        compat.record(self.url, self.bearer, "9.0.0")
        with patch.object(rulebook_hook, "_api", return_value=("https://example.test", self.bearer, mcp_http)):
            self.assertEqual(rulebook_hook.upgrade_status("repo")["minimum_version"], "9.0.0")

    def test_cursor_notice_is_bounded_and_host_specific(self):
        compat.record(self.url, self.bearer, "9.0.0")
        with patch("_memhub_auth.resolve_bearer", return_value=(self.url, self.bearer)), patch.object(compat, "check") as check:
            message = compat.startup_message(host="cursor", session="session")
            self.assertIn("/plugin/cursor", message)
            self.assertIsNone(compat.startup_message(host="cursor", session="session"))
            check.assert_not_called()

    def test_repeated_calls_do_not_retry_unsupported_requests(self):
        compat.record(self.url, self.bearer, "9.0.0")
        with patch.object(mcp_http, "_opener") as opener:
            for _ in range(5):
                with self.assertRaises(mcp_http.PluginUpgradeRequired):
                    mcp_http.request(self.url, self.bearer, "tools/call")
            opener.assert_not_called()
        self.assertIsNone(compat.status(self.url, "different-key"))
        self.assertIsNone(compat.status("https://other.test/mcp", self.bearer))

    def test_no_op_update_and_unavailable_release_preserve_pending_status(self):
        compat.record(self.url, self.bearer, "9.0.0")
        data = {"operations_enforced": True, "supported": False, "minimum_version": "9.0.0",
                "current_version": version.ACTIVE_PLUGIN_VERSION}
        with patch.object(mcp_http, "rest", return_value=SimpleNamespace(data=data)), patch.object(mcp_http, "call_tool") as read:
            self.assertIsNotNone(compat.check(self.url, self.bearer))
            read.assert_not_called()

    def test_recovery_requires_current_code_and_successful_protected_read(self):
        compat.record(self.url, self.bearer, "0.1.0")
        data = {"operations_enforced": True, "supported": True, "minimum_version": "0.1.0",
                "current_version": version.ACTIVE_PLUGIN_VERSION}
        with patch.object(mcp_http, "rest", return_value=SimpleNamespace(data=data)), patch.object(mcp_http, "call_tool") as read:
            read.return_value = SimpleNamespace(isError=True)
            self.assertIsNotNone(compat.check(self.url, self.bearer))
            read.return_value = SimpleNamespace(isError=False)
            self.assertIsNone(compat.check(self.url, self.bearer))
            self.assertEqual(read.call_args.args[2], "list_orgs")
        self.assertIsNone(compat.status(self.url, self.bearer))

    def test_failed_update_or_backend_outage_keeps_status(self):
        compat.record(self.url, self.bearer, "9.0.0")
        with patch.object(mcp_http, "rest", side_effect=mcp_http.McpError("offline")):
            self.assertIsNotNone(compat.check(self.url, self.bearer))

    def test_downloaded_manifest_does_not_change_loaded_version(self):
        root = Path(self.tmp.name) / "plugin"
        (root / "scripts").mkdir(parents=True)
        (root / ".claude-plugin").mkdir()
        for filename in ("plugin_version.py", "_memhub_auth.py"):
            shutil.copy(SCRIPTS / filename, root / "scripts" / filename)
        manifest = root / ".claude-plugin/plugin.json"
        manifest.write_text('{"version":"0.1.0"}')
        code = ("import plugin_version as v; from pathlib import Path; "
                "p=Path(__import__('sys').argv[1]); "
                "p.write_text('{\"version\":\"9.0.0\"}'); "
                "assert v.request_headers()['X-MemHub-Plugin-Version']=='0.1.0'")
        env = dict(os.environ, PYTHONPATH=str(root / "scripts"), CLAUDE_PLUGIN_ROOT=str(root))
        subprocess.run([sys.executable, "-c", code, str(manifest)], env=env, check=True)

    def test_capture_watermark_does_not_advance_on_upgrade(self):
        import flush_turn as ft
        transcript = Path(self.tmp.name) / "session.jsonl"
        transcript.write_text(json.dumps({"type": "user", "uuid": "u1", "cwd": "/repo",
            "message": {"role": "user", "content": "Preserve this pending work"}}) + "\n")
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=mcp_http.PluginUpgradeRequired("9.0.0")))
        with patch.object(ft, "STATE_DIR", Path(self.tmp.name) / "capture"), \
             patch.object(ft, "resolve_bearer", return_value=(self.url, self.bearer)), \
             patch.object(ft, "resolve_repo_brain", AsyncMock(return_value=None)), \
             patch.object(ft, "_namespace", return_value=("/repo", "repo")), \
             patch.object(ft.mcp_http, "Session", return_value=session):
            asyncio.run(ft._flush("pending-session", str(transcript)))
            state = ft._read_state("pending-session")
            self.assertFalse(state.get("offset"))
            self.assertNotIn("last_ok_at", state)
            self.assertEqual(state.get("last_error"), "upgrade_required")


if __name__ == "__main__":
    unittest.main()
