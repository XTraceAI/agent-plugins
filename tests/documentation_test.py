"""Keep public installation docs on the native multi-host plugin path.

The legacy Codex guide told users to create a global MCP entry. That entry
shadows the MCP server bundled by the plugin and, when paired with the old
static OAuth client, fails on Codex's random loopback callback port. These
checks make the supported install and recovery paths part of the test suite.
"""
from __future__ import annotations

import json
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


def test_the_finder_collects_facts_from_the_pr_it_was_given() -> None:
    """`gh pr view <n>` resolves the number against the CURRENT checkout, so a
    URL for another repo would have ranked sessions against a different pull
    request and then linked them to the one the user named (Codex, #182)."""
    skill = (ROOT / "plugins" / "memhub" / "skills" / "find-contributing-sessions"
             / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(skill.split())
    # Step 1 resolving a bare NUMBER against the current repo is correct and
    # stays; it is the facts section that must use the resolved URL.
    facts = skill[skill.index("## 2. Collect the PR's facts"):skill.index("## 3. Run the scanner")]
    check("the facts commands take the URL, not the bare number",
          'gh pr view "$PR_URL" --json' in facts and "gh pr view <n> --json" not in facts)
    check("…and it says why", "resolves the number against the CURRENT checkout" in flat)
    check("…and it checks the answer came from that PR",
          "Sanity-check the `url` that comes back" in flat)


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


def test_the_specs_do_not_prescribe_patterns_the_code_rejects() -> None:
    """A spec is the sole source of truth by its own header, so a pattern left
    in it that the implementation has since rejected is a trap for the next
    implementer — it reintroduces the bug and looks authorised (Codex, #182)."""
    specs = (ROOT / "docs" / "specs" / "pr-linking-plugin-spec.md").read_text(encoding="utf-8")
    rulebook = (ROOT / "docs" / "specs"
                / "rulebook-disclosure-and-authoring-spec.md").read_text(encoding="utf-8")
    # A pattern may still be NAMED as known-bad — that is how the next
    # implementer learns not to reach for it. What must not survive is a
    # pattern presented as the rule to implement, so each dead one is allowed
    # only inside the paragraph that warns against it.
    warning = specs[specs.index("**Do not do this with one regex.**"):][:1200]
    for dead in ("mcp__[^_]*", "[^_]*[Gg]it[Hh]ub[^_]*",
                 "(create|open|submit).*(pull.?request"):
        outside = specs.replace(warning, "")
        check(f"pr-linking spec no longer prescribes {dead!r}", dead not in outside)
    # Prose wraps; a phrase check must not depend on where a line ended.
    flat = " ".join(specs.split())
    check("…and it says why, so the pattern is not reached for again",
          "was the first attempt" in flat and "cannot take back" in flat)
    check("the rulebook spec no longer prescribes a detached forward test",
          "worktree add --detach" not in rulebook)
    # …and the manifest the spec quotes must be the manifest that ships.
    shipped = (ROOT / "plugins" / "memhub" / "hooks"
               / "claude-hooks.json").read_text(encoding="utf-8")
    for group in json.loads(shipped)["hooks"]["PostToolUse"]:
        for hook in group["hooks"]:
            if "pr_link_trigger" in hook.get("command", ""):
                check(f"the spec quotes the shipped matcher {group['matcher']}",
                      group["matcher"] in specs)
                guard = 'case \"$IN\" in *gh*pr*|*[Gg]it[Hh]ub*|*api/v3*|*repos/*pulls*)'
                check("…and the shipped case guard", guard in hook["command"])


def test_create_rule_skill_keeps_its_authoring_gates() -> None:
    """The skill is prose, and prose silently loses steps."""
    skill = (ROOT / "plugins" / "memhub" / "skills" / "create-rule"
             / "SKILL.md").read_text(encoding="utf-8")
    for text in ("### 4b.", "scratch worktree", "advise mode",
                 "Never anchor a `command_rx` with `^`",
                 "command position",
                 # The ledger window must bracket the sub-agent, not the whole
                 # step — otherwise the skill's own setup fires the candidate.
                 "immediately before the Agent call",
                 "worktree add -b",
                 # The ledger is shared per repo, so another live session can
                 # fire the armed candidate inside the window.
                 "must be the sub-agent's rather than the parent's",
                 # An interrupted run leaves a doctored book that SessionStart
                 # deliberately considers fresh — it does not heal itself.
                 'ls "$BOOK".pretest-*',
                 # …and the no-original case needs its own marker, or an
                 # interruption there leaves an armed candidate undiscoverable.
                 "$BOOK.pretest-absent"):
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
