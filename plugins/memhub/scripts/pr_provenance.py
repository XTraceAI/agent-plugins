#!/usr/bin/env python3
"""Bounded GitHub pull-request reference hints from coding-host output.

User prompts and assistant prose are intentionally outside this module's input
surface. Transcript extraction visits only a result paired to a direct
``gh pr create`` call, and Cursor's shell hook inspects that command's output.
Neither transcript structure nor hook payload is an authentication boundary:
the backend treats these as authenticated-client telemetry, requires the org's
repository catalog and an independently observed PR row, and owns link policy.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

import git_provenance

MAX_URLS = 50
MAX_TEXT_BYTES = 1024 * 1024
MAX_NODES = 10_000
MAX_DEPTH = 64

_PR_URL_RE = re.compile(
    r"https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]*)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=\S*\Z")
_SHELL_PUNCTUATION = ";&|`()\n"


def _bounded_utf8(value: str, limit: int) -> tuple[str, int]:
    """Return at most ``limit`` UTF-8 bytes and the consumed byte count."""
    # Slice before encoding too: one Unicode code point is at most four UTF-8
    # bytes, so even a hostile input cannot make this helper allocate an
    # unbounded second copy merely to enforce the scan budget.
    encoded = value[:limit].encode("utf-8", errors="ignore")[:limit]
    return encoded.decode("utf-8", errors="ignore"), len(encoded)


def urls_from_output_text(value: object) -> list[str]:
    """Canonical PR URLs in one output string, bounded and unique."""
    if not isinstance(value, str) or not value:
        return []
    text, _ = _bounded_utf8(value, MAX_TEXT_BYTES)
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


def is_pr_creation_command(value: object) -> bool:
    """Whether one bounded command directly invokes ``gh pr create``.

    Shell chains, command substitutions, and shell wrappers are rejected. This
    reduces accidental evidence capture; it does not make caller-supplied hook
    or transcript data authentic.
    """
    if not isinstance(value, str) or not value or len(value) > 16384:
        return False
    try:
        lexer = shlex.shlex(
            value, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or any(
            token and all(ch in _SHELL_PUNCTUATION for ch in token)
            for token in tokens):
        return False

    i = 0
    if tokens[i] == "env":
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 1
    while i < len(tokens) and _ASSIGNMENT_RE.fullmatch(tokens[i]):
        i += 1
    if i >= len(tokens) or tokens[i] != "gh":
        return False
    i += 1
    while i < len(tokens) and tokens[i].startswith("-"):
        option = tokens[i]
        i += 1
        if ("=" not in option and i < len(tokens)
                and not tokens[i].startswith("-") and tokens[i] != "pr"):
            i += 1
    return i + 1 < len(tokens) and tokens[i:i + 2] == ["pr", "create"]


def _tool_command(block: dict) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("cmd", "command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _trusted_strings(
    value: object,
    budget: list[int],
    nodes: list[int],
    depth: int = 0,
) -> Iterable[str]:
    """Text leaves under one tool-result content value, within one byte budget."""
    if budget[0] <= 0 or nodes[0] <= 0 or depth >= MAX_DEPTH:
        return
    nodes[0] -= 1
    if isinstance(value, str):
        take, consumed = _bounded_utf8(value, budget[0])
        budget[0] -= consumed
        yield take
    elif isinstance(value, list):
        for item in value:
            yield from _trusted_strings(item, budget, nodes, depth + 1)
            if budget[0] <= 0 or nodes[0] <= 0:
                return
    elif isinstance(value, dict):
        # Canonical tool results use content/text/output leaves. Restricting the
        # walk to their values avoids scanning ids or any future metadata field.
        for key in ("content", "text", "output"):
            if key in value:
                yield from _trusted_strings(
                    value[key], budget, nodes, depth + 1)
                if budget[0] <= 0 or nodes[0] <= 0:
                    return


def urls_from_tool_results(records: Iterable[object]) -> list[str]:
    """Exact PR URLs returned by paired ``gh pr create`` tool calls only."""
    found: list[str] = []
    budget = [MAX_TEXT_BYTES]
    nodes = [MAX_NODES]
    pr_create_calls: set[str] = set()
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
                if (isinstance(call_id, str)
                        and is_pr_creation_command(_tool_command(block))):
                    pr_create_calls.add(call_id)
                continue
            if (block.get("type") != "tool_result"
                    or block.get("tool_use_id") not in pr_create_calls):
                continue
            for text in _trusted_strings(
                    block.get("content"), budget, nodes):
                for url in urls_from_output_text(text):
                    if url not in found:
                        found.append(url)
                        if len(found) >= MAX_URLS:
                            return found
            if budget[0] <= 0 or nodes[0] <= 0:
                return found
    return found


def merge_urls(*groups: Iterable[object]) -> list[str]:
    """Canonical, order-preserving union of already extracted URL groups."""
    merged: list[str] = []
    for group in groups:
        for value in group:
            parsed = urls_from_output_text(value) if isinstance(value, str) else []
            # Persisted state and server acknowledgements must already be the
            # minimal canonical form. A suffix from a corrupt/older state is
            # rejected fail-closed rather than silently broadened into evidence.
            if len(parsed) != 1 or parsed[0] != value.lower().rstrip("/"):
                continue
            if parsed[0] not in merged:
                merged.append(parsed[0])
                if len(merged) >= MAX_URLS:
                    return merged
    return merged


def import_provenance(
    urls: Iterable[object], git: object = None,
) -> dict | None:
    """Bounded ``import_conversation.provenance`` arguments, or ``None``."""
    canonical = merge_urls(urls)
    if not canonical:
        return None
    payload: dict = {"github_pr_urls": canonical}
    if isinstance(git, dict):
        repository_url = git_provenance.normalize_repository_url(
            git.get("repository_url"))
        if repository_url:
            payload["git"] = {"repository_url": repository_url}
    return payload


def accepted_urls(ack: object) -> list[str]:
    """The exact URL subset a server explicitly accepted in an import ack."""
    if not isinstance(ack, dict):
        return []
    received = ack.get("provenance_received")
    values = received.get("github_pr_urls") if isinstance(received, dict) else None
    return merge_urls(values if isinstance(values, list) else [])
