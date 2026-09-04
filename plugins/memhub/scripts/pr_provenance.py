#!/usr/bin/env python3
"""Bounded exact PR URLs from direct ``gh pr create`` tool results.

This is high-confidence, partial-recall telemetry. It deliberately ignores
prompts, assistant prose, branch names, cwd, filesystem paths, and Git remotes.
The backend scopes writes to the authenticated session and counts distinct
canonical URLs; these caller-observed URLs are not authorization or authorship
proof.
"""
from __future__ import annotations

import json
import re
import shlex
from collections import deque
from collections.abc import Iterable

MAX_URLS = 50
MAX_RESULTS = 50
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
_DIRECT_COMMAND_TOOLS = {"bash", "exec", "exec_command", "shell"}
_CODEX_EXEC_PREFIX_RE = re.compile(
    r"\A(?:[ \t]*// @exec:[^\r\n]*\r?\n)?[ \t]*"
    r"const[ \t]+(?P<variable>[A-Za-z_$][A-Za-z0-9_$]*)[ \t]*="
    r"[ \t]*await[ \t]+tools\.exec_command\([ \t]*"
    r"(?P<object>\{)[ \t\r\n]*cmd[ \t]*:[ \t]*"
)
_CODEX_RESULT_HEADER_RE = re.compile(
    r"\AScript completed\r?\nWall time [^\r\n]+\r?\nOutput:\r?\n?\Z"
)


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


def _has_command_substitution(value: str) -> bool:
    """Reject executable substitutions while allowing quoted punctuation."""
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if quote is None and char == "'":
            quote = "'"
            index += 1
            continue
        if char == "`" or (char == "$" and value[index:index + 2] == "$("):
            return True
        index += 1
    return False


def is_pr_creation_command(value: object) -> bool:
    """Return whether a bounded command directly invokes ``gh pr create``."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_COMMAND_CHARS
        or _has_command_substitution(value)
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


def _matching_object_end(source: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                return None
    return None


def _codex_exec_command(block: dict, tool_input: dict) -> str | None:
    """Extract ``cmd`` from Codex Desktop's generated exec envelope only."""
    if block.get("name") != "exec":
        return None
    source = tool_input.get("input")
    if (
        not isinstance(source, str)
        or len(source) > MAX_COMMAND_CHARS
        or source.count("tools.") != 1
    ):
        return None
    match = _CODEX_EXEC_PREFIX_RE.match(source)
    if match is None or source[match.end():match.end() + 1] != '"':
        return None
    try:
        command, command_end = json.JSONDecoder().raw_decode(source, match.end())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(command, str):
        return None
    object_end = _matching_object_end(source, match.start("object"))
    if object_end is None or object_end < command_end:
        return None
    variable = re.escape(match.group("variable"))
    suffix = source[object_end + 1:]
    if re.fullmatch(
        rf"[ \t\r\n]*\)[ \t]*;[ \t\r\n]*"
        rf"text[ \t]*\([ \t]*{variable}\.output[ \t]*\)[ \t]*;[ \t\r\n]*",
        suffix,
    ) is None:
        return None
    return command


