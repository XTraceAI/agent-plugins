"""Client-side text encryption backed exclusively by the XTrace SDK.

There is intentionally no key exchange yet.  The caller supplies the same
passphrase on the machine that encrypts and the machine that decrypts.  It is
resolved from ``MEMHUB_ENCRYPTION_PASSPHRASE`` first, then from a private local
``.env`` file.  The passphrase never enters an MCP request.

The prefix below is only a versioned container marker.  Everything after it is
the byte-for-byte ASCII output of ``xtrace_sdk``'s ``AESClient.encrypt``.  This
module does not implement AES, key derivation, nonce generation, padding, or
authentication; those operations stay in the audited SDK.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from os import PathLike
from pathlib import Path

PASSPHRASE_ENV = "MEMHUB_ENCRYPTION_PASSPHRASE"
PASSPHRASE_FILE_ENV = "MEMHUB_ENCRYPTION_ENV_FILE"
DEFAULT_PASSPHRASE_FILE = Path.home() / ".config" / "memhub-plugin" / ".env"
ENVELOPE_PREFIX = "xtrace:aes256gcm:v1:"
ENCRYPTED_ARTIFACT_TAG = "xtrace-encrypted:v1"


class EncryptionConfigurationError(RuntimeError):
    """The local encryption dependency or passphrase is unavailable."""


class EncryptedTextError(ValueError):
    """An encrypted-text envelope is invalid or cannot be authenticated."""


def is_encrypted_text(value: object) -> bool:
    """Return whether *value* is a v1 XTrace encrypted-text envelope."""
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def _passphrase_from_file(path: Path) -> str:
    """Read only the passphrase key from a private dotenv file.

    ``dotenv_values`` deliberately avoids copying unrelated values into the
    process environment.  Interpolation is disabled because characters such
    as ``${...}`` may legitimately be part of a passphrase.
    """
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise EncryptionConfigurationError(
            f"encryption passphrase file not found: {path}; set {PASSPHRASE_ENV} "
            f"or create the file with mode 0600"
        ) from exc
    except OSError as exc:
        raise EncryptionConfigurationError(
            f"cannot inspect encryption passphrase file {path}: {exc}"
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise EncryptionConfigurationError(
            f"encryption passphrase path is not a regular file: {path}"
        )
    if os.name == "posix" and file_stat.st_mode & 0o077:
        raise EncryptionConfigurationError(
            f"encryption passphrase file {path} is accessible by group/others; "
            f"run `chmod 600 {path}`"
        )

    try:
        from dotenv import dotenv_values
    except (ImportError, ModuleNotFoundError) as exc:
        raise EncryptionConfigurationError(
            "loading the passphrase .env file requires python-dotenv, which is "
            "included with xtrace-ai-sdk==0.1.1"
        ) from exc

    try:
        values = dotenv_values(path, interpolate=False)
    except (OSError, UnicodeError) as exc:
        raise EncryptionConfigurationError(
            f"cannot read encryption passphrase file {path}: {exc}"
        ) from exc
    passphrase = values.get(PASSPHRASE_ENV)
    if not isinstance(passphrase, str) or not passphrase:
        raise EncryptionConfigurationError(
            f"{path} does not define a non-empty {PASSPHRASE_ENV}"
        )
    return passphrase


def _new_sdk_client(passphrase: str):
    """Construct the SDK's AES client without implementing crypto locally."""
    try:
        from xtrace_sdk.x_vec.crypto.encryption.aes import AESClient
        from xtrace_sdk.x_vec.crypto.key_provider import PassphraseKeyProvider
    except (ImportError, ModuleNotFoundError) as exc:
        raise EncryptionConfigurationError(
            "client-side encryption requires xtrace-ai-sdk==0.1.1; run this "
            "script with `uv run --with 'mcp<2' --with xtrace-ai-sdk==0.1.1`"
        ) from exc

    # The SDK owns both scrypt key derivation and AES-256-GCM construction.
    provider = PassphraseKeyProvider(passphrase)
    return AESClient(provider.get_key())


class XTraceTextCipher:
    """Encrypt and decrypt versioned text envelopes using ``xtrace_sdk``."""

    def __init__(self, passphrase: str) -> None:
        if not isinstance(passphrase, str) or not passphrase:
            raise EncryptionConfigurationError(
                "the encryption passphrase must not be empty"
            )
        self._client = _new_sdk_client(passphrase)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        env_file: str | PathLike[str] | None = None,
    ) -> XTraceTextCipher:
        """Build a cipher from the process environment or a private ``.env``.

        Resolution order is the direct passphrase environment variable, an
        explicit *env_file*, ``MEMHUB_ENCRYPTION_ENV_FILE``, then the stable
        per-user default at ``~/.config/memhub-plugin/.env``.
        """
        source = os.environ if environ is None else environ
        passphrase = source.get(PASSPHRASE_ENV)
        if passphrase:
            return cls(passphrase)

        configured_path = env_file or source.get(PASSPHRASE_FILE_ENV)
        path = (
            Path(configured_path).expanduser()
            if configured_path
            else DEFAULT_PASSPHRASE_FILE
        )
        return cls(_passphrase_from_file(path))

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
        payload = envelope[len(ENVELOPE_PREFIX) :]
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
