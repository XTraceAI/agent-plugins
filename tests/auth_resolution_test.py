#!/usr/bin/env python3
"""Stored PAK validation must stay identical across both auth resolvers."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _memhub_auth as auth  # noqa: E402

URL = "https://api.memhub.xtrace.ai/mcp-server/mcp"


def test_stored_pak_secret_validation():
    originals = {
        "stored_pak": auth._stored_pak,
        "refresh": auth._refresh_cached_token_if_stale,
        "cached": auth._cached_access_token,
        "oauth": auth.build_oauth,
    }
    refreshed: list[str] = []
    oauth = object()
    try:
        auth._refresh_cached_token_if_stale = refreshed.append
        auth._cached_access_token = lambda _url: "oauth-token"
        auth.build_oauth = lambda _url, interactive=True: oauth
        os.environ.pop("MEMHUB_TOKEN", None)

        for record in ({}, {"secret": None}, {"secret": ["not", "a", "key"]}):
            auth._stored_pak = lambda _url, value=record: value
            assert auth.resolve_bearer(URL) == (URL, "oauth-token")
            assert auth.resolve_url_and_auth(URL) == (URL, None, oauth)

        auth._stored_pak = lambda _url: {"secret": "mhk_valid"}
        assert auth.resolve_bearer(URL) == (URL, "mhk_valid")
        assert auth.resolve_url_and_auth(URL) == (
            URL, {"Authorization": "Bearer mhk_valid"}, None)
    finally:
        auth._stored_pak = originals["stored_pak"]
        auth._refresh_cached_token_if_stale = originals["refresh"]
        auth._cached_access_token = originals["cached"]
        auth.build_oauth = originals["oauth"]

    assert refreshed == [URL] * 6
    print("PASS test_stored_pak_secret_validation")


if __name__ == "__main__":
    test_stored_pak_secret_validation()
    print("ALL PASS")
