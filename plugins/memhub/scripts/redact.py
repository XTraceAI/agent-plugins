#!/usr/bin/env python3
"""Strip MemHub credentials out of transcript records before they are shipped.

**Why this is a prerequisite and not a nicety.** This plugin records terminal
sessions and uploads them into a shared team brain, where they are indexed,
searched by colleagues, and distilled into extracted facts. The moment we also
tell people to hold a long-lived ``mhk_`` key, the obvious things they will do —
``export MEMHUB_TOKEN=mhk_…``, ``cat`` the config, paste a key to a teammate —
happen inside exactly the buffer we capture. Without this, onboarding advice and
the capture pipeline combine into a credential leak with our name on it, and the
leak lands somewhere durable and shared rather than scrolling away.

**Scoped to what can be recognised for certain.** Only credentials with a
documented, distinctive prefix are matched — ``mhk_`` (personal access keys) and
``xtk_`` (Memory API data-plane keys). Bearer JWTs are deliberately NOT matched:
they are unprefixed base64url, indistinguishable by shape from ordinary content
like a transcript uuid or a chunk of encoded payload, and a redactor that eats
real content teaches people to distrust the archive. The prefixed keys are the
ones this feature puts on disk, and they are the ones it removes.

Structure-preserving: strings are rewritten in place, everything else is walked,
so a redacted record is still the same record. Never raises — a redaction bug
must not become a capture outage. Failing closed would be worse than shipping
the batch, so on error the caller keeps the original and this returns it.

Run the self-test:  python3 tests/redact_test.py  (from the repo root; tests
live outside the plugin so they are not shipped to installs)
"""
from __future__ import annotations

import re

# ``mhk_`` secrets observed at 47 chars total; ``{16,}`` after the prefix is
# comfortably below that while still far too long to collide with prose that
# merely mentions the prefix (e.g. "keys look like mhk_…" in docs).
_SECRET = re.compile(r"\b((?:mhk|xtk)_)[A-Za-z0-9_\-]{16,}")

PLACEHOLDER = r"\1<redacted>"


def redact_text(text: str) -> str:
    """Replace any MemHub key in ``text``, keeping the prefix as a breadcrumb.

    The prefix survives so a reader can tell a credential WAS here — which is
    what makes an accidental paste visible and fixable — without the value.
    """
    return _SECRET.sub(PLACEHOLDER, text)


def redact(value):
    """Recursively redact a JSON-shaped value, preserving its structure.

    Dict KEYS are rewritten too. They are almost never secrets, but a key is
    just as capturable as a value, and skipping them would leave an obvious
    hole in a guarantee whose whole worth is being unconditional.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, dict):
        return {(redact_text(k) if isinstance(k, str) else k): redact(v)
                for k, v in value.items()}
    return value


def redact_records(records: list) -> list:
    """Redact a batch of transcript records. Never raises.

    On any unexpected failure the records are returned untouched: capture
    stopping is a worse outcome than this pass not running, and the caller has
    no better option to fall back to.
    """
    try:
        return [redact(r) for r in records]
    except Exception:  # noqa: BLE001 — never break capture over redaction
        return records


def contains_secret(value) -> bool:
    """True if a secret survives anywhere in ``value`` — for tests and checks."""
    if isinstance(value, str):
        return bool(_SECRET.search(value))
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v) for v in value)
    if isinstance(value, dict):
        return any(contains_secret(k) or contains_secret(v)
                   for k, v in value.items())
    return False
