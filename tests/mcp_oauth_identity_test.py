"""ENG-1118: new promotions must keep Codex's registered OAuth identity."""
import base64
import hashlib
import unittest

from version_parity_test import PRODUCTION_MCP_URL, connection_errors


def config(version, *, url=PRODUCTION_MCP_URL, headers=None):
    return {"mcpServers": {"memhub": {
        "url": url,
        "headers": {"X-MemHub-Plugin-Version": version} if headers is None else headers,
    }}}


class McpOAuthIdentityTests(unittest.TestCase):
    def test_new_versions_keep_the_registered_identity(self):
        for version in ("0.77.1", "0.78.0"):
            candidate = config(version)
            self.assertEqual(connection_errors(candidate, version, PRODUCTION_MCP_URL), [])
            url = candidate["mcpServers"]["memhub"]["url"]
            identity = base64.urlsafe_b64encode(hashlib.sha256(url.encode()).digest()[:9]).decode()
            self.assertEqual(identity, "YzZcYxKAiT6g")

    def test_only_the_already_shipped_legacy_version_is_grandfathered(self):
        for version in ("0.76.0", "0.76.1", "0.77.1", "0.78.0"):
            candidate = config(version, url=PRODUCTION_MCP_URL +
                               f"?memhub_plugin_version={version}", headers={})
            errors = connection_errors(candidate, version, PRODUCTION_MCP_URL)
            self.assertEqual(bool(errors), version != "0.76.1")

    def test_new_versions_reject_release_or_channel_queries(self):
        for query in ("?memhub_plugin_version=0.77.1", "?client=codex"):
            candidate = config("0.77.1", url=PRODUCTION_MCP_URL + query)
            self.assertTrue(connection_errors(candidate, "0.77.1", PRODUCTION_MCP_URL))

    def test_missing_stale_or_ambiguous_headers_fail(self):
        for headers in ({}, {"X-MemHub-Plugin-Version": "0.76.1"},
                        {"X-MemHub-Plugin-Version": "0.77.1",
                         "x-memhub-plugin-version": "0.76.1"}):
            self.assertTrue(connection_errors(config("0.77.1", headers=headers),
                                              "0.77.1", PRODUCTION_MCP_URL))


if __name__ == "__main__":
    unittest.main()
