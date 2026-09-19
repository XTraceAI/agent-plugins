#!/usr/bin/env python3
"""Local cross-repo probe: actual plugin request, transport, parser and cache.

Authentication is supplied by the backend test fixture. This intentionally only
accepts loopback URLs; production credentials/release gating are a later step.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_contract(backend):
    lock = json.loads((ROOT / "contracts/rulebook.lock.json").read_text())
    relative = lock["path"]
    pinned = subprocess.run(
        ["git", "-C", str(backend), "show", f"{lock['revision']}:{relative}"],
        capture_output=True, check=True,
    ).stdout
    require(hashlib.sha256(pinned).hexdigest() == lock["sha256"], "contract digest mismatch")
    require((backend / relative).read_bytes() == pinned, "backend contract differs from pinned revision")
    owns = lock.get("spec_owns")
    if owns:
        fixture = subprocess.run(["git", "-C", str(backend), "show", f"{lock['revision']}:{owns['path']}"], capture_output=True, check=True).stdout
        require(hashlib.sha256(fixture).hexdigest() == owns["sha256"], "spec ownership fixture digest mismatch")
        require((ROOT / "contracts/spec-owns.json").read_bytes() == fixture, "plugin spec fixture differs from backend pin")
        require((backend / owns["path"]).read_bytes() == fixture, "backend spec fixture differs from pin")
        parser = (backend / "app/services/spec_owns.py").read_bytes()
        require(hashlib.sha256(parser).hexdigest() == owns["parser_sha256"], "backend spec parser differs from plugin contract")
    return json.loads(pinned)


def probe(base, org, contract, plugin_root=ROOT):
    parts = urlsplit(base)
    require(parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment and parts.path in {"", "/"},
            "probe requires an HTTP loopback origin")
    token = os.environ.get("MEMHUB_CONTRACT_TOKEN")
    require(bool(token), "MEMHUB_CONTRACT_TOKEN is required")
    require("MEMHUB_RULEBOOK_HOOK_VERSION" not in os.environ,
            "candidate version override is forbidden")
    scripts = Path(plugin_root).resolve() / "plugins/memhub/scripts"
    sys.path.insert(0, str(scripts))
    import mcp_http

    spec = importlib.util.spec_from_file_location("contract_candidate", scripts / "rulebook_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    manifest = json.loads((scripts.parent / ".claude-plugin/plugin.json").read_text())
    version = manifest["version"]
    require(hook.hook_version() == tuple(int(n) for n in version.split(".")),
            "reported version differs from manifest")
    require(contract["contract_id"] == "memhub.rulebook.fetch.v1", "unsupported contract")
    require(hook.hook_version() >= tuple(int(n) for n in contract["rule_floor"].split(".")),
            "candidate is below fixture feature floor")
    calls = []

    class Transport:
        @staticmethod
        def rest(url, bearer, method="GET", **kwargs):
            target = urlsplit(url)
            require((target.scheme, target.netloc) == (parts.scheme, parts.netloc),
                    "candidate attempted a different origin")
            require(method == "GET" and target.path == contract["path"], "unexpected request")
            require(parse_qs(target.query) == {
                "view": ["hook"], "repo": [contract["repo"]], "hook_version": [version],
            }, "candidate query differs from contract")
            headers = dict(kwargs.pop("headers", {}) or {})
            conditional = headers.get("If-None-Match")
            headers["X-Org-Id"] = org
            reply = mcp_http.rest(url, bearer, method, headers=headers, **kwargs)
            calls.append({"status": reply.status, "conditional": conditional})
            return reply

    with tempfile.TemporaryDirectory(prefix="rulebook-contract-") as cache:
        hook.BASE = cache
        hook.BOOK_DIR = str(Path(cache) / "book")
        # Inject fixture authentication, preserving the candidate HTTP implementation.
        hook._api = lambda: (base.rstrip("/"), token, Transport)
        repo = contract["repo"]
        hook.fetch_book(repo, timeout=10)
        cold = hook.load_book(repo)
        require(len(calls) == 1 and calls[0] == {"status": 200, "conditional": None},
                "cold fetch did not complete with HTTP 200")
        require(bool(cold) and bool(cold.get("etag")), "cold cache or ETag missing")
        require(sorted(r["title"] for r in cold["rules"]) == contract["expected_titles"],
                "fixture rules missing or unexpected rules included")
        require(all(r["mode"] == "gate" for r in cold["rules"]), "fixture mode mismatch")
        # A sentinel avoids a timing-sensitive sleep and proves revalidation occurred.
        cold["fetched_at"] = "2000-01-01T00:00:00+00:00"
        hook._atomic_json(hook.book_path(repo), cold)
        hook.fetch_book(repo, timeout=10)
        warm = hook.load_book(repo)
        require(len(calls) == 2 and calls[1] == {"status": 304, "conditional": cold["etag"]},
                "unchanged fixture did not revalidate with HTTP 304")
        require(warm["rules"] == cold["rules"] and warm["etag"] == cold["etag"],
                "304 changed cached rules or ETag")
        require(bool(warm.get("fetched_at")) and warm["fetched_at"] != cold["fetched_at"],
                "304 did not refresh validation time")
    return {"ok": True, "contract_id": contract["contract_id"], "plugin_version": version,
            "checks": ["cold_fetch", "fixture_scope_and_mode", "etag_revalidation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-root", required=True, type=Path)
    parser.add_argument("--base", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--plugin-root", type=Path, default=ROOT,
                        help="Plugin source checkout to certify (may be an older release without this probe)")
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.base, args.org, read_contract(args.backend_root), args.plugin_root)))
        return 0
    except Exception as exc:
        # Never echo HTTP bodies, credentials, or full transport exceptions.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"ok": False, "error": message}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
