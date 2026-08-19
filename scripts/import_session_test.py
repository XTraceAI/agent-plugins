#!/usr/bin/env python3
"""Manual end-to-end test of the plugin's session-import path.

Mirrors what the SessionEnd hook does (plugins/memhub/hooks/claude-hooks.json):
read a Claude Code .jsonl transcript, pass the raw records AS-IS to the
`import_conversation` MCP tool with conversation_id=session_id and
source_platform="claude". Then polls `search_memory` to confirm the
background agentic extraction landed.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set, else the cached plugin OAuth token, else a one-time
browser approval.

Run (mcp SDK pulled ephemerally by uv):
    uv run --with 'mcp<2' python scripts/import_session_test.py \
        --session /path/to/<session>.jsonl \
        --max-bytes 800000 \
        --query "context agent creation"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"))
from _memhub_auth import resolve_url_and_auth  # noqa: E402

STAGING_MCP_URL = "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def slice_to_bytes(records: list[dict], max_bytes: int) -> tuple[list[dict], int]:
    """First N records whose cumulative minified size first reaches max_bytes."""
    out, cum = [], 0
    for r in records:
        cum += len(json.dumps(r, separators=(",", ":")))
        out.append(r)
        if cum >= max_bytes:
            break
    return out, cum


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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--max-bytes", type=int, default=800_000,
                    help="cap payload to first records reaching this size (0 = whole file)")
    ap.add_argument("--query", default="", help="search_memory query to verify ingest")
    ap.add_argument("--url", default=STAGING_MCP_URL)
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait before verifying")
    args = ap.parse_args()

    all_records = load_records(args.session)
    if args.max_bytes and args.max_bytes > 0:
        records, payload_bytes = slice_to_bytes(all_records, args.max_bytes)
    else:
        records = all_records
        payload_bytes = len(json.dumps(records, separators=(",", ":")))

    conv_id = args.session.stem  # mirrors hook: conversation_id = session_id
    print(f"session file       : {args.session}")
    print(f"records (total)    : {len(all_records)}")
    print(f"records (sent)     : {len(records)}")
    print(f"payload bytes      : {payload_bytes:,}  (~{payload_bytes // 4:,} tokens)")
    print(f"conversation_id    : {conv_id}")
    print(f"endpoint           : {args.url}")
    print("-" * 60)

    url, headers, auth = resolve_url_and_auth(args.url)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            res = await session.call_tool(
                "import_conversation",
                arguments={
                    "messages": records,
                    "conversation_id": conv_id,
                    "source_platform": "claude",
                },
            )
            imported = unwrap(res)
            print("import_conversation ->")
            print(json.dumps(imported, indent=2))

            if imported.get("path") != "agentic":
                print(f"\n!! expected path='agentic', got {imported.get('path')!r}")

            if args.query:
                print("-" * 60)
                print(f"waiting {args.wait}s for background extraction ...")
                await asyncio.sleep(args.wait)
                sres = await session.call_tool(
                    "search_memory",
                    arguments={"query": args.query, "top_k": 8},
                )
                found = unwrap(sres)
                items = found.get("items", []) if isinstance(found, dict) else []
                print(f"search_memory({args.query!r}) -> {len(items)} items, "
                      f"scope={found.get('scope') if isinstance(found, dict) else '?'}")
                for it in items:
                    print(f"  [{it.get('type')}] score={it.get('score'):.3f} "
                          f"{str(it.get('content'))[:100].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
