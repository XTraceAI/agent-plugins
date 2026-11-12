#!/usr/bin/env python3
"""Strip credentials and personal data out of what the plugin uploads, on this
machine, before it is sent.

**Why here and not only on the server.** This plugin records terminal sessions
and uploads them into a shared team brain, where they are indexed, searched by
colleagues and distilled into facts. Tool output is captured verbatim, so a
secret a command printed (`aws lambda get-function-configuration`, a `cat` of an
env file, a pasted key) is in the buffer we ship. MemHub's server redacts at
ingest (MemHub-Backend `app/core/redaction.py`, ENG-1312), but by then the value
has left the machine and crossed our edge. Redacting here means it never does.

**The same rules as the server, on purpose.** The section "the server's rules"
below is that module's body, ported as it stands (origin/staging `f57dacac0`,
last changed by `10af40ac9`): vendor-prefixed keys, JWTs, DSN passwords, PEM
private keys, values assigned to secret-named keys, and context-free
high-entropy tokens (Shannon entropy), plus, at capture strength, standalone
32+ character hex tokens and basic PII. Every function is deterministic and
idempotent, so the server's own pass over an already-redacted upload changes
nothing, and a server that is ahead of this copy still catches what this one
misses. Keep the two in step: a rule added there belongs here too.

Two strengths, as on the server:

- `redact_records` — transcript records about to be uploaded: capture strength
  (`redact_capture`) on every free-text string, skipping identifier and enum
  keys (record uuids, tool-call ids, `cwd`, `gitBranch`) and base64 media, which
  dedup, tool-card pairing and repo identity depend on.
- `redact_text` / `redact` — any other text the plugin sends (titles, agent
  arguments, captured documents, a model-bound window): `redact_secrets`. No
  hex or PII rule here: a captured spec may quote a commit SHA or an address on
  purpose.

A redacted prefixed key keeps its prefix (`mhk_<redacted>`) so a reader can tell
a credential WAS there, which is what makes an accidental paste visible and
fixable, without the value.

Structure-preserving: strings are rewritten in place, everything else is walked,
so a redacted record is still the same record. Never raises — a redaction bug
must not become a capture outage. Failing closed would be worse than shipping
the batch, so on error the caller keeps the original and this returns it.

Run the self-test:  python3 tests/redact_test.py  (from the repo root; tests
live outside the plugin so they are not shipped to installs)
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Any, Callable

# ``mhk_`` secrets observed at 47 chars total; ``{16,}`` after the prefix is
# comfortably below that while still far too long to collide with prose that
# merely mentions the prefix (e.g. "keys look like mhk_…" in docs).
_SECRET = re.compile(r"\b((?:mhk|xtk)_)[A-Za-z0-9_\-]{16,}")

PLACEHOLDER = r"\1<redacted>"

# ── the server's rules ──────────────────────────────────────────────────────
# MemHub-Backend app/core/redaction.py, ported as it stands (see the module
# docstring). Change a rule there first, then here.

REDACTED = "<redacted>"

# --- vendor-prefixed credentials: keep the prefix as a breadcrumb ----------
_PREFIXED = [
    re.compile(r"\b((?:mhk|xtk)_)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{30,}"),
    re.compile(r"\b(github_pat_)[A-Za-z0-9_]{22,}"),
    re.compile(r"\b((?:sk|rk|pk)_(?:live|test)_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(whsec_)[A-Za-z0-9+/=]{16,}"),
    re.compile(r"\b(xox[abprse]-)[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(ya29\.)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(AIza)[A-Za-z0-9_\-]{35}"),
    re.compile(r"\b(glpat-)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(npm_)[A-Za-z0-9]{36}"),
    re.compile(r"\b(hf_)[A-Za-z0-9]{30,}"),
    re.compile(r"\b(sbp_)[a-f0-9]{40}"),
    re.compile(r"\b(SG\.)[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(https://hooks\.slack\.com/services/)[A-Za-z0-9/]+"),
]
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
_DSN = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@'\"]+:)(?!<redacted)[^\s@/'\"]+(@)")
# ENV-style name: any value with a letter (the name is the signal).
_ASSIGN = re.compile(
    r"(\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APP_KEY|AUTH_KEY|PRIVATE_KEY"
    r"|ACCESS_KEY|CREDENTIALS?)[A-Z0-9_]*(?:\\?[\"'])?\s*[=:]\s*(?:\\?[\"'])?)(?![$<])"
    # a value that already holds a breadcrumb (``sk-proj-<redacted>``) is done;
    # re-redacting it would erase the vendor prefix ``has_secret`` reads.
    r"(?![^\s\"'#,;\\]*<redacted)"
    r"(?=[^\s\"'#,;\\]*[A-Za-z])[^\s\"'#,;\\]{8,}"
)
# json / yaml / kwarg style name, any case: entropy-gated (``token = self.token``
# and ``password: getpass()`` are code, not secrets).
_ASSIGN_ANYCASE = re.compile(
    r"(?i)(\b[a-z0-9_.\-]*(?:secret|token|passw(?:or)?d|api[_\-]?key|apikey|app[_\-]?key|auth[_\-]?key"
    r"|private[_\-]?key|access[_\-]?key|credentials?)(?:\\?[\"'])?\s*[=:]\s*(?:\\?[\"'])?)"
    r"(?!<redacted)([A-Za-z0-9+/=_\-.~]{8,})(?=$|[\s\"'\\,;})\]])"
)
_PEM = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE\s+KEY-----)"
    # Terminated: up to the END line (bounded, so a truncated key cannot swallow
    # everything up to some later key's END). Truncated output (``head``, a
    # cut-off tool result): the base64 lines that follow, escaped or not.
    r"(?:[\s\S]{0,12000}?(-----END [A-Z ]*PRIVATE\s+KEY-----)"
    r"|(?:(?:\s|\\n|\\r)+(?:[A-Za-z\-]+: [^\n\\]*|[A-Za-z0-9+/=]{16,}))+)"
)

# --- context-free entropy -----------------------------------------------------
_B64_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_\-<])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/=_\-])")
_HEX_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])(?:[0-9a-f]{32,}|[0-9A-F]{32,})(?![A-Za-z0-9_\-])")
#: Integrity hashes (``sha512-…`` in lockfiles) are public digests, not secrets.
_DIGEST_PREFIX = re.compile(r"(?:sha(?:1|256|384|512)[-:])$")
_DIGEST_TOKEN = re.compile(r"sha(?:1|256|384|512)-")
#: Dictionary-ish runs (``Backend``, ``capture``). A random token rarely has
#: three lowercase letters in a row, so when such runs cover most of a token it
#: is an identifier (``MemHubBackendCaptureRedaction2026``), not a secret.
_WORDISH = re.compile(r"[A-Z]?[a-z]{3,}")
_ENTROPY_HEX_MIN = 3.0
#: Longer runs are encoded payloads (images, archives), not credentials; PEM
#: keys, the one long secret, have their own rule.
_B64_MAX = 1024
#: A token continuing a path (``/Users/me/.claude/…``) or a data URI.
_PATH_OR_DATA_BEFORE = re.compile(r"(?:[/.~]|;base64,)$")
_ENTROPY_ANYCASE_MIN = 3.0

# --- PII ------------------------------------------------------------------------
_PII_EMAIL = re.compile(r"(?<![\w.+\-])(?!git@)[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"(?<![\d\-])\d{3}-\d{2}-\d{4}(?![\d\-])")
#: A card number is contiguous digits or consistently grouped (4-4-4-4…, Amex
#: 4-6-5); ``1234567890123 456`` is two numbers, not a card.
_CARD = re.compile(
    r"(?<![\d\-])(?=4|5[1-5]|2[2-7]|3[47]|6(?:011|5))"
    r"(?:\d{13,19}|\d{4}([ \-])\d{4}\1\d{4}\1\d{1,7}|\d{4}([ \-])\d{6}\2\d{5})(?![\d\-])"
)
_PHONE = re.compile(
    r"(?<![\w\-.])(?:\+?1[ .\-]?)?(?:\(\d{3}\)\s?|\d{3}[ .\-])\d{3}[ .\-]\d{4}(?![\w\-.])"
    r"|(?<![\w\-.])\+\d{1,3}[ .\-]\d{1,4}(?:[ .\-]\d{2,4}){2,4}(?![\w\-.])"
)


def shannon_entropy(s: str) -> float:
    """Bits per character of ``s``'s own character distribution."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _b64_threshold(n: int) -> float:
    """Entropy a random base62 string of length ``n`` reliably clears.

    A sample's entropy underestimates the alphabet's (finite-sample bias,
    roughly ``(k-1) / (2 n ln 2)``); this is that expectation for k=62 less a
    0.8-bit margin, floored at 4.0 (a DER private-key line scores ~4.6 at n=64,
    so a tighter margin misses it). n=32 → 4.0, n=64 → ~4.5.
    """
    expected = math.log2(62) - 61 / (2 * n * math.log(2))
    return max(4.0, expected - 0.8)


