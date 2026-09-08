"""One context budget for everything MemHub injects at session start.

The brief (map, apply, recall) and the rulebook's session posture both land
in the agent's context before the first prompt. They used to size themselves
independently, so neither could promise what the pair costs. Now there is one
number — ``MEMHUB_BRIEF_TOKEN_BUDGET``, default 2,500 tokens — split 2:1
brief:rulebook (navigation spec §4, "Session start").

Tokens are approximated as chars/4, the same rule the rulebook already used
for its own cap. Stdlib only: read on the synchronous SessionStart path.
"""
from __future__ import annotations

import os

DEFAULT_TOKENS = 2500
CHARS_PER_TOKEN = 4
_MIN_TOKENS = 200   # below this the brief cannot even name the brain


def total_tokens() -> int:
    raw = (os.environ.get("MEMHUB_BRIEF_TOKEN_BUDGET") or "").strip()
    try:
        n = int(raw) if raw else DEFAULT_TOKENS
    except ValueError:
        n = DEFAULT_TOKENS
    return max(_MIN_TOKENS, n)


def total_chars() -> int:
    return total_tokens() * CHARS_PER_TOKEN


def brief_chars() -> int:
    """Two thirds: the brief carries the map, which is never trimmed."""
    return total_chars() * 2 // 3


def rulebook_chars() -> int:
    """One third: session-posture rules, text + why."""
    return total_chars() - brief_chars()
