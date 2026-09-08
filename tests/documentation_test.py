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
# Prose wraps; a phrase check must not depend on where a line happened to end.
README_FLAT = " ".join(README.split())

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


def test_readme_names_every_shipped_skill() -> None:
    """A renamed skill must not silently vanish from the docs.

    The skills list is the only place a user learns a command exists, and a
    skill directory can be renamed without anything else breaking — so the
    directory listing is the source of truth and the README is checked
    against it.
    """
    skills = sorted(p.name for p in (ROOT / "plugins" / "memhub" / "skills").iterdir()
                    if (p / "SKILL.md").is_file())
    for name in skills:
        check(f"README names /memhub:{name}", f"/memhub:{name}" in README)
    check(f"README's skill count matches the {len(skills)} on disk",
          f"{_SPELLED[len(skills)]} skills ship in" in README)


def test_readme_explains_session_pr_linking_and_its_per_host_gaps() -> None:
    for text in (
        # The two properties a user has to be able to trust.
        "a session that opens a PR always links itself",
        "linking is otherwise never automatic for work this session did not do",
        # The undetected paths, said out loud rather than left to be discovered.
        "is not detected, deliberately",
        "Cursor links a session to a pull request with `/memhub:link-pr`",
    ):
        check(f"root README says {text!r}", text in README_FLAT)


_SPELLED = {11: "Eleven", 12: "Twelve", 13: "Thirteen", 14: "Fourteen",
            15: "Fifteen", 16: "Sixteen"}


def test_create_rule_skill_keeps_its_authoring_gates() -> None:
    """The skill is prose, and prose silently loses steps."""
    skill = (ROOT / "plugins" / "memhub" / "skills" / "create-rule"
             / "SKILL.md").read_text(encoding="utf-8")
    for text in ("### 4b.", "scratch worktree", "advise mode",
                 "Never anchor a `command_rx` with `^`",
                 "command position"):
        check(f"create-rule keeps {text!r}", text in skill)


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