def _looks_like_b64_secret(tok: str) -> bool:
    body = tok.rstrip("=")
    if not (any(c.isupper() for c in body) and any(c.islower() for c in body)
            and any(c.isdigit() for c in body)):
        return False
    # Paths and slugs: many separators splitting real words.
    if body.count("/") > len(body) // 16 + 1:
        return False
    if sum(len(w) for w in _WORDISH.findall(body)) >= 0.6 * len(body):
        return False
    parts = [p for p in re.split(r"[/_\-]", body) if p]
    if len(parts) >= 3 and sum(p.isalpha() and len(p) >= 3 for p in parts) * 2 >= len(parts):
        return False
    return shannon_entropy(body) >= _b64_threshold(len(body))


def _sub_b64(m: re.Match) -> str:
    tok = m.group(0)
    if (len(tok) > _B64_MAX or _DIGEST_TOKEN.match(tok)
            or _DIGEST_PREFIX.search(m.string, 0, m.start())
            or _PATH_OR_DATA_BEFORE.search(m.string, 0, m.start())):
        return tok
    return REDACTED if _looks_like_b64_secret(tok) else tok


def _sub_hex(m: re.Match) -> str:
    tok = m.group(0)
    if _DIGEST_PREFIX.search(m.string, 0, m.start()):
        return tok
    return REDACTED if shannon_entropy(tok) >= _ENTROPY_HEX_MIN else tok


