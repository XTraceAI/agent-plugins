"""Client-side text encryption backed exclusively by the XTrace SDK.

There is intentionally no key exchange yet.  The caller supplies the same
passphrase on the machine that encrypts and the machine that decrypts via
``MEMHUB_ENCRYPTION_PASSPHRASE``.  The passphrase never enters an MCP request.

The prefix below is only a versioned container marker.  Everything after it is
the byte-for-byte ASCII output of ``xtrace_sdk``'s ``AESClient.encrypt``.  This
module does not implement AES, key derivation, nonce generation, padding, or
authentication; those operations stay in the audited SDK.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

PASSPHRASE_ENV = "MEMHUB_ENCRYPTION_PASSPHRASE"
ENVELOPE_PREFIX = "xtrace:aes256gcm:v1:"
ENCRYPTED_ARTIFACT_TAG = "xtrace-encrypted:v1"


class EncryptionConfigurationError(RuntimeError):
    """The local encryption dependency or passphrase is unavailable."""


class EncryptedTextError(ValueError):
    """An encrypted-text envelope is invalid or cannot be authenticated."""


def is_encrypted_text(value: object) -> bool:
    """Return whether *value* is a v1 XTrace encrypted-text envelope."""
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def _new_sdk_client(passphrase: str):
    """Construct the SDK's AES client without implementing crypto locally."""
    try:
        from xtrace_sdk.x_vec.crypto.encryption.aes import AESClient
        from xtrace_sdk.x_vec.crypto.key_provider import PassphraseKeyProvider
    except (ImportError, ModuleNotFoundError) as exc:
        raise EncryptionConfigurationError(
            "client-side encryption requires xtrace-ai-sdk==0.1.1; run this "
            "script with `uv run --with mcp --with xtrace-ai-sdk==0.1.1`"
        ) from exc

    # The SDK owns both scrypt key derivation and AES-256-GCM construction.
    provider = PassphraseKeyProvider(passphrase)
    return AESClient(provider.get_key())


class XTraceTextCipher:
    """Encrypt and decrypt versioned text envelopes using ``xtrace_sdk``."""

    def __init__(self, passphrase: str) -> None:
        if not isinstance(passphrase, str) or not passphrase:
            raise EncryptionConfigurationError("the encryption passphrase must not be empty")
        self._client = _new_sdk_client(passphrase)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None,
    ) -> "XTraceTextCipher":
        """Build a cipher from ``MEMHUB_ENCRYPTION_PASSPHRASE``."""
        source = os.environ if environ is None else environ
        passphrase = source.get(PASSPHRASE_ENV)
        if not passphrase:
            raise EncryptionConfigurationError(
                f"{PASSPHRASE_ENV} is required for encrypted artifact operations; "
                "set it to the same secret when saving and loading"
            )
        return cls(passphrase)

    def encrypt(self, plaintext: str) -> str:
        """Return a versioned envelope containing SDK-produced ciphertext."""
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be a string")
        encrypted = self._client.encrypt(plaintext)
        try:
            payload = encrypted.decode("ascii")
        except (AttributeError, UnicodeError) as exc:
            raise EncryptedTextError(
                "xtrace_sdk returned an unsupported ciphertext representation"
            ) from exc
        return f"{ENVELOPE_PREFIX}{payload}"

    def decrypt(self, envelope: str) -> str:
        """Authenticate and decrypt a v1 envelope using the XTrace SDK."""
        if not is_encrypted_text(envelope):
            raise EncryptedTextError(
                f"content is not a supported encrypted envelope ({ENVELOPE_PREFIX}...)"
            )
        payload = envelope[len(ENVELOPE_PREFIX):]
        if not payload:
            raise EncryptedTextError("encrypted envelope contains no ciphertext")
        try:
            return self._client.decrypt(payload.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            # AES-GCM authentication failures, malformed base64, and wrong keys
            # all fail closed with one stable plugin-level error.
            raise EncryptedTextError(
                "ciphertext authentication failed (wrong passphrase or corrupted content)"
            ) from exc
