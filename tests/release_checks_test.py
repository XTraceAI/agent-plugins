"""The release harness must reject missing evidence, not turn it into success."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
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
session_start = module("release_session_start_test", "check-host-session-start.py")
PACKAGE = ROOT / "plugins/memhub"
SHA = "a" * 40
SID = "11111111-1111-4111-8111-111111111111"


class ReleaseChecksTests(unittest.TestCase):
    def test_fixture_probe_loads_transport_siblings_in_fresh_process(self):
        code = r"""
import importlib.util, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, 'scripts')
spec = importlib.util.spec_from_file_location('agent_check', 'scripts/check-agent-session.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
original = list(sys.path)
class ReachedTransport(Exception): pass
with patch('urllib.request.OpenerDirector.open', side_effect=ReachedTransport):
    try:
        agent.validate_live_fixture(Path('plugins/memhub').resolve(), {}, 'mhk_test-only')
    except ReachedTransport:
        pass
    else:
        raise AssertionError('probe did not reach the transport')
assert sys.path == original
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgrade_notice_diagnostics_identify_each_missing_requirement(self):
        nonce = "999.4242.17"
        fields = {"error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": nonce,
                  "remediation": "Update MemHub to 999.4242.17 or newer, then restart this agent session."}
        complete = agent.upgrade_notice_evidence("", fields, nonce, True)
        self.assertTrue(all(complete.values()))
        for field, answer, contacted in (
            ("server_contacted", fields, False),
            ("error_code_reported", {**fields, "error_code": "UPGRADE"}, True),
            ("minimum_version_reported", {**fields, "minimum_version": "999.0.0"}, True),
            ("restart_reported", {**fields, "remediation": "retry later"}, True),
        ):
            with self.subTest(field=field):
                evidence = agent.upgrade_notice_evidence("", answer, nonce, contacted)
                self.assertEqual([key for key in agent.UPGRADE_REQUIRED_EVIDENCE if not evidence[key]], [field])
        # A prose answer with the right facts still counts; it is recorded as unstructured.
        prose = agent.upgrade_notice_evidence(
            f"PLUGIN_UPGRADE_REQUIRED: update to {nonce} and restart.", {}, nonce, True)
        self.assertTrue(all(prose[key] for key in agent.UPGRADE_REQUIRED_EVIDENCE))
        self.assertFalse(prose["structured_answer"])
        # The constant that used to be accepted is not the nonce.
        stale = agent.upgrade_notice_evidence("PLUGIN_UPGRADE_REQUIRED 999.0.0 restart", {}, nonce, True)
        self.assertFalse(stale["minimum_version_reported"])
        evidence = agent.upgrade_notice_evidence("secret-from-child-output", {"remediation": "secret-two"}, nonce, True)
        self.assertTrue(all(type(value) is bool for value in evidence.values()))
        self.assertNotIn("secret", json.dumps(evidence))

    def test_upgrade_response_diagnostics_separate_sources_without_exposing_text(self):
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "restart secret-output"}]}},
            {"subtype": "hook_response", "stdout": "restart secret-hook"},
            {"type": "result", "subtype": "success", "result": "secret-result"},
        ]
        details = agent.upgrade_response_diagnostics(events, "open a new session secret-result", {})
        self.assertTrue(details["assistant_text_has_restart"])
        self.assertTrue(details["hook_output_has_restart"])
        self.assertFalse(details["final_text_has_restart"])
        self.assertTrue(details["final_text_has_new_session"])
        self.assertEqual(details["result_kind"], "success")
        self.assertNotIn("secret", json.dumps(details))
        events[-1]["subtype"] = "secret-subtype"
        self.assertEqual(agent.upgrade_response_diagnostics(events, "", {})["result_kind"], "other")
        self.assertFalse(agent.upgrade_notice_evidence("open a new session", {}, "999.1.2", True)["restart_reported"])

    def test_claude_upgrade_requires_complete_native_delivery_and_fresh_model_version(self):
        import copy
        nonce = "999.4242.17"
        notice = f"PLUGIN_UPGRADE_REQUIRED: Update to {nonce}, then restart this agent session."
        output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": notice},
                  "systemMessage": notice}
        event = {"subtype": "hook_response", "hook_event": "PreToolUse", "outcome": "success",
                 "exit_code": 0, "stdout": json.dumps(output)}
        def passes(events, answer):
            evidence = agent.upgrade_notice_evidence("", answer, nonce, True)
            evidence["native_notice_delivered"] = agent.claude_upgrade_delivery(events, nonce)
            return all(evidence[k] for k in agent.required_upgrade_evidence("claude"))
        self.assertTrue(passes([event], {"minimum_version": nonce}))
        self.assertTrue(passes([{**event, "output": event["stdout"], "stdout": ""}], {"minimum_version": nonce}))
        self.assertTrue(passes([{**event, "output": ""}], {"minimum_version": nonce}))
        self.assertFalse(passes([{**event, "output": "not json"}], {"minimum_version": nonce}))
        self.assertFalse(passes([event], {"minimum_version": "999.0.0"}))
        self.assertFalse(passes([], {"minimum_version": nonce, "error_code": "PLUGIN_UPGRADE_REQUIRED", "remediation": "restart"}))
        for field in ("additionalContext", "systemMessage"):
            for missing in ("PLUGIN_UPGRADE_REQUIRED", nonce, "restart this agent session"):
                changed = copy.deepcopy(output)
                target = changed["hookSpecificOutput"] if field == "additionalContext" else changed
                target[field] = target[field].replace(missing, "")
                self.assertFalse(passes([{**event, "stdout": json.dumps(changed)}], {"minimum_version": nonce}))
        for changed in ({"exit_code": 1}, {"outcome": "error"}, {"hook_event": "Stop"},
                        {"subtype": "assistant"}, {"stdout": "not json"}, {"stdout": "[]"}):
            self.assertFalse(passes([{**event, **changed}], {"minimum_version": nonce}))
        self.assertEqual(agent.required_upgrade_evidence("codex"), agent.UPGRADE_REQUIRED_EVIDENCE)
        self.assertEqual(agent.required_upgrade_evidence("cursor"), agent.UPGRADE_REQUIRED_EVIDENCE)

    def test_upgrade_nonce_is_fresh_and_acceptable_to_the_hook(self):
        import re
        seen = {agent.upgrade_nonce() for _ in range(20)}
        self.assertGreater(len(seen), 1)
        for nonce in seen:
            # The hook's shape, and a release's shape: `999.NNNNNN.NNNNNN` made
            # Sonnet 4.6 call the notice fabricated and refuse to report it.
            self.assertRegex(nonce, r"^[3-9]\.[1-9][0-9]\.[1-9][0-9]{2}$")
            self.assertNotEqual(nonce, "999.0.0")
            self.assertNotEqual(nonce, agent.policy.PolicyServer().minimum_version)

    def test_policy_server_serves_the_configured_minimum_version(self):
        import urllib.request
        server = agent.policy.PolicyServer(minimum_version="999.77.88")
        server.reject = True
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(server.url + "/v1/team/rulebook/rules?view=hook&repo=x")
            body = json.loads(caught.exception.read())
            self.assertEqual(caught.exception.code, 426)
            self.assertEqual(body["data"]["minimum_version"], "999.77.88")
            self.assertEqual(body["data"]["policy_revision"], "rulebook-v1:999.77.88")
            self.assertEqual(body["data"]["error_code"], "PLUGIN_UPGRADE_REQUIRED")
        finally:
            server.close()
        # The contract test's default is unchanged.
        self.assertEqual(agent.policy.PolicyServer().minimum_version, "999.0.0")

    def test_answer_fields_takes_the_last_object_and_survives_wrapping(self):
        obj = {"error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": "999.1.2", "remediation": "restart"}
        raw = json.dumps(obj)
        self.assertEqual(agent.answer_fields(raw), obj)
        self.assertEqual(agent.answer_fields("Here is the report:\n```json\n" + raw + "\n```\n"), obj)
        self.assertEqual(agent.answer_fields("Ran the echo. " + raw + " Done."), obj)
        # Two objects: the last one is the answer (an earlier one is a quoted hook payload).
        self.assertEqual(agent.answer_fields('{"hookSpecificOutput": {"x": 1}}\n' + raw), obj)
        # Nested braces inside strings do not break the scan.
        nested = {"advice": "use {braces} carefully", "blocked_command_denied": True, "allowed_file_written": True}
        self.assertEqual(agent.answer_fields("done " + json.dumps(nested)), nested)
        for junk in ("no json here", "{not: json}", "[1, 2]", "", None, 42):
            self.assertEqual(agent.answer_fields(junk), {}, junk)

    def test_final_answer_prefers_claude_structured_output_and_parses_codex_text(self):
        structured = {"error_code": "PLUGIN_UPGRADE_REQUIRED", "minimum_version": "999.5.6", "remediation": "restart"}
        events = [{"type": "result", "result": "prose that disagrees {\"error_code\": \"X\"}", "structured_output": structured}]
        self.assertEqual(agent.final_answer(events, "claude"), (events[0]["result"], structured))
        # No structured_output: parse the text.
        events = [{"type": "result", "result": json.dumps(structured)}]
        self.assertEqual(agent.final_answer(events, "claude")[1], structured)
        # Codex: the joined agent messages, last object wins.
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "working"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(structured)}},
                  {"type": "turn.completed"}]
        self.assertEqual(agent.final_answer(events, "codex")[1], structured)
        # A failed turn still cannot pass on an earlier message.
        with self.assertRaises(lib.compat.GateError):
            agent.final_answer(events + [{"type": "turn.failed"}], "codex")

    def test_commands_carry_the_answer_schema_per_host(self):
        with tempfile.TemporaryDirectory() as raw:
            path = agent.answer_schema_path(raw, agent.UPGRADE_ANSWER)
            self.assertEqual(json.loads(Path(path).read_text()), agent.UPGRADE_ANSWER)
            codex = agent.command("codex", "codex", Path(raw), "m", schema=agent.UPGRADE_ANSWER, schema_path=path)
            self.assertEqual(codex[codex.index("--output-schema") + 1], str(path))
            self.assertEqual(codex[-1], "-")
            claude = agent.command("claude", "claude", Path(raw), "m", schema=agent.UPGRADE_ANSWER, schema_path=path)
            self.assertEqual(json.loads(claude[claude.index("--json-schema") + 1]), agent.UPGRADE_ANSWER)
            self.assertNotIn("--output-schema", claude)
            # Without a schema the commands are exactly as before.
            self.assertNotIn("--json-schema", agent.command("claude", "claude", Path(raw), "m"))
            self.assertNotIn("--output-schema", agent.command("codex", "codex", Path(raw), "m"))

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

    def test_codex_bridge_cold_gate_upgrade_and_rollback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = lib.isolated_env(root)
            ws = agent.prepare_workspace(root, env, "memhub-release-upgrade")
            server = agent.policy.PolicyServer()
            env.update(MEMHUB_TOKEN="synthetic", MEMHUB_RULEBOOK_RECALL="0",
                       MEMHUB_PLUGIN_ROOT=str(PACKAGE),
                       MEMHUB_MCP_BASE_URL=server.url + "/mcp-server/mcp")
            try:
                def invoke(tool, inp, sid):
                    payload = json.dumps({"cwd": str(ws), "session_id": sid,
                                          "tool_name": tool, "tool_input": inp})
                    return json.loads(lib.run([sys.executable, str(PACKAGE / "scripts/codex_hook_bridge.py"),
                                               "dispatch", "PreToolUse"], env=env, cwd=ws, stdin=payload))
                for i, (tool, inp) in enumerate((
                        ("exec_command", {"cmd": "memhub-synthetic-command"}),
                        ("shell", {"command": ["/bin/bash", "-lc", "memhub-synthetic-command"]}),
                        ("shell_command", {"command": "memhub-synthetic-command"}))):
                    result = invoke(tool, inp, f"cold-session-{i}")
                    self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertTrue(server.requests)  # No pre-seeded book: bridge fetched it.
                fetch = [sys.executable, str(PACKAGE / "scripts/rulebook_hook.py"),
                         "fetch", "memhub-release-upgrade"]
                server.reject = True
                lib.run(fetch, env=env, cwd=ws)
                result = invoke("exec_command", {"cmd": "memhub-synthetic-command"}, "upgrade-session")
                self.assertIn("PLUGIN_UPGRADE_REQUIRED", result["hookSpecificOutput"]["additionalContext"])
                self.assertNotEqual(result["hookSpecificOutput"].get("permissionDecision"), "deny")
                server.reject = False
                lib.run(fetch, env=env, cwd=ws)
                result = invoke("exec_command", {"cmd": "memhub-synthetic-command"}, "rollback-session")
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            finally:
                server.close()

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

    def test_session_manifest_records_the_id_capture_sent_and_only_that(self):
        """The manifest is what cleanup deletes from, so it must carry the
        harness id exactly as the plugin's capture sent it (codex- / cursor-
        prefixed, Claude bare), survive the disposable home, dedup a re-record,
        and refuse an id that could not be a session."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "reports/sessions.json"
            with tempfile.TemporaryDirectory() as home:
                lib.isolated_env(Path(home))
                agent.record_session(manifest, host="codex", sid=SID, org_id=SID)
            # The agent home is gone; the manifest is not.
            rows = json.loads(manifest.read_text())["sessions"]
            self.assertEqual([r["harness_session_id"] for r in rows], [f"codex-{SID}"])
            agent.record_session(manifest, host="claude", sid=SID, org_id=SID)
            agent.record_session(manifest, host="cursor", sid=SID, org_id=SID)
            agent.record_session(manifest, host="codex", sid=SID, org_id=SID)  # re-record: no duplicate
            rows = json.loads(manifest.read_text())["sessions"]
            self.assertEqual([r["harness_session_id"] for r in rows], [f"codex-{SID}", SID, f"cursor-{SID}"])
            self.assertTrue(all(r["org_id"] == SID and r["repo"] == agent.REPO for r in rows))
            self.assertFalse(manifest.with_name("sessions.json.tmp").exists())
            for bad in ("../../other-file", "x" * 200, "short"):
                with self.assertRaises(lib.compat.GateError):
                    agent.record_session(manifest, host="codex", sid=bad, org_id=SID)
            with self.assertRaises(ValueError):
                agent.record_session(manifest, host="codex", sid=SID, org_id="not-an-org")
            self.assertEqual(len(json.loads(manifest.read_text())["sessions"]), 3)

    def test_identity_streams_out_before_the_host_fails(self):
        """A host that announces its session and then exits nonzero, or hangs
        past its budget, has already let capture create the production
        session. ``drive`` must hand the identity to ``on_event`` mid-run, so
        the manifest exists even though ``drive`` itself raises."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = lib.isolated_env(root)
            for name, tail in (("crash", "raise SystemExit(1)"), ("hang", "import time; time.sleep(30)")):
                # Stands in for the host binary: ``command()`` puts the
                # executable first and the host's flags after it, so the fake
                # must be the executable itself and ignore its arguments.
                fake = root / f"{name}-host"
                fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                                f"print(json.dumps({{'type': 'thread.started', 'thread_id': {SID!r}}}), flush=True)\n"
                                "sys.stdin.read()\n" + tail + "\n")
                fake.chmod(0o700)
                seen = []
                with self.subTest(name=name), patch.object(agent, "AGENT_TIME_BUDGET_S", 2), \
                        self.assertRaises(lib.compat.GateError):
                    agent.drive("codex", str(fake), None, "m", "prompt", env, root,
                                on_event=lambda e: seen.append(agent.identity_of(e, "codex")))
                self.assertEqual([v for v in seen if v], [SID], name)

    def test_identity_extractor_is_the_one_the_final_check_uses(self):
        codex = [{"type": "thread.started", "thread_id": SID}, {"type": "item.completed"}]
        claude = [{"type": "system", "subtype": "init", "session_id": SID}, {"type": "result"}]
        self.assertEqual([agent.identity_of(e, "codex") for e in codex], [SID, None])
        self.assertEqual([agent.identity_of(e, "claude") for e in claude], [SID, None])
        self.assertEqual(agent.session_id(codex, "codex"), SID)
        self.assertEqual(agent.session_id(claude, "cursor"), SID)
        with self.assertRaises(lib.compat.GateError):
            agent.session_id([{"type": "thread.started", "thread_id": 7}], "codex")

    def test_fixture_cannot_target_arbitrary_repo_or_reuse_one_rule(self):
        fixture = {"schema_version": 1, "org_id": SID, "repo": agent.REPO,
                   "advice_rule_id": SID, "gate_rule_id": "22222222-2222-4222-8222-222222222222",
                   "advice_marker": "MEMHUB_RELEASE_ADVICE_12345678"}
        self.assertEqual(agent.fixture_config(json.dumps(fixture)), fixture)
        for changed in (dict(fixture, repo="real-customer-repo"), dict(fixture, gate_rule_id=SID),
                        dict(fixture, advice_marker="arbitrary prose")):
            with self.assertRaises((lib.compat.GateError, lib.NotVerified)):
                agent.fixture_config(json.dumps(changed))

    def test_claude_session_start_evidence_needs_the_notice_in_the_host_record(self):
        notice = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                  "PLUGIN_UPGRADE_REQUIRED: MemHub plugin 0.0.0 is unsupported. Update MemHub to 999.0.0 "
                  "or newer using your host's plugin manager, then restart this agent session."}}
        def response(output, outcome="success"):
            return {"type": "system", "subtype": "hook_response", "hook_event": "SessionStart",
                    "outcome": outcome, "exit_code": 0, "output": output}
        class Server:
            requests = ["/v1/team/rulebook/rules?view=hook&repo=memhub-release-upgrade&hook_version=0.57.0"]
        good = session_start.claude_evidence([response(""), response(json.dumps(notice))], Server())
        self.assertEqual(good, {"hook_responses": 2, "all_succeeded": True,
                                "notice_in_host_record": True, "server_fetched": True})
        # A hook that ran but printed the notice somewhere the host does not
        # forward (stderr, a bare line) is not delivery.
        bare = session_start.claude_evidence([response("PLUGIN_UPGRADE_REQUIRED 999.0.0 restart")], Server())
        self.assertFalse(bare["notice_in_host_record"])
        # A cancelled hook is not success even if its output looks right.
        cancelled = session_start.claude_evidence([response(json.dumps(notice), outcome="cancelled")], Server())
        self.assertFalse(cancelled["all_succeeded"])
        # No SessionStart hook at all: the host did not run the plugin.
        none = session_start.claude_evidence([{"type": "system", "subtype": "init"}], Server())
        self.assertEqual(none["hook_responses"], 0)
        self.assertFalse(none["all_succeeded"])
        # The version-carrying fetch is what produced the 426; without it the
        # notice could be a stale marker from an earlier session.
        class Quiet:
            requests = ["/v1/team/rulebook/rules?view=hook&repo=memhub-release-upgrade"]
        self.assertFalse(session_start.claude_evidence([response(json.dumps(notice))], Quiet())["server_fetched"])

    def test_codex_session_start_evidence_is_the_hooks_own_per_session_record(self):
        import hashlib
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book = root / "book" / "memhub-release-upgrade-deadbeef.json"
            book.parent.mkdir()
            thread = "019a0000-0000-7000-8000-000000000001"
            marker = book.parent / (book.name + ".notice-" + hashlib.sha256(thread.encode()).hexdigest()[:16])
            events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.failed"}]
            class Server:
                requests = ["/v1/team/rulebook/rules?view=hook&repo=memhub-release-upgrade&hook_version=0.57.0"]
            with patch.object(session_start, "run", return_value=str(book) + "\n"):
                before = session_start.codex_evidence(events, root, root, {}, Server())
                self.assertEqual(before, {"session_started": True, "server_fetched": True, "notice_markers": 0,
                                          "notice_emitted_for_session": False, "notice_in_host_stream": False})
                marker.write_text("[]")
                after = session_start.codex_evidence(events, root, root, {}, Server())
                self.assertTrue(after["notice_emitted_for_session"])
                self.assertEqual(after["notice_markers"], 1)
                # A marker for some OTHER session is not evidence for this one.
                other = session_start.codex_evidence([{"type": "thread.started", "thread_id": "other"}],
                                                     root, root, {}, Server())
                self.assertFalse(other["notice_emitted_for_session"])
                # No thread: Codex never started a session; nothing else can compensate.
                self.assertFalse(session_start.codex_evidence([], root, root, {}, Server())["session_started"])

    def test_session_start_notice_needs_all_three_facts(self):
        full = "PLUGIN_UPGRADE_REQUIRED: update to 999.0.0 then restart"
        self.assertTrue(session_start.notice_delivered(full))
        for broken in (full.replace("PLUGIN_UPGRADE_REQUIRED", "upgrade"), full.replace("999.0.0", "1.0.0"),
                       full.replace("restart", "retry"), None, 42):
            self.assertFalse(session_start.notice_delivered(broken), broken)

    def test_unknown_cli_output_is_withheld(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(lib.compat.GateError, "raw output withheld") as caught:
                lib.run([sys.executable, "-c", "print('secret'); raise SystemExit(1)"],
                        env=lib.isolated_env(Path(raw)), cwd=raw)
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
