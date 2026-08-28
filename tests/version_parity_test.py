"""Every memhub manifest must declare the SAME version — across every host.

`memhub-staging` shares its scripts, hooks and skills with `memhub` by symlink,
so the code genuinely never drifts. The VERSION does — and that is what gates
delivery: the plugin cache is keyed by version on Claude
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`) AND on Codex
(`~/.codex/plugins/cache/...`), so a manifest that still reads an old number is
never re-fetched. `/plugin update` reports success and installs nothing.

That happened: prod was bumped for a release and staging was not, so everyone on
the staging build silently stayed on code from several releases earlier while
believing they were current. Shared code, unshared version number, no error
anywhere.

Multi-host raises the stakes: `memhub` now carries FIVE version-bearing
manifests (the Agent Plugins 1.0 root manifest, the Claude/Codex/Cursor native
manifests, and staging's Claude manifest). On the unpinned channels (Codex,
Cursor) a version bump reaching `main` IS the release, so a straggler manifest
is a straggler *channel*.

The MCP endpoint must agree too: `mcp.json` (Agent Plugins — read by Codex,
Cursor, and every other AP client) and `.mcp.json` (Claude) both name the
production server. If they diverge, half the hosts talk to a different backend
than the other half — with no error anywhere.

Run: python3 version_parity_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMHUB = ROOT / "plugins" / "memhub"
MANIFESTS = {
    "memhub (AP root)": MEMHUB / "plugin.json",
    "memhub (claude)": MEMHUB / ".claude-plugin" / "plugin.json",
    "memhub (codex)": MEMHUB / ".codex-plugin" / "plugin.json",
    "memhub (cursor)": MEMHUB / ".cursor-plugin" / "plugin.json",
    "memhub-staging (claude)": ROOT / "plugins" / "memhub-staging" / ".claude-plugin" / "plugin.json",
}
AP_SCHEMA_PREFIX = "https://agent-plugins.org/schemas/"
MCP_AP = MEMHUB / "mcp.json"          # Agent Plugins format (Codex, Cursor, …)
MCP_CLAUDE = MEMHUB / ".mcp.json"     # Claude Code format (stdio proxy + backend env)
MCP_STAGING = ROOT / "plugins" / "memhub-staging" / ".mcp.json"


def _load(path: Path) -> dict | None:
    if not path.exists():
        print(f"FAIL missing manifest: {path}")
        return None
    return json.loads(path.read_text())


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
    print("\nok  all manifests declare the same version")

    ap_root = _load(MEMHUB / "plugin.json")
    ap_mcp = _load(MCP_AP)
    claude_mcp = _load(MCP_CLAUDE)
    staging_mcp = _load(MCP_STAGING)
    if (ap_root is None or ap_mcp is None or claude_mcp is None
            or staging_mcp is None):
        return 1

    failures = 0
    for label, data in (("plugin.json", ap_root), ("mcp.json", ap_mcp)):
        schema = data.get("$schema", "")
        if not schema.startswith(AP_SCHEMA_PREFIX):
            print(f"FAIL {label}: $schema is {schema!r}, want {AP_SCHEMA_PREFIX}…")
            failures += 1
    if not failures:
        print("ok  AP manifests carry the agent-plugins.org $schema")

    # Both Claude entries start the same stdio proxy and carry the backend the
    # proxy, the hooks and /memhub:login all read from the env block. A missing
    # key here is a plugin whose tools and capture disagree about where to go.
    PROXY_ARGS = ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_proxy.py"]
    ENV_KEYS = ("MEMHUB_MCP_URL", "MEMHUB_OAUTH_CLIENT_ID",
                "MEMHUB_OAUTH_METADATA_URL", "MEMHUB_OAUTH_CALLBACK_PORT")
    for label, config in (("production", claude_mcp),
                          ("staging", staging_mcp)):
        server = config.get("mcpServers", {}).get("memhub", {})
        env = server.get("env", {})
        missing = [k for k in ENV_KEYS if not env.get(k)]
        if (server.get("type") != "stdio" or server.get("args") != PROXY_ARGS
                or missing):
            print(f"FAIL {label} .mcp.json is not the stdio proxy with a full "
                  f"backend env (type={server.get('type')!r}, "
                  f"args={server.get('args')!r}, missing={missing})")
            failures += 1
        else:
            print(f"ok  {label} starts mcp_proxy.py with its backend declared")

    def server_url(cfg: dict) -> str | None:
        server = cfg.get("mcpServers", {}).get("memhub", {})
        return server.get("url") or server.get("env", {}).get("MEMHUB_MCP_URL")

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
    print("ok  both MCP configs point at the same server")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
