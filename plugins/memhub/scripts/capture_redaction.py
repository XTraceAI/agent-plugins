"""Bounded redaction reuse within one multi-destination hook invocation."""
from __future__ import annotations

import copy
from contextvars import ContextVar
import json
import threading
from redact import redact_records

# Bounded cache shared across destinations during this one invocation. Each
# projection gets its own copy, so endpoint-specific arguments cannot mutate it.
CACHE: ContextVar[dict | None] = ContextVar("turn_redaction_cache", default=None)
_CACHE_LOCK = threading.Lock()
_MISSING = object()


def redact_once(records):
    cache = CACHE.get()
    if cache is None:
        return redact_records(records)
    result = []
    items = cache["items"]
    for record in records:
        key = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with _CACHE_LOCK:
            redacted = items.get(key, _MISSING)
        if redacted is _MISSING:
            redacted = redact_records([record])[0]
            # Keep memory bounded for long catch-up sessions. Eviction only
            # repeats redaction; it can never skip or change a record.
            if len(key) > 8 * 1024 * 1024:
                result.append(redacted)
                continue
            with _CACHE_LOCK:
                existing = items.get(key, _MISSING)
                if existing is not _MISSING:
                    redacted = existing
                else:
                    if cache["bytes"] + len(key) > 8 * 1024 * 1024:
                        items.clear()
                        cache["bytes"] = 0
                    items[key] = redacted
                    cache["bytes"] += len(key)
        # A cancelled destination's worker may still evict the shared cache.
        # Retain this value locally and never hold the lock during redaction.
        result.append(copy.deepcopy(redacted))
    return result