def _sub_anycase(m: re.Match) -> str:
    val = m.group(2)
    if val.startswith(("$", "<")):
        return m.group(0)
    strong = any(c.isdigit() for c in val) or shannon_entropy(val) >= 3.5
    if strong and shannon_entropy(val) >= _ENTROPY_ANYCASE_MIN:
        return m.group(1) + REDACTED
    return m.group(0)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _sub_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "<card>" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def redact_secrets(text: str) -> str:
    """Remove high-confidence credentials, keeping a breadcrumb of where they were."""
    if not text:
        return text
    for pattern in _PREFIXED:
        text = pattern.sub(r"\1" + REDACTED, text)
    text = _PEM.sub(lambda m: m.group(1) + REDACTED + (m.group(2) or ""), text)
    text = _JWT.sub("<redacted-jwt>", text)
    text = _DSN.sub(r"\1" + REDACTED + r"\2", text)
    text = _ASSIGN.sub(r"\1" + REDACTED, text)
    text = _ASSIGN_ANYCASE.sub(_sub_anycase, text)
    return _B64_TOKEN.sub(_sub_b64, text)


#: What :func:`redact_secrets` leaves where a vendor key, JWT or PEM key was.
_BREADCRUMB = re.compile(
    r"(?:\b(?:mhk|xtk)_|\bsk-(?:ant-|proj-)?|\bgh[pousr]_|\bgithub_pat_|\b(?:sk|rk|pk)_(?:live|test)_"
    r"|\bwhsec_|\bxox[abprse]-|\b(?:AKIA|ASIA)|\bya29\.|\bAIza|\bglpat-|\bnpm_|\bhf_|\bsbp_|\bSG\."
    r"|hooks\.slack\.com/services/|PRIVATE\s+KEY-----)<redacted>|<redacted-jwt>"
)


