#!/usr/bin/env python3
"""Fetch an encrypted MemHub artifact and decrypt its text locally.

The artifact body remains ciphertext throughout the MCP round-trip.  Only this
process receives ``MEMHUB_ENCRYPTION_PASSPHRASE``; it is consumed by
``xtrace-ai-sdk`` locally and is never included in tool arguments.

Usage:
    # After securely exporting MEMHUB_ENCRYPTION_PASSPHRASE:
    uv run --with mcp --with xtrace-ai-sdk==0.1.1 \
      python load_encrypted_artifact.py --artifact-id <id>

Use ``--output`` to write the plaintext to a file instead of stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
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


async def _load(artifact_id: str, url_override: str | None) -> tuple[object, dict]:
    url, headers, auth = resolve_url_and_auth(url_override)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool = _get_artifact_tool(await session.list_tools())
            id_argument = _artifact_id_argument(tool)
            result = await session.call_tool(
                "get_artifact", arguments={id_argument: artifact_id},
            )
            return result, unwrap(result)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch a MemHub artifact and decrypt its SDK-encrypted text locally."
    )
    ap.add_argument("--artifact-id", required=True)
    ap.add_argument("--output", type=Path, default=None,
                    help="write plaintext to this path instead of stdout")
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
        cipher = XTraceTextCipher.from_env()
        result, payload = await _load(args.artifact_id, args.url)
        if getattr(result, "isError", False):
            detail = payload.get("_raw") or payload.get("error") or json.dumps(payload)
            print(f"ERROR: get_artifact failed: {detail}", file=sys.stderr)
            return 1
        plaintext = cipher.decrypt(_artifact_content(payload))
    except (EncryptedTextError, EncryptionConfigurationError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.output is not None:
        args.output.write_text(plaintext, encoding="utf-8")
        print(f"decrypted {len(plaintext):,} chars -> {args.output}")
    else:
        sys.stdout.write(plaintext)
        if plaintext and not plaintext.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
