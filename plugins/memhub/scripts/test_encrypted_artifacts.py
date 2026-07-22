#!/usr/bin/env python3
"""Offline tests for the opt-in encrypted artifact path.

Run with the real XTrace crypto implementation:

    uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
      python plugins/memhub/scripts/test_encrypted_artifacts.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _memhub_crypto import (  # noqa: E402
    ENCRYPTED_ARTIFACT_TAG,
    ENVELOPE_PREFIX,
    PASSPHRASE_ENV,
    EncryptedTextError,
    EncryptionConfigurationError,
    XTraceTextCipher,
    is_encrypted_text,
)
from load_encrypted_artifact import (  # noqa: E402
    _artifact_content,
    _artifact_id_argument,
    _get_artifact_tool,
)
from save_artifact import _prepare_content  # noqa: E402


class TextCipherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cipher = XTraceTextCipher("correct horse battery staple")

    def test_round_trip_text_shapes(self) -> None:
        samples = [
            "",
            "ordinary text",
            "private \N{LOCK} Unicode: Zażółć gęślą jaźń / 数据",
            "line one\nline two\n",
            "x" * 100_000,
        ]
        for plaintext in samples:
            with self.subTest(length=len(plaintext)):
                envelope = self.cipher.encrypt(plaintext)
                self.assertTrue(is_encrypted_text(envelope))
                self.assertEqual(self.cipher.decrypt(envelope), plaintext)

    def test_ciphertext_does_not_contain_plaintext(self) -> None:
        plaintext = "never send this plaintext phrase to XTrace!"
        envelope = self.cipher.encrypt(plaintext)
        self.assertTrue(envelope.startswith(ENVELOPE_PREFIX))
        self.assertNotIn(plaintext, envelope)

    def test_fresh_nonce_changes_ciphertext(self) -> None:
        plaintext = "same input, independent authenticated encryption"
        self.assertNotEqual(self.cipher.encrypt(plaintext), self.cipher.encrypt(plaintext))

    def test_wrong_passphrase_fails_closed(self) -> None:
        envelope = self.cipher.encrypt("classified")
        wrong = XTraceTextCipher("definitely the wrong passphrase")
        with self.assertRaisesRegex(EncryptedTextError, "authentication failed"):
            wrong.decrypt(envelope)

    def test_tampering_fails_closed(self) -> None:
        envelope = self.cipher.encrypt("authenticated payload")
        offset = len(ENVELOPE_PREFIX)
        replacement = "A" if envelope[offset] != "A" else "B"
        tampered = envelope[:offset] + replacement + envelope[offset + 1:]
        with self.assertRaisesRegex(EncryptedTextError, "authentication failed"):
            self.cipher.decrypt(tampered)

    def test_non_envelope_and_empty_payload_are_rejected(self) -> None:
        with self.assertRaisesRegex(EncryptedTextError, "not a supported"):
            self.cipher.decrypt("plaintext")
        with self.assertRaisesRegex(EncryptedTextError, "no ciphertext"):
            self.cipher.decrypt(ENVELOPE_PREFIX)

    def test_passphrase_must_be_configured(self) -> None:
        with self.assertRaisesRegex(
            EncryptionConfigurationError, "MEMHUB_ENCRYPTION_PASSPHRASE",
        ):
            XTraceTextCipher.from_env({})


class ArtifactResponseTests(unittest.TestCase):
    def test_get_artifact_tool_and_live_id_schema(self) -> None:
        other = SimpleNamespace(name="search_memory", inputSchema={})
        target = SimpleNamespace(
            name="get_artifact",
            inputSchema={"properties": {"artifactId": {"type": "string"}}},
        )
        self.assertIs(_get_artifact_tool(SimpleNamespace(tools=[other, target])), target)
        self.assertEqual(_artifact_id_argument(target), "artifactId")

    def test_snake_case_id_is_preferred(self) -> None:
        tool = SimpleNamespace(
            inputSchema={"properties": {"id": {}, "artifact_id": {}}},
        )
        self.assertEqual(_artifact_id_argument(tool), "artifact_id")

    def test_content_is_found_through_fastmcp_wrappers(self) -> None:
        envelope = self.ciphertext_fixture()
        payload = {"result": {"data": {"artifact": {"content": envelope}}}}
        self.assertEqual(_artifact_content(payload), envelope)

    @staticmethod
    def ciphertext_fixture() -> str:
        return f"{ENVELOPE_PREFIX}opaque-base64-from-sdk"


class UploadBoundaryTests(unittest.TestCase):
    def test_encrypted_body_is_the_exact_mcp_value(self) -> None:
        plaintext = "this exact body must never enter the MCP request"
        passphrase = "upload-boundary-test-passphrase"
        with patch.dict(os.environ, {PASSPHRASE_ENV: passphrase}):
            stored, tags = _prepare_content(plaintext, "private,private", True)

        self.assertTrue(is_encrypted_text(stored))
        self.assertNotIn(plaintext, stored)
        self.assertEqual(tags.count(ENCRYPTED_ARTIFACT_TAG), 1)
        self.assertEqual(XTraceTextCipher(passphrase).decrypt(stored), plaintext)

    def test_plaintext_path_is_unchanged(self) -> None:
        content = "ordinary artifact"
        stored, tags = _prepare_content(content, "spec,team", False)
        self.assertEqual(stored, content)
        self.assertEqual(tags, ["spec", "team"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
