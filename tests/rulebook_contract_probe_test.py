"""Probe failure tests. Real backend coverage lives in its contract suite."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("contract_probe", ROOT / "scripts/check-rulebook-contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

# This synthetic fixture tests the probe's failure detection, not backend behavior.
CONTRACT = {"contract_id": "memhub.rulebook.fetch.v1", "path": "/v1/team/rulebook/rules",
            "repo": "probe fixture/repo", "rule_floor": "0.54.0",
            "expected_titles": ["probe-rule"]}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MEMHUB_CONTRACT_TOKEN": "synthetic",
                                          "MEMHUB_RULEBOOK_BASE": self.temp.name})
        self.env.start()
        self.mode = "ok"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status = 400 if owner.mode == "rejected" else 200
                if self.headers.get("If-None-Match") and owner.mode != "always_200":
                    status = 304
                rules = [] if owner.mode == "empty" else [{"title": "probe-rule", "mode": "gate"}]
                data = {"code": 0, "data": {"rules": rules}}
                if owner.mode == "bad_envelope":
                    data["code"] = 1
                if owner.mode == "bad_shape":
                    data["data"] = {"rules": "not a list"}
                body = b"" if status == 304 else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("ETag", '"fixture"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        self.temp.cleanup()

    def test_valid_fetch_and_304_pass(self):
        self.assertTrue(probe.probe(self.base, "org-fixture", CONTRACT)["ok"])

    def test_http_200_is_not_enough(self):
        for mode in ("empty", "bad_envelope", "bad_shape", "always_200", "rejected"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(ValueError):
                    probe.probe(self.base, "org-fixture", CONTRACT)

    def test_cannot_fake_candidate_version(self):
        with patch.dict(os.environ, {"MEMHUB_RULEBOOK_HOOK_VERSION": "99.0.0"}):
            with self.assertRaisesRegex(ValueError, "override is forbidden"):
                probe.probe(self.base, "org-fixture", CONTRACT)

    def test_production_origin_is_not_supported_yet(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            probe.probe("https://api.example.com", "org-fixture", CONTRACT)


class PinTests(unittest.TestCase):
    def test_changed_contract_or_digest_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "contracts").mkdir()
            raw = b'{"contract_id":"fixture"}'
            lock = {"revision": "a" * 40, "path": "contract.json", "sha256": hashlib.sha256(raw).hexdigest()}
            (root / "contract.json").write_bytes(raw)
            (root / "contracts/rulebook.lock.json").write_text(json.dumps(lock))
            result = subprocess.CompletedProcess([], 0, stdout=raw)
            with patch.object(probe, "ROOT", root), patch.object(probe.subprocess, "run", return_value=result):
                self.assertEqual(probe.read_contract(root), {"contract_id": "fixture"})
                (root / "contract.json").write_bytes(b'{}')
                with self.assertRaisesRegex(ValueError, "differs from pinned"):
                    probe.read_contract(root)
                lock["sha256"] = "0" * 64
                (root / "contracts/rulebook.lock.json").write_text(json.dumps(lock))
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    probe.read_contract(root)


if __name__ == "__main__":
    unittest.main()
