"""Cleanup deletes exactly this attempt's sessions, verifies, and reports every outcome by name."""
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
sys.path.insert(0, str(ROOT / "plugins/memhub/scripts"))
import release_check_lib as lib  # noqa: E402
import mcp_http  # noqa: E402


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


cleanup = module("release_cleanup_test_module", "cleanup-agent-sessions.py")
ORG = "11111111-1111-4111-8111-111111111111"
OTHER_ORG = "22222222-2222-4222-8222-222222222222"
TOKEN = "mhk_synthetic_release_key"


def session(hid, host="codex", org=ORG):
    return {"host": host, "harness_session_id": hid, "org_id": org}


def cleanup_session(rest, session_row, waited=None):
    """Runs cleanup with the late-capture wait recorded rather than slept."""
    waited = [] if waited is None else waited
    return cleanup.cleanup_session(rest, TOKEN, session_row, sleep=waited.append)


class FakeRest:
    """Scripted replies per session id: each call pops the next entry; an int
    is an HTTP status, an Exception is raised as-is."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, url, bearer, method="GET", body=None, headers=None, timeout=None):
        self.calls.append({"url": url, "bearer": bearer, "method": method, "headers": headers or {},
                           "timeout": timeout})
        hid = url.split("session_id=eq.", 1)[1]
        step = self.script[hid].pop(0)
        if isinstance(step, Exception):
            raise step
        if step == 200:
            return mcp_http.RestReply(200, None, {"deleted": True})
        if step == 404:
            body = '{"code": 404, "msg": "conversation not found", "data": {"reason": "conversation_not_found"}}'
        elif step == "404-bare":
            step, body = 404, "<html>404 Not Found</html>"
        elif step == "404-other":
            step, body = 404, '{"code": 404, "msg": "no", "data": {"reason": "workspace_not_found"}}'
        else:
            body = "no"
        raise mcp_http.McpError(f"DELETE {url} failed ({step}): {body}", step)


class CleanupTests(unittest.TestCase):
    def test_delete_verify_wait_and_recheck_is_the_happy_path(self):
        """Delete, prove it gone, give a late flush its window, prove it again."""
        rest, waited = FakeRest({"codex-abcdefgh": [200, 404, 404]}), []
        self.assertEqual(cleanup_session(rest, session("codex-abcdefgh"), waited), {"outcome": "deleted"})
        self.assertEqual([c["method"] for c in rest.calls], ["DELETE"] * 3)
        self.assertEqual(waited, [cleanup.LATE_CAPTURE_WINDOW_S])
        for call in rest.calls:
            self.assertTrue(call["url"].startswith(lib.compat.PRODUCTION + "/v1/team/conversations?session_id=eq."))
            self.assertEqual(call["headers"], {"X-Org-Id": ORG})
            self.assertEqual(call["bearer"], TOKEN)
            self.assertIsNotNone(call["timeout"])

    def test_already_gone_is_absent_and_idempotent(self):
        """The initial 404 is its own proof; only the late-flush recheck follows."""
        rest, waited = FakeRest({"codex-abcdefgh": [404, 404]}), []
        self.assertEqual(cleanup_session(rest, session("codex-abcdefgh"), waited), {"outcome": "absent"})
        self.assertEqual(len(rest.calls), 2)
        self.assertEqual(waited, [cleanup.LATE_CAPTURE_WINDOW_S])

    def test_a_session_recreated_under_the_delete_is_deleted_again_until_proved_gone(self):
        """Several asynchronous flushes can be in flight for one session: the
        DELETE that discovers a re-creation has just deleted it, so the proof
        goes around again — bounded, and the race is named in the result."""
        rest = FakeRest({"abcdefgh-1111": [200, 200, 404, 404]})
        self.assertEqual(cleanup_session(rest, session("abcdefgh-1111", host="claude")),
                         {"outcome": "deleted", "recreated": 1})
        rest = FakeRest({"abcdefgh-2222": [200, 200, 200, 404, 404]})
        self.assertEqual(cleanup_session(rest, session("abcdefgh-2222", host="claude")),
                         {"outcome": "deleted", "recreated": 2})
        # Keeps coming back: give up, and say so.
        rest = FakeRest({"abcdefgh-3333": [200] * (cleanup.MAX_SETTLE_ROUNDS + 1)})
        result = cleanup_session(rest, session("abcdefgh-3333", host="claude"))
        self.assertEqual(result["outcome"], "recreated")
        self.assertNotIn("recreated", cleanup.OK)
        # The initial delete plus one per bounded reappearance, and no more.
        self.assertEqual(len(rest.calls), cleanup.MAX_SETTLE_ROUNDS + 1)

    def test_every_session_gets_a_late_capture_window(self):
        """A flush can still be in flight after the host is gone (Codex and
        Cursor flush from detached processes): "gone" is re-checked after a
        bounded wait, and a session that lands in between is deleted, verified
        and named as late."""
        for hid, script, outcome, calls in (
            # gone, waited, still gone: absent is final
            ("codex-late0001", [404, 404], {"outcome": "absent"}, 2),
            # gone, waited, landed late: delete (the recheck IS a delete), verify
            ("codex-late0002", [404, 200, 404], {"outcome": "deleted", "late_capture": True}, 3),
            # deleted+verified, waited, landed late, verified again
            ("codex-late0003", [200, 404, 200, 404], {"outcome": "deleted", "late_capture": True}, 4),
            # recreated under the first delete, settled, then the window: the
            # Codex round-3 case — a recreation must not skip the window
            ("codex-late0003b", [200, 200, 404, 404], {"outcome": "deleted", "recreated": 1}, 4),
            # landed late and kept coming back past the bound
            ("codex-late0004", [404] + [200] * cleanup.MAX_SETTLE_ROUNDS,
             {"outcome": "recreated", "http_status": 200}, cleanup.MAX_SETTLE_ROUNDS + 1),
            # the recheck itself could not prove anything
            ("codex-late0005", [404, "404-bare"], {"outcome": "unverified", "http_status": 404}, 2),
        ):
            with self.subTest(hid=hid):
                rest, waited = FakeRest({hid: script}), []
                self.assertEqual(cleanup_session(rest, session(hid), waited), outcome)
                self.assertEqual(waited, [cleanup.LATE_CAPTURE_WINDOW_S])
                self.assertEqual(len(rest.calls), calls)
        # Refused or failed outright: nothing to wait for.
        for hid, script in (("codex-late0006", [401]), ("codex-late0007", [500])):
            rest, waited = FakeRest({hid: script}), []
            cleanup_session(rest, session(hid), waited)
            self.assertEqual((len(rest.calls), waited), (1, []))

    def test_a_404_without_the_backends_reason_proves_nothing(self):
        """A proxy page or a missing route is also a 404; only the backend's
        own ``conversation_not_found`` says the id resolves to nothing."""
        for hid, script, outcome in (
            ("codex-bare404a", ["404-bare"], "failed"),
            ("codex-bare404b", ["404-other"], "failed"),
            ("codex-bare404c", [200, "404-bare"], "unverified"),
            ("codex-bare404d", [200, "404-other"], "unverified"),
        ):
            with self.subTest(hid=hid):
                rest = FakeRest({hid: script})
                self.assertEqual(cleanup_session(rest, session(hid))["outcome"], outcome)

    def test_refused_key_and_transport_failures_are_named(self):
        for hid, script, outcome in (
            ("codex-refused01", [401], "unauthorized"),
            ("codex-refused02", [403], "unauthorized"),
            ("codex-broken001", [500], "failed"),
            ("codex-broken002", [OSError("connection reset")], "failed"),
            ("codex-broken003", [200, OSError("timed out")], "unverified"),
        ):
            with self.subTest(hid=hid):
                rest = FakeRest({hid: script})
                self.assertEqual(cleanup_session(rest, session(hid))["outcome"], outcome)

    def test_ids_are_url_encoded_and_org_bound(self):
        rest = FakeRest({"codex-a%2Fb%3Fc%3Dd": [404, 404]})
        cleanup_session(rest, session("codex-a/b?c=d", org=OTHER_ORG))
        self.assertIn("session_id=eq.codex-a%2Fb%3Fc%3Dd", rest.calls[0]["url"])
        self.assertEqual(rest.calls[0]["headers"]["X-Org-Id"], OTHER_ORG)

    def test_manifest_is_the_only_source_of_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sessions.json"
            self.assertIsNone(cleanup.load_manifest(path))
            path.write_text(json.dumps({"schema_version": 1, "sessions": [
                {"host": "codex", "harness_session_id": "codex-abcdefgh", "org_id": ORG},
                {"host": "claude", "harness_session_id": "abcdefgh-1111", "org_id": ORG}]}))
            self.assertEqual([s["harness_session_id"] for s in cleanup.load_manifest(path)],
                             ["codex-abcdefgh", "abcdefgh-1111"])
            for bad in ({"schema_version": 2, "sessions": []},
                        {"schema_version": 1, "sessions": [{"harness_session_id": "../x", "org_id": ORG}]},
                        {"schema_version": 1, "sessions": [{"harness_session_id": "codex-abcdefgh", "org_id": "nope"}]}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(lib.compat.GateError):
                    cleanup.load_manifest(path)

    def _main(self, manifest_rows, rest, env):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "sessions.json"
            if manifest_rows is not None:
                manifest.write_text(json.dumps({"schema_version": 1, "sessions": manifest_rows}))
            report = root / "cleanup.json"
            summary = root / "summary.md"
            fake_http = type("H", (), {"rest": staticmethod(rest)})
            with patch.dict(os.environ, {**env, "GITHUB_STEP_SUMMARY": str(summary)}, clear=False), \
                    patch.object(cleanup.compat, "load_module", lambda name, path: fake_http), \
                    patch.object(cleanup.time, "sleep", lambda s: None), \
                    patch.object(sys, "argv", ["cleanup", "--manifest", str(manifest), "--report", str(report)]):
                code = cleanup.main()
            return code, json.loads(report.read_text()), summary.read_text() if summary.exists() else ""

    def test_one_failure_never_skips_the_other_sessions(self):
        rest = FakeRest({"codex-abcdefgh": [401], "abcdefgh-1111": [200, 404, 404]})
        code, report, summary = self._main([session("codex-abcdefgh"), session("abcdefgh-1111", host="claude")],
                                           rest, {"MEMHUB_PROD_E2E_TOKEN": TOKEN})
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual({r["harness_session_id"]: r["outcome"] for r in report["sessions"]},
                         {"codex-abcdefgh": "unauthorized", "abcdefgh-1111": "deleted"})
        self.assertEqual(report["counts"], {"deleted": 1, "unauthorized": 1})
        self.assertNotIn("late_capture", report["counts"])
        self.assertIn("Session cleanup: FAILED", summary)
        self.assertNotIn(TOKEN, json.dumps(report) + summary)

    def test_no_manifest_and_empty_manifest_are_clean_not_failures(self):
        for rows in (None, []):
            with self.subTest(rows=rows):
                rest = FakeRest({})
                code, report, summary = self._main(rows, rest, {"MEMHUB_PROD_E2E_TOKEN": TOKEN})
                self.assertEqual(code, 0)
                self.assertTrue(report["ok"])
                self.assertEqual(rest.calls, [])
                self.assertIn("Session cleanup: ok", summary)

    def test_missing_key_deletes_nothing_and_fails_loudly(self):
        rest = FakeRest({"codex-abcdefgh": [200, 404]})
        env = {k: v for k, v in os.environ.items() if k != "MEMHUB_PROD_E2E_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            code, report, summary = self._main([session("codex-abcdefgh")], rest, {})
        self.assertEqual(code, 1)
        self.assertEqual(rest.calls, [])
        self.assertEqual(report["sessions"][0]["outcome"], "skipped")
        self.assertIn("not provisioned", summary)


if __name__ == "__main__":
    unittest.main()
