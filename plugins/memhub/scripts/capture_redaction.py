"""Bounded redaction reuse within one multi-destination hook invocation."""
from __future__ import annotations

import copy
from contextvars import ContextVar
import json
from redact import redact_records

# Bounded cache shared across destinations during this one invocation. Each
# projection gets its own copy, so endpoint-specific arguments cannot mutate it.
CACHE: ContextVar[dict | None] = ContextVar("turn_redaction_cache", default=None)


def redact_once(records):
    cache = CACHE.get()
    if cache is None:
        return redact_records(records)
    result = []
    items = cache["items"]
    for record in records:
        key = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if key not in items:
            redacted = redact_records([record])[0]
            # Keep memory bounded for long catch-up sessions. Eviction only
            # repeats redaction; it can never skip or change a record.
            if len(key) > 8 * 1024 * 1024:
                result.append(redacted)
                continue
            if cache["bytes"] + len(key) > 8 * 1024 * 1024:
                items.clear()
                cache["bytes"] = 0
            items[key] = redacted
            cache["bytes"] += len(key)
        result.append(copy.deepcopy(items[key]))
    return result

