"""ENG-1118: package releases must retain Auth0's registered Codex identity."""
import base64
import copy
import hashlib
import json
import unittest

from version_parity_test import (
    MCP_AP, MCP_CLAUDE, MCP_STAGING, PRODUCTION_MCP_URL, STAGING_MCP_URL,
    connection_errors,
)


def callback_id(url):
    # Codex oauth_callback.rs: SHA-256 of the complete URL, first nine bytes,
    # URL-safe base64. These values were checked against the production log
    # and the existing Auth0 CIMD registration, not inferred from the config.
    return base64.urlsafe_b64encode(hashlib.sha256(url.encode()).digest()[:9]).decode()


class McpOAuthIdentityTests(unittest.TestCase):
    def test_shipped_connections_reuse_canonical_oauth_identity(self):
        for path, endpoint, registered in (
            (MCP_AP, PRODUCTION_MCP_URL, "YzZcYxKAiT6g"),
            (MCP_CLAUDE, PRODUCTION_MCP_URL, "YzZcYxKAiT6g"),
            (MCP_STAGING, STAGING_MCP_URL, "lYfHlhqd4tzK"),
        ):
            with self.subTest(config=path):
                config = json.loads(path.read_text())
                server = config["mcpServers"]["memhub"]
                self.assertEqual(server["url"], endpoint)
                self.assertEqual(callback_id(server["url"]), registered)
                for version in ("0.76.1", "0.77.0"):
                    candidate = copy.deepcopy(config)
                    candidate["mcpServers"]["memhub"]["headers"]["X-MemHub-Plugin-Version"] = version
                    self.assertEqual(connection_errors(candidate, version, endpoint), [])
                    self.assertEqual(callback_id(candidate["mcpServers"]["memhub"]["url"]), registered)

    def test_release_guard_rejects_the_production_incident(self):
        config = json.loads(MCP_AP.read_text())
        server = config["mcpServers"]["memhub"]
        version = server["headers"]["X-MemHub-Plugin-Version"]
        server["url"] += "?memhub_plugin_version=0.76.0"
        self.assertEqual(callback_id(server["url"]), "-ugMLRSp9raH")
        self.assertTrue(connection_errors(config, version, PRODUCTION_MCP_URL))

    def test_release_guard_rejects_missing_stale_and_ambiguous_version_headers(self):
        for headers in ({}, {"X-MemHub-Plugin-Version": "0.76.0"},
                        {"X-MemHub-Plugin-Version": "0.76.1",
                         "x-memhub-plugin-version": "0.76.0"}):
            with self.subTest(headers=headers):
                config = json.loads(MCP_AP.read_text())
                config["mcpServers"]["memhub"]["headers"] = headers
                self.assertTrue(connection_errors(config, "0.76.1", PRODUCTION_MCP_URL))


if __name__ == "__main__":
    unittest.main()
