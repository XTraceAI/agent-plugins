#!/usr/bin/env python3
"""Fetch an encrypted MemHub artifact and decrypt its text locally.

The artifact body remains ciphertext throughout the MCP round-trip.  Only this
process receives the passphrase (from the environment or a private local
``.env``); it is consumed by ``xtrace-ai-sdk`` locally and is never included in
tool arguments.

Usage:
    uv run --with 'mcp<2' --with xtrace-ai-sdk==0.1.1 \
      python load_encrypted_artifact.py --artifact-id <id> \
        --agent-brain-id <brain-id>

Use ``--output`` to write the plaintext to a file instead of stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402


def unwrap(result) -> dict:
    """Pull a dictionary payload from an MCP ``CallToolResult``."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"_raw": text}
            except json.JSONDecodeError:
                return {"_raw": text}
    return {"_raw": str(result)}


def _get_artifact_tool(tools_result):
    """Return the server's ``get_artifact`` declaration."""
    for tool in getattr(tools_result, "tools", []) or []:
        if getattr(tool, "name", None) == "get_artifact":
            return tool
    raise RuntimeError("the connected MemHub server does not expose get_artifact")


def _artifact_id_argument(tool) -> str:
    """Resolve the artifact-id field from the live MCP schema.

    Using the server declaration avoids baking a guessed argument spelling into
    the encrypted client while MemHub's tool schemas continue to evolve.
    """
    schema = getattr(tool, "inputSchema", None) or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for candidate in ("artifact_id", "artifactId", "id"):
        if candidate in properties:
            return candidate
    raise RuntimeError(
        "get_artifact has no recognized artifact id argument in its input schema"
    )


def _brain_id_argument(tool) -> str | None:
    """Resolve an optional agent-brain field from the live MCP schema."""
    schema = getattr(tool, "inputSchema", None) or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for candidate in ("agent_brain_id", "agentBrainId", "brain_id", "brainId"):
        if candidate in properties:
            return candidate
    return None


def _artifact_arguments(
    tool,
    artifact_id: str,
    agent_brain_id: str | None,
) -> dict[str, str]:
    """Build a schema-compatible request, including scoped brain context."""
    schema = getattr(tool, "inputSchema", None) or {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    id_argument = _artifact_id_argument(tool)
    brain_argument = _brain_id_argument(tool)

    if agent_brain_id and not brain_argument:
        raise RuntimeError(
            "get_artifact does not expose a recognized agent brain argument"
        )
    if brain_argument in required and not agent_brain_id:
        raise RuntimeError("get_artifact requires brain context; pass --agent-brain-id")

    arguments = {id_argument: artifact_id}
    if agent_brain_id and brain_argument:
        arguments[brain_argument] = agent_brain_id
    return arguments


def _artifact_content(payload: dict) -> str:
    """Find the stored artifact body in common FastMCP response wrappers."""
    queue: list[object] = [payload]
    seen: set[int] = set()
    while queue:
        value = queue.pop(0)
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str):
                return content
            for key in ("result", "artifact", "item", "data"):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    queue.append(nested)
        elif isinstance(value, list):
            queue.extend(v for v in value if isinstance(v, (dict, list)))
    raise RuntimeError("get_artifact response did not contain a text content field")


def _write_private_text(path: Path, plaintext: str) -> None:
    """Write decrypted text without a group/world-readable creation window."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1  # ownership transferred to the file object
            output.write(plaintext)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


async def _load(
    artifact_id: str,
    agent_brain_id: str | None,
    url_override: str | None,
) -> tuple[object, dict]:
    url, headers, auth = resolve_url_and_auth(url_override)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool = _get_artifact_tool(await session.list_tools())
            result = await session.call_tool(
                "get_artifact",
                arguments=_artifact_arguments(tool, artifact_id, agent_brain_id),
            )
            return result, unwrap(result)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch a MemHub artifact and decrypt its SDK-encrypted text locally."
    )
    ap.add_argument("--artifact-id", required=True)
    ap.add_argument(
        "--agent-brain-id",
        default=None,
        help="brain containing the artifact (required for brain-scoped artifacts)",
    )
    ap.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="private dotenv file for encryption (default: "
        "~/.config/memhub-plugin/.env; direct environment variable wins)",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write plaintext to this path instead of stdout",
    )
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    # Lazy import gives a concise dependency/configuration error instead of an
    # import traceback when the script is launched without the documented SDK.
    from _memhub_crypto import (  # noqa: E402
        EncryptedTextError,
        EncryptionConfigurationError,
        XTraceTextCipher,
    )

    try:
        cipher = XTraceTextCipher.from_env(env_file=args.env_file)
        result, payload = await _load(
            args.artifact_id,
            args.agent_brain_id,
            args.url,
        )
        if getattr(result, "isError", False):
            detail = payload.get("_raw") or payload.get("error") or json.dumps(payload)
            print(f"ERROR: get_artifact failed: {detail}", file=sys.stderr)
            return 1
        plaintext = cipher.decrypt(_artifact_content(payload))
    except (EncryptedTextError, EncryptionConfigurationError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.output is not None:
        try:
            _write_private_text(args.output, plaintext)
        except OSError as exc:
            print(
                f"ERROR: cannot write decrypted output {args.output}: {exc}",
                file=sys.stderr,
            )
            return 2
        print(f"decrypted {len(plaintext):,} chars -> {args.output}")
    else:
        sys.stdout.write(plaintext)
        if plaintext and not plaintext.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