def has_secret(text: str) -> bool:
    """True when ``text`` holds a high-confidence credential — raw, or already
    redacted (its breadcrumb). Vendor-prefixed keys, JWTs and PEM private keys
    only: assignment and entropy hits are too soft to report as an incident.

    Capture redacts at ingest, so the session-signals ``secret_pattern``
    detector only ever sees breadcrumbs on new rows; matching them keeps that
    signal alive.
    """
    if not text:
        return False
    return (any(p.search(text) for p in _PREFIXED) or bool(_JWT.search(text))
            or bool(_PEM.search(text)) or bool(_BREADCRUMB.search(text)))


def redact_pii(text: str) -> str:
    """Mask emails, card numbers, US SSNs and phone numbers."""
    if not text:
        return text
    text = _PII_EMAIL.sub("<email>", text)
    text = _CARD.sub(_sub_card, text)
    text = _SSN.sub("<ssn>", text)
    return _PHONE.sub("<phone>", text)


def redact_capture(text: str) -> str:
    """Ingest-strength redaction: secrets, standalone hex tokens, and PII."""
    if not text:
        return text
    text = redact_secrets(text)
    text = _HEX_TOKEN.sub(_sub_hex, text)
    return redact_pii(text)


#: JSON keys whose string values are identifiers or enums, never free text.
#: Rewriting them would break dedup (record uuids), tool-card pairing
#: (tool-call ids), thinking-block replay (signature) or provenance.
_SKIP_KEYS = frozenset({
    "type", "role", "state", "status", "mode", "model", "resolvedModel", "requestedModel",
    "signature", "timestamp", "time_stamp", "version", "cwd", "gitBranch", "branch",
    "userType", "entrypoint", "name", "subagent_type", "media_type", "stop_reason",
    "permissionMode", "service_tier", "speed", "effort", "inference_geo",
})
_ID_KEY = re.compile(r"^(?:id|.*(?:_id|Id|ID|_ids|Ids|uuid|Uuid|UUID))$")


def _skip_key(key: Any) -> bool:
    return isinstance(key, str) and (key in _SKIP_KEYS or bool(_ID_KEY.match(key)))


