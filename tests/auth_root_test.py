#!/usr/bin/env python3
"""Plugin-root selection must reject Cursor's unrelated global root."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "memhub"
STAGING = ROOT / "plugins" / "memhub-staging"
sys.path.insert(0, str(PLUGIN / "scripts"))

import _memhub_auth as auth  # noqa: E402

PROD_URL = "https://api.memhub.xtrace.ai/mcp-server/mcp"
STAGING_URL = "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"


@contextmanager
def roots(claude: str | None, cursor: str | None = None):
    old = {name: os.environ.get(name) for name in
           ("CLAUDE_PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT")}
    try:
        for name, value in (("CLAUDE_PLUGIN_ROOT", claude),
                            ("CURSOR_PLUGIN_ROOT", cursor)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_matching_claude_root_is_preserved():
    with roots(str(PLUGIN)):
        assert auth._plugin_root() == PLUGIN
        assert auth.default_url() == PROD_URL
    print("PASS test_matching_claude_root_is_preserved")


def test_staging_symlink_keeps_staging_identity():
    with roots(str(STAGING)):
        assert auth._plugin_root() == STAGING
        assert auth.default_url() == STAGING_URL
    print("PASS test_staging_symlink_keeps_staging_identity")


def test_cursor_unrelated_root_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        unrelated = Path(td) / "vercel"
        unrelated.mkdir()
        (unrelated / "scripts").mkdir()
        (unrelated / "scripts" / "_memhub_auth.py").write_text(
            "# unrelated plugin module\n", encoding="utf-8")
        (unrelated / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"memhub": {"url": STAGING_URL}}
        }), encoding="utf-8")
        with roots(str(unrelated), str(unrelated)):
            assert auth._plugin_root() == PLUGIN
            assert auth.default_url() == PROD_URL
    print("PASS test_cursor_unrelated_root_is_rejected")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
