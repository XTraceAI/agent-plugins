#!/usr/bin/env python3
"""Read-only release gate against production, using the candidate's actual fetch/cache.

No saved login, origin override, fixture creation, or retry-until-green. Run only
with reviewed candidate code and a dedicated production test identity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

PRODUCTION = "https://api.memhub.xtrace.ai"
RULES_PATH = "/v1/team/rulebook/rules"
MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json",
             ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json")


class GateError(Exception):
    """Only fixed, credential-free diagnostics may reach the report."""


def require(condition, message):
    if not condition:
        raise GateError(message)


def fixture_config(raw):
    try:
        fixture = json.loads(raw)
        require(fixture["schema_version"] == 1, "unsupported fixture schema")
        UUID(fixture["org_id"])
        require(isinstance(fixture["repo"], str) and 0 < len(fixture["repo"]) <= 200,
                "fixture repo is required")
        rules = fixture["rules"]
        require(isinstance(rules, list) and bool(rules), "fixture rules must not be empty")
        for rule in rules:
            UUID(rule["id"])
            require(rule["mode"] in {"gate", "advise"}, "invalid fixture rule mode")
        require(len({r["id"] for r in rules}) == len(rules), "duplicate fixture rule IDs")
        return fixture
    except (KeyError, TypeError, ValueError):
        raise GateError("MEMHUB_PROD_FIXTURE_JSON must contain the provisioned fixture configuration") from None


def package_digest(root):
    digest = hashlib.sha256()
    require(root.is_dir() and not root.is_symlink(), "candidate package is missing or symlinked")
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "production package contains a symlink")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def package_version(root):
    versions = []
    for relative in MANIFESTS:
        data = json.loads((root / relative).read_text())
        require(data.get("name") == "memhub", "candidate has a non-production plugin identity")
        version = data.get("version")
        require(isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version),
                "candidate version must have three numeric components")
        versions.append(version)
    require(len(set(versions)) == 1, "candidate manifest versions disagree")
    servers = json.loads((root / ".mcp.json").read_text()).get("mcpServers")
    require(isinstance(servers, dict) and set(servers) == {"memhub"}, "unexpected MCP configuration")
    require((servers["memhub"].get("url") or "").split("?", 1)[0] == PRODUCTION + "/mcp-server/mcp",
            "candidate MCP must point to production")
    return versions[0]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(root, fixture, token):
    root = root.resolve()
    require(bool(token) and "\n" not in token and "\r" not in token,
            "MEMHUB_PROD_READ_TOKEN is required and must be a raw bearer token")
    require("MEMHUB_RULEBOOK_HOOK_VERSION" not in os.environ,
            "candidate version override is forbidden")
    version = package_version(root)
    before = package_digest(root)
    scripts = root / "scripts"
    calls = []
    with tempfile.TemporaryDirectory(prefix="memhub-prod-gate-") as temp:
        # Set before importing so breadcrumbs cannot touch a developer's cache.
        previous = os.environ.get("MEMHUB_RULEBOOK_BASE")
        os.environ["MEMHUB_RULEBOOK_BASE"] = temp
        sys.path.insert(0, str(scripts))
        try:
            transport = load_module("production_candidate_http", scripts / "mcp_http.py")
            hook = load_module("production_candidate_hook", scripts / "rulebook_hook.py")
            require(hook.hook_version() == tuple(map(int, version.split("."))),
                    "candidate reports a version different from its manifest")

            class Transport:
                @staticmethod
                def rest(url, bearer, method="GET", **kwargs):
                    target = urlsplit(url)
                    require(target.scheme == "https" and target.netloc == urlsplit(PRODUCTION).netloc
                            and not target.fragment and target.path == RULES_PATH and method == "GET",
                            "candidate attempted an unexpected request")
                    require(parse_qs(target.query) == {"view": ["hook"], "repo": [fixture["repo"]],
                                                       "hook_version": [version]},
                            "candidate query differs from the supported contract")
                    require(bearer == token and not kwargs.get("body"), "unexpected request payload")
                    headers = dict(kwargs.pop("headers", {}) or {})
                    require(set(headers) <= {"If-None-Match"}, "unexpected request headers")
                    conditional = headers.get("If-None-Match")
                    headers["X-Org-Id"] = fixture["org_id"]
                    reply = transport.rest(url, bearer, method, headers=headers, **kwargs)
                    calls.append((reply.status, conditional))
                    return reply

            hook._api = lambda: (PRODUCTION, token, Transport)
            hook.fetch_book(fixture["repo"], timeout=15)
            cold = hook.load_book(fixture["repo"])
            require(calls == [(200, None)] and cold and cold.get("etag"),
                    "production cold fetch failed (credentials, version policy, response, or ETag)")
            # The hook view names a rule `rule_id`; `id` is only the fixture's own key.
            # A row missing either field is a contract change, reported by name rather
            # than as a KeyError the generic handler would hide.
            require(all(isinstance(r, dict) and isinstance(r.get("rule_id"), str) and "mode" in r
                        for r in cold["rules"]),
                    "production rules lack rule_id or mode (hook view contract changed)")
            actual = sorted((r["rule_id"], r["mode"]) for r in cold["rules"])
            expected = sorted((r["id"], r["mode"]) for r in fixture["rules"])
            require(actual == expected, "production fixture rules or modes do not match")
            cold["fetched_at"] = "2000-01-01T00:00:00+00:00"
            hook._atomic_json(hook.book_path(fixture["repo"]), cold)
            hook.fetch_book(fixture["repo"], timeout=15)
            warm = hook.load_book(fixture["repo"])
            require(calls == [(200, None), (304, cold["etag"])], "production ETag revalidation failed")
            require(warm and warm["rules"] == cold["rules"] and warm["etag"] == cold["etag"]
                    and warm.get("fetched_at") and warm["fetched_at"] != cold["fetched_at"],
                    "candidate did not preserve and refresh its validated cache")
        finally:
            sys.path.pop(0)
            if previous is None:
                os.environ.pop("MEMHUB_RULEBOOK_BASE", None)
            else:
                os.environ["MEMHUB_RULEBOOK_BASE"] = previous
    require(package_digest(root) == before, "candidate package changed during verification")
    return {"ok": True, "plugin_version": version, "package_sha256": before,
            "production_origin": PRODUCTION, "checked_at": datetime.now(timezone.utc).isoformat(),
            "fixture_sha256": hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest(),
            "checks": ["production_package", "cold_fetch", "fixture_ids_and_modes", "etag_revalidation"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True, type=Path, help="Actual plugin directory, not repository root")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        require(bool(re.fullmatch(r"[0-9a-f]{40}", args.source_sha)), "source SHA must be a full commit ID")
        fixture = fixture_config(os.environ.get("MEMHUB_PROD_FIXTURE_JSON", ""))
        result = probe(args.plugin_root, fixture, os.environ.get("MEMHUB_PROD_READ_TOKEN", ""))
        result["source_sha"] = args.source_sha
    except Exception as exc:
        # HTTP bodies, fixture content, credentials, and candidate exceptions are private.
        result = {"ok": False, "error": str(exc) if isinstance(exc, GateError) else "production check failed"}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
