#!/usr/bin/env python3
"""Trust-boundary tests for exact pull-request URL evidence."""
from __future__ import annotations

import json
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
            {"type": "tool_use", "id": "view", "name": "Bash", "input": {
                "cmd": "gh pr view 2"}},
            {"type": "tool_use", "id": "create", "name": "Bash", "input": {
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
        {"type": "tool_use", "id": "create", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create", "content": url,
         "is_error": True},
    ]}}]
    assert p.urls_from_tool_results(records) == []
    assert p.scan_tool_results(records) == ([], 1)

    statusless_failure = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create",
         "content": f"a pull request already exists:\n{url}"},
    ]}}]
    assert p.scan_tool_results(statusless_failure) == ([], 1)

    explicit_success = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create",
         "content": f"Created {url}", "is_error": False},
    ]}}]
    assert p.urls_from_tool_results(explicit_success) == [url]


def test_missing_output_is_diagnostic_and_redirection_is_rejected():
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "web", "name": "Bash", "input": {
            "command": "gh pr create --web"}},
        {"type": "tool_result", "tool_use_id": "web",
         "content": "Opening browser"},
        {"type": "tool_use", "id": "redirected", "name": "Bash", "input": {
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
        r'"C:\bin\gh.exe" pr create --fill',
        "env -u GH_TOKEN gh pr create --fill",
        "gh pr create --title 'fix; still one command'",
    )
    rejected = (
        "echo x; gh pr create --fill",
        "bash -lc 'gh pr create --fill'",
        "printf 'gh pr create'",
        "gh pr view 6",
        r"C:\bin\gh.exe pr create --fill",
        'gh pr create --body "$(cat body.md)"',
        'gh pr create --body "`cat body.md`"',
    )
    assert all(p.is_pr_creation_command(value) for value in accepted)
    assert not any(p.is_pr_creation_command(value) for value in rejected)


def test_current_codex_exec_envelope_is_narrowly_supported():
    url = "https://github.com/x/r/pull/19"

    def records(source: str, name: str = "exec"):
        return [{"message": {"content": [
            {"type": "tool_use", "id": "create", "name": name,
             "input": {"input": source}},
            {"type": "tool_result", "tool_use_id": "create", "content": url},
        ]}}]

    accepted = (
        'const r = await tools.exec_command({cmd:"gh pr create --fill",'
        'workdir:"/tmp"}); text(r.output);',
        '// @exec: {"yield_time_ms": 30000}\n'
        'const result = await tools.exec_command({\n'
        '  cmd: "gh --repo x/r pr create --fill",\n'
        '  yield_time_ms: 30000\n'
        '});\ntext(result.output);',
    )
    assert all(p.urls_from_tool_results(records(source)) == [url]
               for source in accepted)

    codex_success = [
        {"type": "input_text",
         "text": "Script completed\nWall time 0.9 seconds\nOutput:\n"},
        {"type": "input_text", "text": url + "\n"},
    ]
    encoded = records(accepted[0])
    encoded[0]["message"]["content"][1]["content"] = json.dumps(codex_success)
    assert p.urls_from_tool_results(encoded) == [url]

    codex_failure = records(accepted[0])
    codex_failure[0]["message"]["content"][1]["content"] = json.dumps([
        {"type": "input_text",
         "text": "Script completed\nWall time 0.9 seconds\nOutput:\n"},
        {"type": "input_text", "text":
         f"a pull request already exists:\n{url}\n"},
    ])
    assert p.scan_tool_results(codex_failure) == ([], 1)

    rejected = (
        "const r = await tools.exec_command({cmd:'gh pr create --fill'}); "
        "text(r.output);",
        'const r = await tools.exec_command({cmd:"gh pr create --fill"}); '
        'text(other.output);',
        'const r = await tools.exec_command({cmd:"gh pr create --fill"}); '
        'notify("extra"); text(r.output);',
        'const r = await tools.exec_command({cmd:"gh pr create --fill"}); '
        'const x = tools.read(); text(r.output);',
        'const r = await tools.exec_command({cmd:"git status; gh pr create"}); '
        'text(r.output);',
        'Here is an example: const r = await tools.exec_command('
        '{cmd:"gh pr create --fill"}); text(r.output);',
    )
    assert all(p.urls_from_tool_results(records(source)) == []
               for source in rejected)
    assert p.urls_from_tool_results(records(accepted[0], name="other")) == []


def test_only_known_shell_tools_and_the_newest_reused_id_can_match():
    url = "https://github.com/x/r/pull/21"
    unknown = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "name": "CustomAction",
         "input": {"command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create", "content": url},
    ]}}]
    assert p.urls_from_tool_results(unknown) == []

    reused = [{"message": {"content": [
        {"type": "tool_use", "id": "same", "name": "Bash",
         "input": {"command": "gh pr create --fill"}},
        {"type": "tool_use", "id": "same", "name": "Bash",
         "input": {"command": "gh pr view 21"}},
        {"type": "tool_result", "tool_use_id": "same", "content": url},
    ]}}]
    assert p.urls_from_tool_results(reused) == []


