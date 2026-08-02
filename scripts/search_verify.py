#!/usr/bin/env python3
"""Search-only verification helper — confirm what an import landed.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set, else the cached plugin OAuth token, else a one-time
browser approval.

    uv run --with 'mcp>=1.28,<2' python scripts/search_verify.py \
        --query "context agent" --created-after 2026-06-09
"""
from __future__ import annotations

import argparse, asyncio, json, sys
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"))
from _memhub_auth import resolve_url_and_auth  # noqa: E402


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
    ap.add_argument("--query", required=True)
    ap.add_argument("--created-after", default=None)
    ap.add_argument("--memory-type", default="all")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    call_args = {"query": args.query, "top_k": args.top_k, "memory_type": args.memory_type}
    if args.created_after:
        call_args["created_after"] = args.created_after

    url, headers, auth = resolve_url_and_auth()
    async with streamablehttp_client(url, headers=headers, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("search_memory", arguments=call_args)
            found = unwrap(res)
    items = found.get("items", []) if isinstance(found, dict) else []
    print(f"query={args.query!r} created_after={args.created_after} type={args.memory_type}"
          f" -> {len(items)} items")
    for it in items:
        sc = it.get("score")
        sc = f"{sc:.3f}" if isinstance(sc, (int, float)) else sc
        print(f"  [{it.get('type')}] score={sc} {str(it.get('content'))[:120].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
