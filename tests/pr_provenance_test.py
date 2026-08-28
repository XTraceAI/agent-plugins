#!/usr/bin/env python3
"""Trust-boundary tests for exact PR URL provenance."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import pr_provenance as p  # noqa: E402


def test_trusted_text_canonicalizes_and_deduplicates():
    text = (
        "Created https://github.com/XTraceAI/Web/pull/42\n"
        "again https://github.com/xtraceai/web/pull/42/\n"
        "files https://github.com/xtraceai/web/pull/42/files\n"
        "other https://github.com/xtraceai/api/pull/7)."
    )
    assert p.urls_from_trusted_text(text) == [
        "https://github.com/xtraceai/web/pull/42",
        "https://github.com/xtraceai/api/pull/7",
    ]


def test_only_tool_results_are_scanned():
    records = [
        {"message": {"role": "user", "content":
                     "https://github.com/x/user-prompt/pull/1"}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text":
             "https://github.com/x/assistant-prose/pull/2"},
            {"type": "tool_use", "input": {"url":
             "https://github.com/x/tool-input/pull/3"}},
            {"type": "tool_use", "id": "call-1", "name": "exec_command",
             "input": {"cmd": "gh pr create --fill"}},
        ]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": [
                {"type": "text", "text":
                 "https://github.com/X/Trusted/pull/4"},
            ]},
        ]}},
    ]
    assert p.urls_from_tool_results(records) == [
        "https://github.com/x/trusted/pull/4",
    ]


def test_only_direct_pr_create_results_qualify():
    url = "https://github.com/x/r/pull/6"
    records = [
        {"message": {"content": [
            {"type": "tool_use", "id": "cat", "input": {
                "command": "cat README.md"}},
            {"type": "tool_use", "id": "view", "input": {
                "command": "gh pr view 6"}},
            {"type": "tool_use", "id": "create", "input": {
                "command": "bash -lc 'cd repo && gh pr create --fill'"}},
        ]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "cat", "content": url},
            {"type": "tool_result", "tool_use_id": "view", "content": url},
            {"type": "tool_result", "tool_use_id": "create", "content": url},
        ]}},
    ]
    assert p.urls_from_tool_results(records) == [url]


def test_lookalikes_and_non_strings_are_rejected():
    text = " ".join((
        "http://github.com/x/r/pull/1",
        "https://github.com.evil.example/x/r/pull/2",
        "https://github.com/x/r/issues/3",
        "https://github.com/x/r/pull/4abc",
        "https://github.com/x/r/pull/5/files",
    ))
    assert p.urls_from_trusted_text(text) == []
    assert p.urls_from_trusted_text(None) == []


def test_scan_budget_is_utf8_bytes_not_characters():
    url = "https://github.com/x/r/pull/8"
    # This remains below one million Python characters but exceeds one million
    # UTF-8 bytes before the URL. The URL must therefore be outside the cap.
    text = ("\u00e9" * (p.MAX_TEXT_BYTES // 2 + 1)) + url
    assert len(text) < p.MAX_TEXT_BYTES
    assert p.urls_from_trusted_text(text) == []
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create", "content": text},
    ]}}]
    assert p.urls_from_tool_results(records) == []


def test_import_payload_and_ack_are_explicit():
    url = "https://github.com/x/r/pull/9"
    assert p.import_provenance(
        [url], {"repository_url": "git@github.com:x/r.git"},
    ) == {
        "github_pr_urls": [url],
        "git": {"repository_url": "git@github.com:x/r.git"},
    }
    assert p.accepted_urls({
        "provenance_received": {"github_pr_urls": [url]},
    }) == [url]
    assert p.accepted_urls({"conversation_id": "c"}) == []


def test_merge_state_rejects_noncanonical_suffixes_fail_closed():
    canonical = "https://github.com/x/r/pull/9"
    assert p.merge_urls([canonical.upper()]) == [canonical]
    assert p.merge_urls([canonical + "/files", canonical + ")"]) == []


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
