"""The last Codex account rate-limit snapshot of a rollout, reduced to an allowlist.

Codex writes a ``rate_limits`` object on its ``token_count`` events: the plan,
how much of the 5-hour and weekly windows is used, when they reset, and the
credit state. MemHub stores the latest one per session so a team can see how
close its engineers run to their plan limits.

Only the keys in ``ALLOWED`` are copied, each with a type and range check. A
key Codex adds later is dropped until it is reviewed here, so nothing the
person did not expect (``individual_limit``, account ids, tokens, auth) can
leave the machine through this path. The backend applies the same allowlist
again on receipt.

This runs beside the capture path, never inside it: ``snapshot_from`` swallows
every error and returns ``None``, so a snapshot Codex reshapes cannot fail a
flush or a checked read, and it is never parsed inside ``codex_usage.apply``
(which raises on anomalies by design).
"""
from __future__ import annotations

import datetime
import math
import re
from typing import Any

SOURCE = "codex"

# Top-level keys copied from a Codex ``rate_limits`` object, plus the two this
# module adds (``source``, ``observed_at``). Anything else is dropped.
ALLOWED = (
    "plan_type",
    "primary",
    "secondary",
    "credits",
    "rate_limit_reached_type",
    "limit_id",
    "limit_name",
    "spend_control_reached",
)
WINDOW_KEYS = ("used_percent", "window_minutes", "resets_at")
CREDIT_KEYS = ("has_credits", "unlimited", "balance")

_MAX_TS_LEN = 40
_MAX_WINDOW_MINUTES = 525_600          # one year
_MAX_RESETS_AT = 4_102_444_800         # 2100-01-01, unix seconds
_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def parse_observed_at(value: Any) -> datetime.datetime | None:
    """An aware UTC datetime from an ISO-8601 string, or None.

    The fraction is padded or cut to microseconds before parsing, so a
    nanosecond Codex stamp parses on every supported Python. A naive stamp is
    read as UTC.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_TS_LEN:
        return None
    match = _ISO_RE.match(value.strip())
    if not match:
        return None
    base, fraction, zone = match.groups()
    micros = (fraction or "").ljust(6, "0")[:6]
    zone = "+00:00" if zone in (None, "Z") else zone
    try:
        parsed = datetime.datetime.fromisoformat(f"{base}.{micros}{zone}")
    except ValueError:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def canonical_observed_at(value: Any) -> str | None:
    """Fixed-width UTC ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` so stamps compare as text too."""
    parsed = parse_observed_at(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if parsed else None


def _int(value: Any, upper: int) -> int | None:
    # type() rather than isinstance: a bool is an int in Python, not a count.
    if type(value) is int and 0 <= value <= upper:
        return value
    return None


def _number(value: Any) -> float | int | None:
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _text(value: Any, limit: int) -> str | None:
    # Non-empty, matching the backend's ``account_limits._str``.
    return value if isinstance(value, str) and value and len(value) <= limit else None


def _window(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    used = _number(raw.get("used_percent"))
    if used is not None and 0 <= used <= 100:
        out["used_percent"] = used
    minutes = _int(raw.get("window_minutes"), _MAX_WINDOW_MINUTES)
    if minutes is not None:
        out["window_minutes"] = minutes
    resets = _int(raw.get("resets_at"), _MAX_RESETS_AT)
    if resets is not None:
        out["resets_at"] = resets
    return out or None


def _credits(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key in ("has_credits", "unlimited"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    if "balance" in raw:
        balance = raw["balance"]
        if balance is None or _number(balance) is not None:
            out["balance"] = balance
        elif isinstance(balance, str) and len(balance) <= 32:
            try:
                if math.isfinite(float(balance)):
                    out["balance"] = balance
            except ValueError:
                pass
    return out or None


def _nullable_text(raw: dict, key: str, limit: int, out: dict) -> None:
    if key not in raw:
        return
    value = raw[key]
    if value is None:
        out[key] = None
    elif _text(value, limit) is not None:
        out[key] = value


def sanitize(raw: Any, observed_at: Any) -> dict | None:
    """The allowlisted, type-checked snapshot, or None when nothing usable remains.

    ``observed_at`` is the ``token_count`` event's own timestamp. Without a
    parseable one the snapshot is dropped: the backend keeps the newest
    snapshot per session, and an undated one cannot be ordered.
    """
    if not isinstance(raw, dict):
        return None
    stamp = canonical_observed_at(observed_at)
    if stamp is None:
        return None
    out: dict = {"source": SOURCE, "observed_at": stamp}
    plan = _text(raw.get("plan_type"), 32)
    if plan is not None:
        out["plan_type"] = plan
    for key in ("primary", "secondary"):
        window = _window(raw.get(key))
        if window is not None:
            out[key] = window
    credits = _credits(raw.get("credits"))
    if credits is not None:
        out["credits"] = credits
    _nullable_text(raw, "rate_limit_reached_type", 64, out)
    limit_id = _text(raw.get("limit_id"), 64)
    if limit_id is not None:
        out["limit_id"] = limit_id
    _nullable_text(raw, "limit_name", 128, out)
    if "spend_control_reached" in raw:
        reached = raw["spend_control_reached"]
        if reached is None or isinstance(reached, bool):
            out["spend_control_reached"] = reached
    # Matches the backend: a snapshot whose surviving keys are all null is none.
    if not any(v is not None for k, v in out.items() if k not in ("source", "observed_at")):
        return None
    return out


def snapshot_from(last_raw: Any, last_ts: Any) -> dict | None:
    """``sanitize`` that can never raise — a bad snapshot is no snapshot."""
    try:
        return sanitize(last_raw, last_ts)
    except Exception:  # noqa: BLE001 — never fail capture over a side observation
        return None
