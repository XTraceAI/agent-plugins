"""Gate rejection tests using the real candidate hook and HTTP parser, with fake HTTPS I/O."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("production_probe", ROOT / "scripts/check-production-compatibility.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
FIXTURE = {"schema_version": 1, "org_id": "11111111-1111-4111-8111-111111111111",
           "repo": "production fixture/repo", "rules": [
               {"id": "22222222-2222-4222-8222-222222222222", "mode": "gate"}]}


class Response:
    def __init__(self, status, body):
        self.status = status
        self.headers = {"ETag": '"fixture"'}
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body


class ProductionGateTests(unittest.TestCase):
    def setUp(self):
        self.mode = "ok"
        self.requests = []
        original = gate.load_module

        def load(name, path):
            module = original(name, path)
            if name == "production_candidate_http":
                module._opener = lambda: self
            return module

        self.loader = patch.object(gate, "load_module", side_effect=load)
        self.loader.start()

    def tearDown(self):
        self.loader.stop()

    def open(self, request, timeout):
        self.requests.append(request)
        self.assertTrue(request.full_url.startswith(gate.PRODUCTION + gate.RULES_PATH + "?"))
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(request.get_header("X-org-id"), FIXTURE["org_id"])
        self.assertEqual(request.get_method(), "GET")
        conditional = request.get_header("If-none-match")
        if self.mode in {"401", "400", "426", "500", "302"}:
            raise urllib.error.HTTPError(request.full_url, int(self.mode), "fixture", {}, io.BytesIO(b"private body"))
        if self.mode == "offline":
            raise urllib.error.URLError("private detail")
        if conditional and self.mode != "always_200":
            raise urllib.error.HTTPError(request.full_url, 304, "fixture", {"ETag": '"fixture"'}, io.BytesIO())
        # Shaped like the real hook view: the server names a rule `rule_id`. A fake
        # that echoed the fixture's `id` key hid a KeyError that failed every live run.
        rules = [{"rule_id": r["id"], "mode": r["mode"]} for r in FIXTURE["rules"]]
        if self.mode == "empty":
            rules = []
        if self.mode == "wrong_mode":
            rules[0]["mode"] = "advise"
        if self.mode == "wrong_org":
            rules[0]["rule_id"] = "33333333-3333-4333-8333-333333333333"
        if self.mode == "id_keyed_rows":
            rules = [{"id": r["id"], "mode": r["mode"]} for r in FIXTURE["rules"]]
        payload = {"code": 0, "data": {"rules": rules}}
        if self.mode == "bad_envelope":
            payload["code"] = 1
        if self.mode == "bad_shape":
            payload["data"]["rules"] = "broken"
        response = Response(200, json.dumps(payload).encode())
        if self.mode == "no_etag":
            response.headers = {}
        return response

    def run_probe(self):
        return gate.probe(ROOT / "plugins/memhub", FIXTURE, "synthetic-token")

    def test_candidate_fetch_and_revalidation(self):
        result = self.run_probe()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(result["package_sha256"]), 64)
        self.assertNotIn(FIXTURE["org_id"], json.dumps(result))

    def test_http_success_alone_cannot_pass(self):
        for mode in ("empty", "wrong_mode", "wrong_org", "id_keyed_rows", "bad_envelope", "bad_shape", "no_etag",
                     "always_200", "401", "400", "426", "500", "302", "offline"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(gate.GateError):
                    self.run_probe()

    def test_no_credentials_or_version_override(self):
        with self.assertRaises(gate.GateError):
            gate.probe(ROOT / "plugins/memhub", FIXTURE, "")
        with patch.dict(os.environ, {"MEMHUB_RULEBOOK_HOOK_VERSION": "999.0.0"}):
            with self.assertRaises(gate.GateError):
                self.run_probe()
        self.assertEqual(self.requests, [])

    def test_fixture_must_be_provisioned_and_nonempty(self):
        for raw in ("", "{}", "null", json.dumps(dict(FIXTURE, rules=[])),
                    json.dumps(dict(FIXTURE, org_id="placeholder"))):
            with self.subTest(raw=raw), self.assertRaises(gate.GateError):
                gate.fixture_config(raw)
        self.assertEqual(gate.fixture_config(json.dumps(FIXTURE)), FIXTURE)

    def test_symlinks_and_changed_bytes_cannot_share_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "file").write_text("before")
            before = gate.package_digest(root)
            (root / "file").write_text("after")
            self.assertNotEqual(gate.package_digest(root), before)
            (root / "link").symlink_to(root / "file")
            with self.assertRaises(gate.GateError):
                gate.package_digest(root)

    def test_staging_identity_and_destination_fail_before_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in gate.MANIFESTS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"name": "memhub", "version": "1.0.0"}))
            mcp = root / ".mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"memhub": {"url": "https://staging.example/mcp"}}}))
            with self.assertRaises(gate.GateError):
                gate.package_version(root)
            mcp.write_text(json.dumps({"mcpServers": {"memhub": {"url": gate.PRODUCTION + "/mcp-server/mcp"}}}))
            self.assertEqual(gate.package_version(root), "1.0.0")
            (root / "plugin.json").write_text(json.dumps({"name": "memhub-staging", "version": "1.0.0"}))
            with self.assertRaises(gate.GateError):
                gate.package_version(root)


if __name__ == "__main__":
    unittest.main()
