#!/usr/bin/env python3
"""HTTPS trust for the plugin's stdlib transport — stdlib only.

**Why this exists.** A python.org build of Python on macOS ships with an EMPTY
OpenSSL trust store until its "Install Certificates.command" has been run, and
nothing in a plain ``python3`` invocation says so. Every HTTPS call the plugin
makes then fails with ``certificate verify failed: unable to get local issuer
certificate`` — while curl, git and the browser on the same machine work,
because they read the system keychain. It first bit on a teammate's first
install: the plugin installed cleanly and the sign-in step died on the spot.
The same shape appears behind a corporate TLS proxy whose root was added to
the keychain but never to Python's bundle.

**What it does.** Verification is never relaxed. The default context —
Python's own bundle, with ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` honoured as
always — is used first. Only when that store holds no roots at all, or a
connection fails on CERTIFICATE VERIFICATION (a handshake-time failure: no
request bytes have been sent, so retrying is safe even for a POST), is the
context augmented with roots the machine already trusts and the call retried
once: ``certifi`` when installed, the OS bundle on Linux, and on macOS the
keychain roots exported by ``security find-certificate`` (system roots,
admin-added roots and the login keychain, so a corporate root works too).
The outcome is remembered for the rest of the process, so a hook pays the
probe at most once — and on a healthy machine pays nothing, not even the
``security`` subprocess.

Every network call in the plugin goes through here: ``mcp_http`` builds its
redirect-refusing opener with :class:`Opener`, and the few bare ``urlopen``
sites call :func:`urlopen`.

Run the self-test:  python3 tests/tls_test.py
"""
from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import urllib.request

# Bundles a distro installs. Each that exists is loaded; duplicates are harmless.
_LINUX_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian / Ubuntu / Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL / Fedora
    "/etc/ssl/ca-bundle.pem",               # openSUSE
    "/etc/ssl/cert.pem",                    # macOS /usr/bin/python3, the BSDs
)

# macOS keychains whose certificates `security` exports without a prompt.
# Certificates are public, so reading them never touches the keychain's secrets.
_MAC_KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
    os.path.expanduser("~/Library/Keychains/login.keychain-db"),
)

_default_ctx: ssl.SSLContext | None = None
_augmented_ctx: ssl.SSLContext | None = None
_augmented_tried = False
_prefer_augmented = False   # flipped once the default store failed and the extra roots cured it


def default_context() -> ssl.SSLContext:
    """Python's own trust: ``create_default_context()``, built once."""
    global _default_ctx
    if _default_ctx is None:
        _default_ctx = ssl.create_default_context()
    return _default_ctx


def extra_roots() -> list[tuple[str, dict]]:
    """Trust roots beyond Python's bundle, as ``(label, load_verify_locations kwargs)``.

    Only sources the machine already trusts for everything else — never a root
    the plugin ships — so this cannot widen what the user trusts."""
    roots: list[tuple[str, dict]] = []
    try:
        import certifi  # noqa: PLC0415 — optional; present with pip/httpx, absent on a bare python
        roots.append(("certifi", {"cafile": certifi.where()}))
    except Exception:  # noqa: BLE001 — absent or broken: the next source may do
        pass
    for path in _LINUX_BUNDLES:
        if os.path.isfile(path):
            roots.append((path, {"cafile": path}))
    if sys.platform == "darwin":
        for keychain in _MAC_KEYCHAINS:
            if not os.path.exists(keychain):
                continue
            try:
                out = subprocess.run(
                    ["security", "find-certificate", "-a", "-p", keychain],
                    capture_output=True, text=True, timeout=10, check=False).stdout
            except Exception:  # noqa: BLE001 — no `security`, or it hung: skip this keychain
                continue
            if "BEGIN CERTIFICATE" in out:
                roots.append((keychain, {"cadata": out}))
    return roots


def augmented_context() -> ssl.SSLContext | None:
    """The default context plus every extra root that loads; ``None`` when none did.

    Built at most once per process: the keychain export is the only cost here,
    and it is only ever paid after the default store has already failed."""
    global _augmented_ctx, _augmented_tried
    if not _augmented_tried:
        _augmented_tried = True
        ctx = ssl.create_default_context()
        before = ctx.cert_store_stats()["x509"]
        for _label, kwargs in extra_roots():
            try:
                ctx.load_verify_locations(**kwargs)
            except Exception:  # noqa: BLE001 — one unparsable source must not sink the rest
                continue
        if ctx.cert_store_stats()["x509"] > before:
            _augmented_ctx = ctx
    return _augmented_ctx


def is_cert_failure(exc: BaseException) -> bool:
    """True when ``exc`` is, wraps (``URLError.reason``) or was caused by a
    certificate-verification failure. Anything else — a timeout, a refused
    connection, an HTTP status — is not ours to retry."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if isinstance(cur, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(cur):
            return True
        for link in (getattr(cur, "reason", None), cur.__cause__, cur.__context__):
            if isinstance(link, BaseException):
                stack.append(link)
    return False


class Opener:
    """An ``OpenerDirector`` under the trust policy above.

    ``handlers`` go to ``urllib.request.build_opener`` (classes or instances) —
    an ``HTTPSHandler`` carrying the chosen context is appended, which replaces
    the default one. ``open`` mirrors ``OpenerDirector.open``. The cure is
    remembered module-wide, so every Opener in the process benefits from the
    first one that found it."""

    def __init__(self, *handlers) -> None:
        self._handlers = handlers
        self._by_ctx: dict[int, urllib.request.OpenerDirector] = {}

    def _opener(self, ctx: ssl.SSLContext) -> urllib.request.OpenerDirector:
        opener = self._by_ctx.get(id(ctx))
        if opener is None:
            opener = urllib.request.build_opener(
                *self._handlers, urllib.request.HTTPSHandler(context=ctx))
            self._by_ctx[id(ctx)] = opener
        return opener

    @property
    def handlers(self) -> list:
        """The handlers of the opener in use, as ``OpenerDirector.handlers``."""
        return self._opener(default_context()).handlers

    def open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        global _prefer_augmented
        if not _prefer_augmented and default_context().cert_store_stats()["x509"] == 0:
            # An empty store verifies nothing: skip the handshake that would fail.
            _prefer_augmented = augmented_context() is not None
        if _prefer_augmented:
            aug = augmented_context()
            if aug is not None:
                return self._opener(aug).open(fullurl, data, timeout)
        try:
            return self._opener(default_context()).open(fullurl, data, timeout)
        except OSError as exc:   # URLError is an OSError; so is a bare SSLError
            if not is_cert_failure(exc):
                raise
            aug = augmented_context()
            if aug is None:
                raise
            resp = self._opener(aug).open(fullurl, data, timeout)
            _prefer_augmented = True
            return resp


_DEFAULT = Opener()


def urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """``urllib.request.urlopen`` under the plugin's trust policy, no extra handlers."""
    return _DEFAULT.open(url, data, timeout)