def _tool_command(block: dict) -> str | None:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    name = block.get("name")
    if not isinstance(name, str) or name.casefold() not in _DIRECT_COMMAND_TOOLS:
        return None
    for key in ("cmd", "command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return _codex_exec_command(block, tool_input)


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


def _exact_url_text(value: object) -> list[str]:
    """Return one URL only when the complete trimmed value is that URL."""
    if not isinstance(value, str):
        return []
    candidate = value.strip()
    urls = urls_from_output_text(candidate)
    if (
        len(urls) == 1
        and candidate.casefold().rstrip("/") == urls[0]
    ):
        return urls
    return []


def _exact_result_urls(value: object) -> list[str]:
    """Accept one exact result URL, including Codex's encoded output blocks."""
    root = value
    if (
        isinstance(value, str)
        and len(value) <= MAX_RESULT_TEXT_BYTES
        and value.lstrip().startswith(("[", "{"))
    ):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            pass
        else:
            root = parsed
    texts = [text.strip() for text in _result_strings(
        root,
        [MAX_RESULT_TEXT_BYTES],
        [MAX_RESULT_NODES],
    ) if text.strip()]
    if texts and _CODEX_RESULT_HEADER_RE.fullmatch(texts[0] + "\n"):
        texts.pop(0)
    if len(texts) != 1:
        return []
    return _exact_url_text(texts[0])


def _execution_status(payload: dict) -> str:
    """Return ``success``, ``failure``, or ``unknown`` from host fields."""
    failure = payload.get("is_error") is True or payload.get("success") is False
    success = payload.get("is_error") is False or payload.get("success") is True
    for key in ("exit_code", "exitCode"):
        code = payload.get(key)
        if type(code) is int:
            if code == 0:
                success = True
            else:
                failure = True
    if failure:
        return "failure"
    return "success" if success else "unknown"


def scan_tool_results(records: Iterable[object]) -> tuple[list[str], int]:
    """Return ``(urls, missing_url_results)`` for direct PR-create results."""
    calls: set[str] = set()
    call_order: deque[str] = deque()
    matched_results: deque[dict] = deque(maxlen=MAX_RESULTS)

    def discard_call(call_id: str) -> None:
        calls.discard(call_id)
        try:
            call_order.remove(call_id)
        except ValueError:
            pass

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
                if isinstance(call_id, str):
                    # The newest call owns a reused id. Pending calls are
                    # bounded too; if a malformed transcript batches more
                    # than MAX_RESULTS before any result, retain the newest.
                    discard_call(call_id)
                    if is_pr_creation_command(_tool_command(block)):
                        if len(call_order) >= MAX_RESULTS:
                            calls.discard(call_order.popleft())
                        call_order.append(call_id)
                        calls.add(call_id)
                continue
            if (
                block.get("type") != "tool_result"
                or block.get("tool_use_id") not in calls
            ):
                continue
            discard_call(block.get("tool_use_id"))
            matched_results.append(block)

    found: list[str] = []
    missing = 0
    for block in matched_results:
        budget = [MAX_RESULT_TEXT_BYTES]
        nodes = [MAX_RESULT_NODES]
        result_urls: list[str] = []
        for text in _result_strings(block.get("content"), budget, nodes):
            for url in urls_from_output_text(text):
                if url not in result_urls:
                    result_urls.append(url)
        status = _execution_status(block)
        if status == "unknown":
            result_urls = _exact_result_urls(block.get("content"))
        if status == "failure" or len(result_urls) != 1:
            missing += 1
        else:
            for url in result_urls:
                if url not in found:
                    found.append(url)
                    if len(found) >= MAX_URLS:
                        return found, missing
    return found, missing


def urls_from_tool_results(records: Iterable[object]) -> list[str]:
    """Return URLs from results paired to direct PR-creation calls only."""
    return scan_tool_results(records)[0]


def scan_shell_event(event: str, payload: object) -> tuple[list[str], int]:
    """Return URLs and a missing count from Cursor's post-shell hook."""
    if event != "afterShellExecution" or not isinstance(payload, dict):
        return [], 0
    if not is_pr_creation_command(payload.get("command")):
        return [], 0
    status = _execution_status(payload)
    urls = urls_from_output_text(payload.get("output"))
    if status == "unknown":
        urls = _exact_url_text(payload.get("output"))
    if status == "failure" or len(urls) != 1:
        return [], 1
    return urls, 0


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


def has_provenance_ack(ack: object) -> bool:
    """Whether this backend understands PR provenance acknowledgement.

    Absence is the rolling-deploy compatibility case: transcript persistence
    was confirmed through ``ack_through``, but an older backend cannot say
    whether it stored optional PR telemetry. A present but malformed/empty
    field is different: that backend knows the contract and did not confirm
    this URL, so callers should retain it for retry.
    """
    return isinstance(ack, dict) and "provenance_received" in ack


def acknowledge(
    pending: Iterable[object],
    accepted: Iterable[object],
    ack: object,
) -> tuple[list[str], list[str]]:
    # Keep the newest confirmed evidence when the bounded cache is full. This
    # matters for Cursor's hook-only URLs: retaining only the oldest entries
    # would leave URL 51 pending forever after the backend had acknowledged it.
    accepted_now = merge_urls(accepted_urls(ack), accepted)
    accepted_set = set(accepted_now)
    pending_now = [url for url in merge_urls(pending) if url not in accepted_set]
    return pending_now, accepted_now


def acknowledge_confirmed_import(
    pending: Iterable[object],
    accepted: Iterable[object],
    ack: object,
) -> tuple[list[str], list[str]]:
    """Reconcile optional provenance after the transcript was confirmed.

    An older backend has no ``provenance_received`` field. Its confirmed core
    write must advance normally instead of retrying optional metadata until
    capture goes dormant. The URL remains discoverable in later full scans.
    """
    if pending and not has_provenance_ack(ack):
        return [], merge_urls(accepted)
    return acknowledge(pending, accepted, ack)
