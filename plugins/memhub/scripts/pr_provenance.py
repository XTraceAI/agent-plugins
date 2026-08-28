#!/usr/bin/env python3
"""Exact GitHub pull-request provenance from trusted host output.

User prompts and assistant prose are intentionally outside this module's input
surface. Transcript extraction visits only canonical ``tool_result`` blocks;
Cursor's shell hook uses :func:`urls_from_trusted_text` explicitly on the
host-owned command output. The backend still authorizes every URL against the
session repository and the org's GitHub catalog before confirming a link.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

MAX_URLS = 50
MAX_TEXT_BYTES = 1024 * 1024

_PR_URL_RE = re.compile(
    r"https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]*)"
    r"(?![A-Za-z0-9/])",
    re.IGNORECASE,
)


def _bounded_utf8(value: str, limit: int) -> tuple[str, int]:
    """Return at most ``limit`` UTF-8 bytes and the consumed byte count."""
    # Slice before encoding too: one Unicode code point is at most four UTF-8
    # bytes, so even a hostile input cannot make this helper allocate an
    # unbounded second copy merely to enforce the scan budget.
    encoded = value[:limit].encode("utf-8", errors="ignore")[:limit]
    return encoded.decode("utf-8", errors="ignore"), len(encoded)


def urls_from_trusted_text(value: object) -> list[str]:
    """Canonical PR URLs in one host-owned output string, bounded and unique."""
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


def _trusted_strings(value: object, budget: list[int]) -> Iterable[str]:
    """Text leaves under one tool-result content value, within one byte budget."""
    if budget[0] <= 0:
        return
    if isinstance(value, str):
        take, consumed = _bounded_utf8(value, budget[0])
        budget[0] -= consumed
        yield take
    elif isinstance(value, list):
        for item in value:
            yield from _trusted_strings(item, budget)
            if budget[0] <= 0:
                return
    elif isinstance(value, dict):
        # Canonical tool results use content/text/output leaves. Restricting the
        # walk to their values avoids scanning ids or any future metadata field.
        for key in ("content", "text", "output"):
            if key in value:
                yield from _trusted_strings(value[key], budget)
                if budget[0] <= 0:
                    return


def urls_from_tool_results(records: Iterable[object]) -> list[str]:
    """Exact PR URLs observed in canonical tool-result blocks only."""
    found: list[str] = []
    budget = [MAX_TEXT_BYTES]
    for record in records:
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            for text in _trusted_strings(block.get("content"), budget):
                for url in urls_from_trusted_text(text):
                    if url not in found:
                        found.append(url)
                        if len(found) >= MAX_URLS:
                            return found
            if budget[0] <= 0:
                return found
    return found


def merge_urls(*groups: Iterable[object]) -> list[str]:
    """Canonical, order-preserving union of already extracted URL groups."""
    merged: list[str] = []
    for group in groups:
        for value in group:
            parsed = urls_from_trusted_text(value) if isinstance(value, str) else []
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
        repository_url = git.get("repository_url")
        if isinstance(repository_url, str) and 0 < len(repository_url) <= 4096:
            payload["git"] = {"repository_url": repository_url}
    return payload


def accepted_urls(ack: object) -> list[str]:
    """The exact URL subset a server explicitly accepted in an import ack."""
    if not isinstance(ack, dict):
        return []
    received = ack.get("provenance_received")
    values = received.get("github_pr_urls") if isinstance(received, dict) else None
    return merge_urls(values if isinstance(values, list) else [])
