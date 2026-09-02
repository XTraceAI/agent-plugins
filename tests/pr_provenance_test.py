#!/usr/bin/env python3
"""Trust-boundary tests for exact pull-request URL evidence."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import pr_provenance as p  # noqa: E402


def test_output_text_canonicalizes_and_deduplicates():
    text = (
        "Created https://github.com/XTraceAI/Web/pull/42\n"
        "again https://github.com/xtraceai/web/pull/42/\n"
        "files https://github.com/xtraceai/web/pull/42/files\n"
        "other https://github.com/xtraceai/api/pull/7)."
    )
    assert p.urls_from_output_text(text) == [
        "https://github.com/xtraceai/web/pull/42",
        "https://github.com/xtraceai/api/pull/7",
    ]


def test_only_paired_direct_pr_create_results_are_scanned():
    trusted = "https://github.com/x/trusted/pull/4"
    records = [
        {"message": {"content": [
            {"type": "text", "text": "https://github.com/x/prose/pull/1"},
            {"type": "tool_use", "id": "view", "input": {
                "cmd": "gh pr view 2"}},
            {"type": "tool_use", "id": "create", "input": {
                "cmd": "gh --repo x/trusted pr create --fill"}},
        ]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "view",
             "content": "https://github.com/x/untrusted/pull/2"},
            {"type": "tool_result", "tool_use_id": "create",
             "content": [{"type": "text", "text": trusted}]},
        ]}},
    ]
    assert p.urls_from_tool_results(records) == [trusted]


def test_error_results_are_not_evidence():
    url = "https://github.com/x/r/pull/5"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create", "content": url,
         "is_error": True},
    ]}}]
    assert p.urls_from_tool_results(records) == []
    assert p.scan_tool_results(records) == ([], 1)


def test_missing_output_is_diagnostic_and_redirection_is_rejected():
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "web", "input": {
            "command": "gh pr create --web"}},
        {"type": "tool_result", "tool_use_id": "web",
         "content": "Opening browser"},
        {"type": "tool_use", "id": "redirected", "input": {
            "command": "gh pr create --fill > created.txt"}},
        {"type": "tool_result", "tool_use_id": "redirected", "content": ""},
        {"type": "tool_result", "content": "missing call id"},
        "malformed block",
    ]}}]
    assert p.scan_tool_results(records) == ([], 1)
    assert not p.is_pr_creation_command("gh pr create --fill > created.txt")


def test_direct_command_variants_and_shell_wrappers():
    accepted = (
        "gh pr create --fill",
        "gh --repo x/r pr create --fill",
        "gh -R x/r pr create --fill",
        "FOO=1 /usr/local/bin/gh pr create --fill",
        r"C:\\bin\\gh.exe pr create --fill",
        "env -u GH_TOKEN gh pr create --fill",
    )
    rejected = (
        "echo x; gh pr create --fill",
        "bash -lc 'gh pr create --fill'",
        "printf 'gh pr create'",
        "gh pr view 6",
    )
    assert all(p.is_pr_creation_command(value) for value in accepted)
    assert not any(p.is_pr_creation_command(value) for value in rejected)


def test_lookalikes_and_nested_result_budget_are_rejected():
    text = " ".join((
        "http://github.com/x/r/pull/1",
        "https://github.com.evil.example/x/r/pull/2",
        "https://github.com/x/r/issues/3",
        "https://github.com/x/r/pull/4abc",
        "https://github.com/x/r/pull/5/files",
    ))
    assert p.urls_from_output_text(text) == []
    assert p.urls_from_output_text(None) == []

    url = "https://github.com/x/r/pull/8"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create",
         "content": ("x" * p.MAX_RESULT_TEXT_BYTES) + url},
    ]}}]
    assert p.urls_from_tool_results(records) == []


def test_large_earlier_result_cannot_starve_a_later_result():
    url = "https://github.com/x/r/pull/18"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "first", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "first",
         "content": "x" * (p.MAX_RESULT_TEXT_BYTES + 1)},
        {"type": "tool_use", "id": "second", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "second", "content": url},
    ]}}]
    assert p.urls_from_tool_results(records) == [url]


def test_queue_survives_until_an_explicit_ack():
    url = "https://github.com/x/r/pull/9"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create", "content": url},
    ]}}]
    pending, accepted, missing = p.queued_urls({}, records)
    assert pending == [url]
    assert accepted == []
    assert missing == 0
    assert p.import_provenance(pending) == {"github_pr_urls": [url]}

    assert p.acknowledge(pending, accepted, {"conversation_id": "c"}) == (
        [url], []
    )
    assert p.acknowledge(pending, accepted, {
        "provenance_received": {"github_pr_urls": [url]},
    }) == ([], [url])


def test_persisted_state_rejects_noncanonical_suffixes_fail_closed():
    canonical = "https://github.com/x/r/pull/9"
    assert p.merge_urls([canonical.upper()]) == [canonical]
    assert p.merge_urls([canonical + "/files", canonical + ")"]) == []


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
