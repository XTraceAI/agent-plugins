"""Production MemHub manifests must declare the same version across hosts.

Staging has a separate release cadence; its manifest is validated independently
so a production release cannot implicitly publish a staging version.

The MCP endpoint must agree too: `mcp.json` (Agent Plugins — read by Codex,
Cursor, and every other AP client) and `.mcp.json` (Claude) both name the
production server. If they diverge, half the hosts talk to a different backend
than the other half — with no error anywhere.

Manifests must also parse with UNIQUE keys. JSON parsers keep the LAST
occurrence of a duplicate key, so an edit that adds a rewritten field without
deleting the old line ships the OLD text while the new wording sits dead in
the file. That happened too: the v0.29.1 audit pass left four memhub manifests
carrying two "description" keys each, and every host kept rendering the stale
one.

Run: python3 version_parity_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMHUB = ROOT / "plugins" / "memhub"
MANIFESTS = {
    "memhub (AP root)": MEMHUB / "plugin.json",
    "memhub (claude)": MEMHUB / ".claude-plugin" / "plugin.json",
    "memhub (codex)": MEMHUB / ".codex-plugin" / "plugin.json",
    "memhub (cursor)": MEMHUB / ".cursor-plugin" / "plugin.json",
}
AP_SCHEMA_PREFIX = "https://agent-plugins.org/schemas/"
MCP_AP = MEMHUB / "mcp.json"          # Agent Plugins format (Codex, Cursor, …)
MCP_CLAUDE = MEMHUB / ".mcp.json"     # Claude Code format (carries oauth)


def _reject_dupes(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    dupes = sorted({key for key in keys if keys.count(key) > 1})
    if dupes:
        raise ValueError(f"duplicate keys {dupes}: parsers keep the last "
                         "occurrence, so the earlier value silently never "
                         "renders")
    return dict(pairs)


def _load(path: Path) -> dict | None:
    if not path.exists():
        print(f"FAIL missing manifest: {path}")
        return None
    try:
        return json.loads(path.read_text(), object_pairs_hook=_reject_dupes)
    except ValueError as exc:  # JSONDecodeError is a ValueError too
        print(f"FAIL {path}: {exc}")
        return None


def main() -> int:
    versions = {}
    for name, path in MANIFESTS.items():
        data = _load(path)
        if data is None:
            return 1
        versions[name] = data.get("version")
        print(f"  {name:<26} {versions[name]}")

    distinct = set(versions.values())
    if len(distinct) != 1 or None in distinct:
        print("\nFAIL versions differ — the stale manifest's channel will not be\n"
              "     re-fetched by its client (caches key on version), and on the\n"
              "     unpinned channels the bump itself is the release.\n"
              "     Bump ALL manifests together.")
        return 1
    print("\nok  all production manifests declare the same version")

    ap_root = _load(MEMHUB / "plugin.json")
    ap_mcp = _load(MCP_AP)
    claude_mcp = _load(MCP_CLAUDE)
    if ap_root is None or ap_mcp is None or claude_mcp is None:
        return 1

    failures = 0
    for label, data in (("plugin.json", ap_root), ("mcp.json", ap_mcp)):
        schema = data.get("$schema", "")
        if not schema.startswith(AP_SCHEMA_PREFIX):
            print(f"FAIL {label}: $schema is {schema!r}, want {AP_SCHEMA_PREFIX}…")
            failures += 1
    if not failures:
        print("ok  AP manifests carry the agent-plugins.org $schema")

    for label, config in (("production", claude_mcp),):
        server = config.get("mcpServers", {}).get("memhub", {})
        cursor_client = server.get("auth", {}).get("CLIENT_ID")
        capture_client = server.get("oauth", {}).get("clientId")
        if not cursor_client or cursor_client != capture_client:
            print(f"FAIL {label} MCP OAuth clients disagree: "
                  f"auth.CLIENT_ID={cursor_client!r}, "
                  f"oauth.clientId={capture_client!r}")
            failures += 1
        else:
            print(f"ok  {label} keeps Cursor auth and capture OAuth aligned")

    def server_url(cfg: dict) -> str | None:
        server = cfg.get("mcpServers", {}).get("memhub", {})
        return server.get("url")

    ap_url, claude_url = server_url(ap_mcp), server_url(claude_mcp)
    print(f"  mcp.json   → {ap_url}")
    print(f"  .mcp.json  → {claude_url}")
    # Compare ignoring query string: the AP entry may carry an install-channel
    # tag (?client=…) without pointing anywhere different.
    strip = lambda u: (u or "").split("?")[0]
    if not ap_url or strip(ap_url) != strip(claude_url):
        print("\nFAIL MCP endpoints disagree — AP-installed hosts (Codex, Cursor)\n"
              "     would talk to a different backend than Claude installs.")
        return 1
    for config, expected in ((ap_mcp, versions["memhub (AP root)"]),
                             (claude_mcp, versions["memhub (claude)"])):
        reported = parse_qs(urlsplit(server_url(config)).query).get("memhub_plugin_version")
        if reported != [expected]:
            print(f"FAIL loaded MCP connection must report package version {expected}, got {reported}")
            failures += 1
    print("ok  both MCP configs point at the same server")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
