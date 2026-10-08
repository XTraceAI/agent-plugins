"""``$MEMHUB_TOKEN`` — the one place the plugin reads a credential from the
user's environment (CI / headless runs; see ``_memhub_auth.explicit_token``).

Its own module so a build can leave it out: the Claude plugin directory export
deletes this file, and ``explicit_token`` then falls back to the userConfig
option, the stored access key and OAuth alone. Every other build — Codex,
Cursor, the XTraceAI/agent-plugins marketplace — ships it unchanged.
"""
from __future__ import annotations

import os


def env_token() -> tuple[str, str] | None:
    """``(token, "$MEMHUB_TOKEN")``, or None when it is unset or blank."""
    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    return (token, "$MEMHUB_TOKEN") if token else None
