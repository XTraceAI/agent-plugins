"""The release harness must reject missing evidence, not turn it into success."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_check_lib as lib


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


agent = module("release_agent_test", "check-agent-session.py")
install = module("release_install_test", "check-host-install.py")
PACKAGE = ROOT / "plugins/memhub"
SHA = "a" * 40
SID = "11111111-1111-4111-8111-111111111111"


class ReleaseChecksTests(unittest.TestCase):
    def test_empty_report_and_negative_return_cannot_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            for negative in (False, True):
                report = lib.Report("codex", PACKAGE, SHA)
                if negative:
                    report.check("negative", lambda: False)
                self.assertEqual(report.finish(Path(raw) / "report.json"), 1)

    def test_missing_evidence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            report = lib.Report("codex", PACKAGE, SHA)
            report.blocked(["advice", "capture"], "not provisioned")
            path = Path(raw) / "report.json"
            self.assertEqual(report.finish(path), 1)
            self.assertFalse(json.loads(path.read_text())["ok"])

    def test_unexpected_exception_never_leaks_details(self):
        with tempfile.TemporaryDirectory() as raw:
            report = lib.Report("codex", PACKAGE, SHA)
            def fail():
                raise ValueError("secret-from-child-output")
            report.check("failure", fail)
            path = Path(raw) / "report.json"
            self.assertEqual(report.finish(path), 1)
            self.assertNotIn("secret-from-child-output", path.read_text())

    def test_isolated_environment_does_not_inherit_credentials(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
            "MEMHUB_TOKEN": "private", "CODEX_API_KEY": "private", "GITHUB_TOKEN": "private",
            "MEMHUB_MCP_BASE_URL": "https://untrusted.example", "PYTHONPATH": "/untrusted"}):
            env = lib.isolated_env(Path(raw))
            for name in ("MEMHUB_TOKEN", "CODEX_API_KEY", "GITHUB_TOKEN", "MEMHUB_MCP_BASE_URL", "PYTHONPATH"):
                self.assertNotIn(name, env)
            self.assertTrue(Path(env["CODEX_HOME"]).is_dir())

    def test_codex_rollout_is_inside_capture_reader_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = lib.isolated_env(root)
            rollout = Path(env["CODEX_HOME"]) / "sessions" / f"rollout-{SID}.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("{}\n")
            outside = root / "outside.jsonl"
            outside.write_text("{}\n")
            code = ("import sys; from pathlib import Path; "
                    f"sys.path.insert(0, {str(PACKAGE / 'scripts')!r}); "
                    "import codex_flush; "
                    f"assert codex_flush._contained(Path({str(rollout)!r})) is not None; "
                    f"assert codex_flush._contained(Path({str(outside)!r})) is None")
            lib.run([sys.executable, "-c", code], env=env, cwd=root)

    def test_cursor_marketplace_reindex_is_not_an_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(lib.NotVerified):
                install.native_install(root, lib.isolated_env(root), PACKAGE, "cursor", "does-not-exist")

    def test_hook_text_alone_is_not_advice_delivered_to_user(self):
        events = [{"type": "system", "subtype": "hook_response", "stdout": "HIDDEN_MARKER"},
                  {"type": "result", "result": "DONE"}]
        self.assertEqual(agent.final_text(events, "cursor"), "DONE")
        with self.assertRaises(lib.compat.GateError):
            agent.final_text(events[:1], "cursor")

    def test_failed_codex_turn_cannot_pass_on_prior_message(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}},
                  {"type": "turn.failed"}]
        with self.assertRaises(lib.compat.GateError):
            agent.final_text(events, "codex")

    def test_claude_final_answer_and_hook_health_are_independent(self):
        events = [{"type": "system", "subtype": "hook_response", "outcome": "error", "exit_code": 1},
                  {"type": "result", "result": "HIDDEN_MARKER"}]
        self.assertEqual(agent.final_text(events, "claude"), "HIDDEN_MARKER")
        with self.assertRaises(lib.compat.GateError):
            agent.claude_hook_health(events)
        with self.assertRaises(lib.compat.GateError):
            agent.claude_hook_health(events[1:])

    def test_diagnostics_never_include_host_controlled_strings(self):
        secret = "private-model-or-memhub-key"
        events = [{"type": "system", "subtype": "hook_response", "hook_event": secret,
                   "outcome": secret, "exit_code": secret, "stderr": "uv: command not found " + secret},
                  {"type": "item.completed", "item": {"type": "command_execution", "status": "failed",
                   "exit_code": 1, "command": secret, "aggregated_output": "bwrap: Operation not permitted " + secret}}]
        diagnostics = agent.event_diagnostics(events)
        self.assertNotIn(secret, json.dumps(diagnostics))
        self.assertIn("uv_missing", diagnostics["hooks"][0]["signals"])
        self.assertIn("sandbox_error", diagnostics["commands"][0]["signals"])

    def test_capture_requires_this_session_and_fresh_success(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "home/.config/memhub-plugin/codexflush"
            folder.mkdir(parents=True)
            (folder / "different-session.json").write_text(json.dumps({"last_ok_at": 100}))
            self.assertFalse(agent.capture_ok(root, "codex", 99, SID))
            path = folder / f"{SID}.json"
            for row in ({"last_ok_at": 98}, {"last_ok_at": 100, "last_error": "unconfirmed"},
                        {"last_ok_at": 100, "unsupported": True},
                        {"last_ok_at": 100, "pending_pr_urls": ["pending"]}):
                path.write_text(json.dumps(row))
                self.assertFalse(agent.capture_ok(root, "codex", 99, SID))
            path.write_text(json.dumps({"last_ok_at": 100, "last_error": None}))
            self.assertTrue(agent.capture_ok(root, "codex", 99, SID))

    def test_claude_requires_turn_and_session_end_capture(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "home/.config/memhub-plugin/turnflush"
            folder.mkdir(parents=True)
            for suffix in ("json", "sessionflush.json"):
                (folder / f"{SID}.{suffix}").write_text(json.dumps({"last_ok_at": 100}))
            self.assertTrue(agent.capture_ok(root, "claude", 99, SID))
            (folder / f"{SID}.sessionflush.json").unlink()
            self.assertFalse(agent.capture_ok(root, "claude", 99, SID))

    def test_capture_identity_cannot_escape_home(self):
        with self.assertRaises(lib.compat.GateError):
            agent.session_id([{"type": "thread.started", "thread_id": "../../other-file"}], "codex")

    def test_fixture_cannot_target_arbitrary_repo_or_reuse_one_rule(self):
        fixture = {"schema_version": 1, "org_id": SID, "repo": agent.REPO,
                   "advice_rule_id": SID, "gate_rule_id": "22222222-2222-4222-8222-222222222222",
                   "advice_marker": "MEMHUB_RELEASE_ADVICE_12345678"}
        self.assertEqual(agent.fixture_config(json.dumps(fixture)), fixture)
        for changed in (dict(fixture, repo="real-customer-repo"), dict(fixture, gate_rule_id=SID),
                        dict(fixture, advice_marker="arbitrary prose")):
            with self.assertRaises((lib.compat.GateError, lib.NotVerified)):
                agent.fixture_config(json.dumps(changed))

    def test_unknown_cli_output_is_withheld(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(lib.compat.GateError, "raw output withheld") as caught:
                lib.run([sys.executable, "-c", "print('secret'); raise SystemExit(1)"],
                        env=lib.isolated_env(Path(raw)), cwd=raw)
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
