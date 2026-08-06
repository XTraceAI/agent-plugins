#!/usr/bin/env python3
"""Store an artifact from a FILE (or stdin) — a terminal operation.

The point: the artifact body is read off disk / the pipe and shipped straight
to the `save_artifact` MCP tool. The model never re-emits the content token by
token — it just runs this with a path, the same way it would `cat` a file.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
else a one-time browser approval. No memhub-cli required.

Run (mcp SDK pulled ephemerally by uv):
    uv run --with mcp python scripts/save_artifact.py \
        --file spec.md --name "Retry Policy Spec" --type spec \
        [--agent-brain-id <id>] [--parent-id <id>] [--rationale "..."] \
        [--tags a,b]

    # Opt-in client-side encryption (passphrase is resolved from the process
    # environment or ~/.config/memhub-plugin/.env; crypto comes from the SDK):
    uv run --with 'mcp<2' --with xtrace-ai-sdk==0.1.1 \
      python scripts/save_artifact.py --file private.md \
        --name "Private notes" --encrypt

    # or pipe terminal output straight in:
    pytest -q | uv run --with mcp python scripts/save_artifact.py \
        --stdin --name "test run 2026-06-09" --type runbook

Endpoint resolution (so the script hits the SAME server the plugin connector
uses, by construction): --url > $MEMHUB_MCP_BASE_URL(+$MEMHUB_MCP_SERVER_PATH) >
the plugin's .mcp.json `mcpServers.*.url` > a default derived from the plugin
install path (prod for `memhub`, staging for `memhub-staging`). There is no
fixed fallback env — see `_memhub_auth.default_url`.
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
from _memhub_crypto import EncryptionConfigurationError  # noqa: E402


def unwrap(result) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
    return {"_raw": str(result)}


def _prepare_content(
    content: str,
    raw_tags: str | None,
    encrypt: bool,
    env_file: Path | None = None,
) -> tuple[str, list[str]]:
    """Return the exact artifact body/tags that will enter the MCP request."""
    tags = [t.strip() for t in (raw_tags or "").split(",") if t.strip()]
    if not encrypt:
        return content, tags

    # Lazy import keeps the existing plaintext path dependency-compatible:
    # callers only need xtrace-ai-sdk when they explicitly request crypto.
    from _memhub_crypto import ENCRYPTED_ARTIFACT_TAG, XTraceTextCipher

    stored_content = XTraceTextCipher.from_env(env_file=env_file).encrypt(content)
    if ENCRYPTED_ARTIFACT_TAG not in tags:
        tags.append(ENCRYPTED_ARTIFACT_TAG)
    return stored_content, tags


async def main() -> int:
    ap = argparse.ArgumentParser(description="Store a file/stdin as a MemHub artifact.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="path to the artifact body")
    src.add_argument("--stdin", action="store_true", help="read body from stdin")
    ap.add_argument(
        "--name", required=True, help="artifact title (re-using a name versions it)"
    )
    ap.add_argument(
        "--type", default="document", help="artifact_type (spec/design_doc/runbook/...)"
    )
    ap.add_argument("--agent-brain-id", default=None)
    ap.add_argument(
        "--parent-id", default=None, help="version an existing artifact by id"
    )
    ap.add_argument(
        "--rationale", default=None, help="why this version supersedes the last"
    )
    ap.add_argument("--tags", default=None, help="comma-separated tags")
    ap.add_argument(
        "--encrypt",
        action="store_true",
        help="AES-encrypt content locally with xtrace-ai-sdk before upload; "
        "requires a configured local passphrase",
    )
    ap.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="private dotenv file for encryption (default: "
        "~/.config/memhub-plugin/.env; direct environment variable wins)",
    )
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    if not args.stdin and not args.file.is_file():
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        return 2
    content = sys.stdin.read() if args.stdin else args.file.read_text(encoding="utf-8")
    if not content.strip():
        print("ERROR: artifact body is empty", file=sys.stderr)
        return 2

    try:
        stored_content, tags = _prepare_content(
            content,
            args.tags,
            args.encrypt,
            args.env_file,
        )
    except EncryptionConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    call_args: dict = {
        "name": args.name,
        "content": stored_content,
        "artifact_type": args.type,
    }
    if args.agent_brain_id:
        call_args["agent_brain_id"] = args.agent_brain_id
    if args.parent_id:
        call_args["parent_id"] = args.parent_id
    if args.rationale:
        call_args["rationale"] = args.rationale
    if tags:
        call_args["tags"] = tags

    url, headers, auth = resolve_url_and_auth(args.url)
    src_desc = "stdin" if args.stdin else str(args.file)
    print(f"source   : {src_desc}  ({len(content):,} plaintext chars)")
    print(f"name     : {args.name}   type={args.type}")
    if args.encrypt:
        print(
            f"encrypted: yes ({len(stored_content):,} chars sent; plaintext stays local)"
        )
    print(f"endpoint : {url}")
    print("-" * 56)

    async with streamablehttp_client(url, headers=headers, auth=auth) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("save_artifact", arguments=call_args)
            out = unwrap(res)
    if getattr(res, "isError", False):
        detail = out.get("_raw") or out.get("error") or json.dumps(out)
        print(f"ERROR: save_artifact failed: {detail}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