def redact_json(value: Any, redact: Callable[[str], str] = redact_secrets) -> Any:  # noqa: A002
    """Return ``value`` with every free-text string leaf passed through ``redact``.

    Never mutates the input: a container is rebuilt only when something inside
    it changed, so a clean transcript comes back as the very same object. Base64 media blocks
    (``{"type": "base64", "data": …}``) are left alone: they are image bytes,
    not text, and the entropy rule would otherwise erase them.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        out = [redact_json(v, redact) for v in value]
        return value if all(a is b for a, b in zip(out, value)) else out
    if isinstance(value, dict):
        is_media = value.get("type") == "base64" and "media_type" in value
        out = {
            k: (v if _skip_key(k) or (is_media and k == "data") else redact_json(v, redact))
            for k, v in value.items()
        }
        return value if all(out[k] is v for k, v in value.items()) else out
    return value


# ── what the plugin calls ───────────────────────────────────────────────────

def redact_text(text: str) -> str:
    """Remove credentials from ``text`` (`redact_secrets`), keeping a vendor
    prefix as a breadcrumb. Never raises: a text this cannot process falls back
    to the MemHub-key rule alone."""
    try:
        return redact_secrets(text)
    except Exception:  # noqa: BLE001 — never break a send over redaction
        try:
            return _SECRET.sub(PLACEHOLDER, text)
        except Exception:  # noqa: BLE001
            return text


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


# ── identities, for text bound for a MODEL, not for the archive ────────────
#
# The capture path above removes only prefixed credentials, so the archive
# stays trustworthy. The harness-tied memory window is a different object: a
# short slice of a session sent to a classifier, whose tool output is
# untrusted and can carry a teammate's name, e-mail or home directory. Over-
# redacting it costs little; passing an identity on cannot be undone.
#
# Two shapes, coarse on purpose: a home directory (`/Users/<name>`,
# `/home/<name>`, `C:\\Users\\<name>`, `/root`, and this machine's own home
# wherever it lives) becomes `~`, which is how the path
# reads to every teammate anyway, and an e-mail address becomes `<email>`.
_PATH_END = r"(?=$|[/\\\s'\"`:;,)])"
_HOME_DIR = re.compile(r"(?<![\w.~-])(?:(?:/(?:Users|home)/|[A-Za-z]:\\Users\\)"
                       r"[^/\\\s'\"`:;,)]+|/root" + _PATH_END + r")")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _own_home():
    """This machine's home directory as a pattern: a CI agent's or a
    container's home often fits none of the fixed shapes."""
    home = os.path.expanduser("~").rstrip("/\\")
    if len(home) < 2 or home.startswith("~"):
        return None
    return re.compile(r"(?<![\w.~-])" + re.escape(home) + _PATH_END)


def redact_identities(text: str) -> str:
    """Home directories to ``~``, e-mail addresses to ``<email>``. For text
    about to reach a model, never for records being captured."""
    if not text:
        return text
    own = _own_home()
    if own is not None:
        text = own.sub("~", text)
    return _EMAIL.sub("<email>", _HOME_DIR.sub("~", text))


def _capture_text(text: str) -> str:
    try:
        return redact_capture(text)
    except Exception:  # noqa: BLE001 — one string the rules choke on is sent as it is
        return redact_text(text)


def _redact_record(value, key=None, media=False):
    """One transcript record at capture strength: every free-text string leaf
    through `redact_capture`, identifier and enum keys and base64 media left
    alone (the server's `redact_json`), and dict KEYS through `redact_text` —
    they are almost never secrets, but a key is as capturable as a value."""
    if isinstance(value, str):
        return value if _skip_key(key) or (media and key == "data") else _capture_text(value)
    if isinstance(value, list):
        return [_redact_record(v, key, media) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_record(v, key, media) for v in value)
    if isinstance(value, dict):
        is_media = value.get("type") == "base64" and "media_type" in value
        return {(redact_text(k) if isinstance(k, str) else k): _redact_record(v, k, is_media)
                for k, v in value.items()}
    return value


def redact_records(records: list) -> list:
    """Redact a batch of transcript records before upload: credentials,
    high-entropy tokens, standalone hex tokens and basic PII. Never raises.

    On a failure in one record that record falls back to the MemHub-key pass,
    and on any other failure the records are returned untouched: capture
    stopping is a worse outcome than this pass not running, and the server's
    own redaction at ingest is still behind it.
    """
    try:
        out = []
        for r in records:
            try:
                out.append(_redact_record(r))
            except Exception:  # noqa: BLE001
                out.append(redact(r))
        return out
    except Exception:  # noqa: BLE001 — never break capture over redaction
        return records


def contains_secret(value) -> bool:
    """True if a secret survives anywhere in ``value`` — for tests and checks.
    A MemHub key, or any other high-confidence credential (`has_secret`: vendor
    keys, JWTs, PEM private keys) that is not already a redaction breadcrumb."""
    if isinstance(value, str):
        return bool(_SECRET.search(value)) or any(p.search(value) for p in _PREFIXED) \
            or bool(_JWT.search(value)) or bool(_PEM.search(value) and "<redacted>" not in value)
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v) for v in value)
    if isinstance(value, dict):
        return any(contains_secret(k) or contains_secret(v)
                   for k, v in value.items())
    return False
