#!/usr/bin/env python3
"""DEPRECATED shim — the transform moved to
``plugins/memhub/scripts/readers/codex.py`` with the multi-host reader
refactor. This re-export keeps existing imports and docs working for one
release; new code should import ``readers.codex``."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "plugins" / "memhub" / "scripts"))
from readers.codex import *  # noqa: F401,F403,E402
from readers.codex import (  # noqa: F401,E402  (explicit, for `from … import x`)
    clean_user_text, load_rollout, rollout_to_claude_records, rollout_uuid,
)
