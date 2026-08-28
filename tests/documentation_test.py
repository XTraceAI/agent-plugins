"""Keep public installation docs on the native multi-host plugin path.

The legacy Codex guide told users to create a global MCP entry. That entry
shadows the MCP server bundled by the plugin and, when paired with the old
static OAuth client, fails on Codex's random loopback callback port. These
checks make the supported install and recovery paths part of the test suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CODEX = (ROOT / "codex" / "README.md").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def test_root_readme_is_multi_host() -> None:
    for host in ("Claude Code", "OpenAI Codex", "Cursor"):
        check(f"root README names {host}", host in README)
    check("root README installs the Codex plugin",
          "codex plugin add memhub@xtrace-plugins" in README)
    check("root README points Codex OAuth at CIMD",
          "--oauth-client-registration cimd" in README)


def test_codex_guide_uses_only_the_plugin_server() -> None:
    prohibited = (
        "codex mcp " + "add memhub",
        "[mcp_servers." + "memhub]",
        "--oauth-client-" + "id",
        "marketplace at the repo root doesn't apply",
    )
    for text in prohibited:
        check(f"public guides exclude legacy instruction {text!r}",
              text not in README and text not in CODEX)

    required = (
        "codex plugin marketplace add XTraceAI/agent-plugins",
        "codex plugin add memhub@xtrace-plugins",
        "codex mcp login memhub --oauth-client-registration cimd",
        "codex mcp remove memhub",
        "Log in to MemHub",
        "Set up MemHub",
        "Onboard MemHub for this repo",
    )
    for text in required:
        check(f"Codex guide includes {text!r}", text in CODEX)


if __name__ == "__main__":
    print("documentation")
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("\nall documentation checks passed")
