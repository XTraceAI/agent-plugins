#!/usr/bin/env python3
"""Bounded exact PR URLs from direct ``gh pr create`` tool results.

This is high-confidence, partial-recall telemetry. It deliberately ignores
prompts, assistant prose, branch names, cwd, filesystem paths, and Git remotes.
The backend owns repository authorization and all eventual link policy.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

MAX_URLS = 50
MAX_COMMAND_CHARS = 16_384
MAX_RESULT_TEXT_BYTES = 64 * 1024
MAX_RESULT_NODES = 512
MAX_DEPTH = 32

_PR_URL_RE = re.compile(
    r"https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]*)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=\S*\Z")
_SHELL_PUNCTUATION = ";&|`()<>\r\n"
_GH_OPTIONS_WITH_VALUES = {
    "--config",
    "--hostname",
    "--repo",
    "-R",
}


def urls_from_output_text(value: object) -> list[str]:
    """Return bounded, canonical PR URLs from one output string."""
    if not isinstance(value, str) or not value:
        return []
    encoded = value[:MAX_RESULT_TEXT_BYTES].encode(
        "utf-8", errors="ignore"
    )[:MAX_RESULT_TEXT_BYTES]
    text = encoded.decode("utf-8", errors="ignore")
    found: list[str] = []
    for match in _PR_URL_RE.finditer(text):
        owner, repo, number_raw = match.groups()
        number = int(number_raw)
        if number > 2_147_483_647:
            continue
        url = f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{number}"
        if url not in found:
            found.append(url)
            if len(found) >= MAX_URLS:
                break
    return found


def _is_gh_executable(token: str) -> bool:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name in {"gh", "gh.exe"}


def is_pr_creation_command(value: object) -> bool:
    """Return whether a bounded command directly invokes ``gh pr create``."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_COMMAND_CHARS
    ):
        return False
    try:
        lexer = shlex.shlex(
            value,
            posix=True,
            punctuation_chars=_SHELL_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or any(
        token and all(ch in _SHELL_PUNCTUATION for ch in token)
        for token in tokens
    ):
        return False

    index = 0
    if tokens[index] == "env":
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            option = tokens[index]
            index += 1
            if option in {"-u", "--unset"} and index < len(tokens):
                index += 1
    while index < len(tokens) and _ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    if index >= len(tokens) or not _is_gh_executable(tokens[index]):
        return False
    index += 1

    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option in {"--help", "--version", "-h"}:
            return False
        index += 1
        if (
            "=" not in option
            and option in _GH_OPTIONS_WITH_VALUES
            and index < len(tokens)
        ):
            index += 1
    return tokens[index:index + 2] == ["pr", "create"]


def _tool_command(block: dict) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("cmd", "command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _result_strings(
    value: object,
    budget: list[int],
    nodes: list[int],
    depth: int = 0,
) -> Iterable[str]:
    if budget[0] <= 0 or nodes[0] <= 0 or depth >= MAX_DEPTH:
        return
    nodes[0] -= 1
    if isinstance(value, str):
        encoded = value[:budget[0]].encode("utf-8", errors="ignore")[:budget[0]]
        budget[0] -= len(encoded)
        yield encoded.decode("utf-8", errors="ignore")
    elif isinstance(value, list):
        for item in value:
            yield from _result_strings(item, budget, nodes, depth + 1)
            if budget[0] <= 0 or nodes[0] <= 0:
                return
    elif isinstance(value, dict):
        for key in ("content", "text", "output"):
            if key in value:
                yield from _result_strings(value[key], budget, nodes, depth + 1)
                if budget[0] <= 0 or nodes[0] <= 0:
                    return


def scan_tool_results(records: Iterable[object]) -> tuple[list[str], int]:
    """Return ``(urls, missing_url_results)`` for direct PR-create results."""
    found: list[str] = []
    calls: set[str] = set()
    missing = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call_id = block.get("id")
                if (
                    isinstance(call_id, str)
                    and len(calls) < MAX_URLS
                    and is_pr_creation_command(_tool_command(block))
                ):
                    calls.add(call_id)
                continue
            if (
                block.get("type") != "tool_result"
                or block.get("tool_use_id") not in calls
            ):
                continue
            calls.discard(block.get("tool_use_id"))
            budget = [MAX_RESULT_TEXT_BYTES]
            nodes = [MAX_RESULT_NODES]
            result_urls: list[str] = []
            for text in _result_strings(block.get("content"), budget, nodes):
                for url in urls_from_output_text(text):
                    if url not in result_urls:
                        result_urls.append(url)
            if block.get("is_error") is True or not result_urls:
                missing += 1
                continue
            for url in result_urls:
                if url not in found:
                    found.append(url)
                    if len(found) >= MAX_URLS:
                        return found, missing
    return found, missing


def urls_from_tool_results(records: Iterable[object]) -> list[str]:
    """Return URLs from results paired to direct PR-creation calls only."""
    return scan_tool_results(records)[0]


def merge_urls(*groups: Iterable[object]) -> list[str]:
    """Return a bounded, order-preserving union of canonical URL groups."""
    merged: list[str] = []
    for group in groups:
        for value in group:
            parsed = urls_from_output_text(value) if isinstance(value, str) else []
            if len(parsed) != 1 or parsed[0] != value.lower().rstrip("/"):
                continue
            if parsed[0] not in merged:
                merged.append(parsed[0])
                if len(merged) >= MAX_URLS:
                    return merged
    return merged


def queued_urls(
    state: dict,
    records: Iterable[object],
) -> tuple[list[str], list[str], int]:
    """Return pending, accepted, and qualifying results without a URL."""
    accepted = merge_urls(state.get("accepted_pr_urls") or [])
    accepted_set = set(accepted)
    observed, missing = scan_tool_results(records)
    pending = [
        url
        for url in merge_urls(
            state.get("pending_pr_urls") or [],
            observed,
        )
        if url not in accepted_set
    ]
    return pending, accepted, missing


def import_provenance(urls: Iterable[object]) -> dict | None:
    canonical = merge_urls(urls)
    return {"github_pr_urls": canonical} if canonical else None


def accepted_urls(ack: object) -> list[str]:
    if not isinstance(ack, dict):
        return []
    received = ack.get("provenance_received")
    values = received.get("github_pr_urls") if isinstance(received, dict) else None
    return merge_urls(values if isinstance(values, list) else [])


def acknowledge(
    pending: Iterable[object],
    accepted: Iterable[object],
    ack: object,
) -> tuple[list[str], list[str]]:
    accepted_now = merge_urls(accepted, accepted_urls(ack))
    accepted_set = set(accepted_now)
    pending_now = [url for url in merge_urls(pending) if url not in accepted_set]
    return pending_now, accepted_now