def test_cursor_after_shell_event_requires_direct_command_and_successful_url():
    url = "https://github.com/x/r/pull/22"
    payload = {"command": "gh pr create --fill", "output": url + "\n"}
    assert p.scan_shell_event("afterShellExecution", payload) == ([url], 0)
    assert p.scan_shell_event("beforeShellExecution", payload) == ([], 0)
    assert p.scan_shell_event("afterShellExecution", {
        **payload, "command": "sh -c 'gh pr create --fill'",
    }) == ([], 0)
    assert p.scan_shell_event("afterShellExecution", {
        **payload, "exit_code": 1,
    }) == ([], 1)
    assert p.scan_shell_event("afterShellExecution", {
        "command": "gh pr create --fill", "output": f"Created {url}",
        "success": True,
    }) == ([url], 0)
    assert p.scan_shell_event("afterShellExecution", {
        "command": "gh pr create --fill",
        "output": f"a pull request already exists:\n{url}",
    }) == ([], 1)
    assert p.scan_shell_event("afterShellExecution", {
        "command": "gh pr create --fill", "output": "failed",
    }) == ([], 1)


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
        {"type": "tool_use", "id": "create", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "create",
         "content": ("x" * p.MAX_RESULT_TEXT_BYTES) + url},
    ]}}]
    assert p.urls_from_tool_results(records) == []


def test_large_earlier_result_cannot_starve_a_later_result():
    url = "https://github.com/x/r/pull/18"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "first", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "first",
         "content": "x" * (p.MAX_RESULT_TEXT_BYTES + 1)},
        {"type": "tool_use", "id": "second", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "second", "content": url},
    ]}}]
    assert p.urls_from_tool_results(records) == [url]


def test_only_newest_fifty_qualifying_results_are_examined():
    url = "https://github.com/x/r/pull/20"
    content = []
    for index in range(p.MAX_RESULTS):
        content.extend((
            {"type": "tool_use", "id": f"missing-{index}", "name": "Bash",
             "input": {
                "command": "gh pr create --fill"}},
            {"type": "tool_result", "tool_use_id": f"missing-{index}",
             "content": "no URL"},
        ))
    content.extend((
        {"type": "tool_use", "id": "too-late", "name": "Bash", "input": {
            "command": "gh pr create --fill"}},
        {"type": "tool_result", "tool_use_id": "too-late", "content": url},
    ))
    assert p.scan_tool_results([{"message": {"content": content}}]) == (
        [url], p.MAX_RESULTS - 1
    )


def test_only_newest_fifty_pending_calls_are_retained():
    url = "https://github.com/x/r/pull/23"
    content = [
        {"type": "tool_use", "id": f"create-{index}", "name": "Bash",
         "input": {"command": "gh pr create --fill"}}
        for index in range(p.MAX_RESULTS + 1)
    ]
    content.extend({
        "type": "tool_result", "tool_use_id": f"create-{index}",
        "content": url if index == p.MAX_RESULTS else "no URL",
    } for index in range(p.MAX_RESULTS + 1))
    assert p.scan_tool_results([{"message": {"content": content}}]) == (
        [url], p.MAX_RESULTS - 1
    )


def test_queue_survives_until_an_explicit_ack():
    url = "https://github.com/x/r/pull/9"
    records = [{"message": {"content": [
        {"type": "tool_use", "id": "create", "name": "Bash", "input": {
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


def test_new_acknowledgement_replaces_oldest_bounded_cache_entry():
    accepted = [f"https://github.com/x/r/pull/{number}"
                for number in range(1, p.MAX_URLS + 1)]
    newest = f"https://github.com/x/r/pull/{p.MAX_URLS + 1}"
    pending, accepted_now = p.acknowledge(
        [newest], accepted,
        {"provenance_received": {"github_pr_urls": [newest]}},
    )
    assert pending == []
    assert accepted_now == [newest, *accepted[:-1]]


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
