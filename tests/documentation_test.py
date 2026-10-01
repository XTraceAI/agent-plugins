"""Keep public installation docs on the native multi-host plugin path.

The legacy Codex guide told users to create a global MCP entry. That entry
shadows the MCP server bundled by the plugin and, when paired with the old
static OAuth client, fails on Codex's random loopback callback port. These
checks make the supported install and recovery paths part of the test suite.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
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


def test_license_files_present() -> None:
    license_bytes = (ROOT / "LICENSE").read_bytes()
    notice_bytes = (ROOT / "NOTICE").read_bytes()
    check("root ships Apache License 2.0", license_bytes.lstrip().startswith(b"Apache License")
          and b"Version 2.0, January 2004" in license_bytes)
    check("NOTICE names XTrace Inc.", b"XTrace Inc." in notice_bytes)
    with tempfile.TemporaryDirectory() as temp:
        # A git-subdirectory install cannot rely on files at the repository root.
        installed = Path(temp) / "installed"
        shutil.copytree(ROOT / "plugins" / "memhub", installed,
                        ignore=shutil.ignore_patterns("__pycache__"))
        for name, expected in (("LICENSE", license_bytes), ("NOTICE", notice_bytes)):
            source = ROOT / "plugins" / "memhub" / name
            check(f"installed {name} is a real identical copy",
                  not source.is_symlink() and (installed / name).read_bytes() == expected)


def test_manifests_declare_apache_license() -> None:
    plugin = ROOT / "plugins" / "memhub"
    for path in (plugin / "plugin.json", plugin / ".claude-plugin" / "plugin.json",
                 plugin / ".codex-plugin" / "plugin.json", plugin / ".cursor-plugin" / "plugin.json"):
        check(f"{path.relative_to(ROOT)} declares Apache-2.0",
              json.loads(path.read_text(encoding="utf-8")).get("license") == "Apache-2.0")


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
    base = ROOT / "plugins" / "memhub" / "skills" / "find-contributing-sessions"
    skill = (base / "SKILL.md").read_text(encoding="utf-8")
    scanner = (base / "scripts" / "find_sessions.py").read_text(encoding="utf-8")
    flat = " ".join(skill.split())
    # Since ENG-1128 the scanner collects the facts itself (batch mode), so the
    # property moved into the script: step 1 still resolves a bare NUMBER to a
    # URL, and the facts are read from that URL.
    facts = skill[skill.index("## 2. Collect the facts"):skill.index("## 3. One consolidated")]
    check("the skill hands the scanner URLs, not numbers",
          "--prs-from" in facts and "**by URL**" in facts)
    check("…and it says why", "resolves the number against the CURRENT checkout" in flat)
    check("the scanner's gh pr view takes the URL",
          '_gh(["pr", "view", pr["url"], "--json"' in scanner)
    check("…and it checks the answer came from that PR",
          '"url_mismatch"' in scanner and "checks that the `url`" in flat)


def test_the_finder_maps_my_unlinked_prs() -> None:
    """ENG-1128: no argument maps the caller's own unlinked PRs through the
    backend's list tool, `--pr` names specific ones, and without a linked
    GitHub identity the user's own `gh` is the fallback."""
    skill = (ROOT / "plugins" / "memhub" / "skills" / "find-contributing-sessions"
             / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(skill.split())
    for text in ("list_my_unlinked_prs(limit=25)", "--pr <url-or-number>",
                 "gh search prs --author=@me", "`github_identity_linked: false`",
                 "`github_connected: false`", "legacy_branch_sessions",
                 "Link nothing without an explicit yes", "Never send `pr_type`"):
        check(f"find-contributing-sessions says {text!r}", text in flat)
    frontmatter = skill.split("---", 2)[1]
    for spelling in ("mcp__plugin_memhub_memhub__list_my_unlinked_prs",
                     "mcp__plugin_memhub-staging_memhub__list_my_unlinked_prs"):
        check(f"allowed-tools lists {spelling}", spelling in frontmatter)


def test_readme_explains_session_pr_linking_and_its_per_host_gaps() -> None:
    for text in (
        # The properties a user has to be able to trust. The old README
        # promised "a session that opens a PR always links itself" — that
        # claim was retired: unconditional self-linking now requires a
        # `gh pr create` whose returned URL provably came from it, and every
        # other shape falls to the judged lane. Over-promising here is worse
        # than the narrower guarantee, because the failure it hid is an
        # authorship claim nobody can withdraw.
        "Unconditional self-linking is deliberately the **narrow** lane",
        "linking is never automatic for work this session did not do",
        "Heredoc bodies are removed before any of this is decided",
        # The undetected paths, said out loud rather than left to be discovered.
        "is not detected, deliberately",
        "Cursor links a session to a pull request with `/memhub:link-pr`",
    ):
        check(f"root README says {text!r}", text in README_FLAT)


_SPELLED = {11: "Eleven", 12: "Twelve", 13: "Thirteen", 14: "Fourteen",
            15: "Fifteen", 16: "Sixteen", 17: "Seventeen"}


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
    for text in ("### 4b.", "scratch worktree", "the mode the rule will ship with",
                 "Never anchor a `command_rx` with `^`",
                 "command position",
                 # The sub-agent's worktree comes from the Agent tool, not from
                 # a `git worktree add` of our own: the claim is keyed on the
                 # hook payload's cwd, which an Agent-tool sub-agent inherits
                 # from the session — a hand-made worktree it merely "works
                 # inside" never becomes its cwd, and the private ledger ends
                 # the test with zero rows for every rule.
                 'isolation: "worktree"',
                 ".claude/worktrees",
                 "0 rows for EVERY rule",
                 # ENG-1107: the test arms its candidate in a PRIVATE base
                 # claimed for its scratch worktree, never in the book every
                 # other session on the machine reads. These three pin the
                 # claim, the check that it took, and its release — lose any
                 # one and the step is doctoring the shared book again.
                 "pretest-redirect.json",
                 "must print a path under",
                 "Release the claim",
                 # The claim keeps other sessions out, but the skill's own
                 # setup runs inside the same worktree, so a candidate matching
                 # cp/git/python3/rm can still fire on the parent's shell.
                 "must be the sub-agent's rather than the parent's",
                 # Step 0 rule 3: ambiguity resolves to the repo's own book
                 # instead of "file nothing", which read as fail-closed and was
                 # not — on two bound all_org books it refused once and picked
                 # a book every other time.
                 "the repo's own book",
                 "create_rulebook",
                 # and the trap under it: an ORG ADMIN gets an empty book by
                 # default, and a book that binds nobody serves nobody.
                 "member_count: 0",
                 "binds nobody"):
        check(f"create-rule keeps {text!r}", text in skill)



def test_the_authoring_skills_send_the_rule_judge_its_four_fields() -> None:
    """A rule filed without `when` / `do` / `why` arrives empty and the server's
    rule judge falls back to its statement (rule-judge-spec §2). Both skills
    author rules, so both must say the fields, where each is written, and that
    they reach `create_rule` — on the person's path and on the silent one."""
    skills = ROOT / "plugins" / "memhub" / "skills"
    create = (skills / "create-rule" / "SKILL.md").read_text(encoding="utf-8")

    def part(text: str, start: str, end: str) -> str:
        check(f"the skill still has {start!r} … {end!r}", start in text and end in text[text.index(start):]
              if start in text else False)
        if start not in text:
            return ""
        return text[text.index(start):].split(end, 1)[0]

    pin = part(create, "### 1. Pin the rule sentence", "### 1b.")
    for field, cap in (("`when`", "300 chars"), ("`do`", "400 chars"), ("`why`", "400 chars"),
                       ("`when_not`", "8 entries, 200 chars each")):
        check(f"create-rule step 1 defines {field} with its cap ({cap})",
              any(field in line and cap in line for line in pin.splitlines()))
    check("…says `when` is a situation, never trigger vocabulary, with a good and a bad example",
          "never trigger vocabulary" in pin and "Good:" in pin and "Bad:" in pin)
    check("…says `when_not` is only for exclusions actually named, otherwise omitted",
          "actually\n  named" in pin and "Otherwise omit it" in pin)
    check("…and that the statement and the fields say the same thing",
          "nothing in one that the other lacks" in pin)
    draft = part(create, "### 3. Draft the rule", "### 4. Prove it fires")
    check("create-rule keeps the pattern broad and leaves the situation to `when` / `when_not`",
          "The pattern stays broad; the judge decides fit" in draft and "wrong **shape**" in draft
          and "wrong **situation**" in draft and "Do not\n  narrow a pattern" in draft)
    file_ = part(create, "### 5. Conflict check, confirm, then file", "### 6. Report")
    check("create-rule shows the four fields in the preview the person approves",
          all(label in file_ for label in ("When:      <when>", "Do:        <do>", "Why:       <why", "Not when:  <each")))
    check("create-rule passes them in the create_rule call",
          "with `title`, `statement`, `when`, `do`, `why`" in file_ and "`when_not` when an exclusion was\nnamed" in file_)
    check("…and says a replacing rule inherits the ones it does not name, so a changed situation is re-stated",
          "inherits what it does not name" in file_ and "re-state `when`" in file_)
    harness = part(create, "## Handed a turn by the harness", "## 0. Which rulebook")
    check("the harness draft's call carries `when`, `do`, `why`",
          "`rulebook_id`, `title`, `statement`, `when`, `do`, `why`" in harness)
    check("…written without asking", "`when`, `do` and `why` are written here without asking" in harness)

    start = (skills / "start-rulebook" / "SKILL.md").read_text(encoding="utf-8")
    declared = part(start, "## 2. Give CLAUDE.md its checks", "## 3. Facet pass")
    check("start-rulebook's CLAUDE.md candidate carries when / do / why",
          all(f'"{k}": "' in declared for k in ("when", "do", "why")))
    check("…from the same origin sentence as `did` and `what`, as a situation and not the check's words",
          "same origin sentence as `did` and `what`" in declared and "is the SITUATION" in declared
          and "Never narrow a pattern" in declared)
    clusters = part(start, "## 3. Facet pass", "## 4. Second pass")
    check("start-rulebook's friction clusters carry them too, from the friction details",
          "`when` / `do` / `why` as in step 2" in clusters and "friction details" in clusters)
    filing = part(start, "- **Rules**: `create_rule` once per row", "- **CLAUDE.md**:")
    check("start-rulebook passes a mined row's context to create_rule",
          "the row's `context`" in filing and "`when`, `do`, `why`, `when_not`" in filing)
    check("…and a starter body's fields as they are", "pass\n     them as they are" in filing)
    banned = part(start, "Words that never reach the user:", "They live in")
    for word in ("precision", "applies-in", "gated", "receipt", "demoted", "matcher", "ordering", "predicate", "delivery"):
        check(f"start-rulebook still bans {word!r} from what the user reads", word in banned)


# ENG-1178: the backend MCP surface is 34 tools, cut over with no aliases. A
# shipped file that still names a removed tool (or the old `memory_type`
# parameter) teaches the agent a call the server refuses.
_MCP_SURFACE = {
    "list_agent_brains", "get_brain_overview", "search_memory", "read_memory",
    "get_artifact_lineage", "list_tags", "save_artifact", "ingest_document_from_url",
    "tag_memory", "copy_to_agent_brain", "delete_memory", "create_agent_brain",
    "share_agent_brain", "list_agent_brain_access", "list_brain_folders",
    "move_brain_to_folder", "list_sessions", "add_memory", "import_conversation",
    "list_rulebooks", "create_rulebook", "list_rules", "create_rule", "link_pr",
    "unlink_pr", "list_my_unlinked_prs", "get_spec_workflow", "list_skills",
    "get_skill", "create_skill", "list_orgs", "list_workspaces", "create_workspace",
    "list_teammates",
}
_REMOVED_MCP = (
    "get_artifact", "search_all_brains", "search_brains", "refresh_brain_overview",
    "diff_artifact_versions", "tag_artifact", "tag_document", "copy_memory_to_brain",
    "add_skill_to_brain", "delete_artifact", "delete_episode", "delete_skill",
    "share_agent_brain_with_workspace", "create_brain_folder",
    "remove_brain_from_folder", "get_skill_file", "memory_type",
)


def test_mcp_surface_is_34_tools() -> None:
    check("the surface this test pins is 34 tools", len(_MCP_SURFACE) == 34)


def test_shipped_files_name_no_removed_mcp_tool() -> None:
    import re
    plugin = ROOT / "plugins" / "memhub"
    pat = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(_REMOVED_MCP) + r")(?![A-Za-z0-9_])")
    hits = []
    for f in sorted(plugin.rglob("*")):
        if f.is_file() and f.suffix in {".md", ".py", ".json", ".sh", ".toml", ".yaml", ".yml"}:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{n}")
    check(f"no shipped file names a removed MCP tool or memory_type {hits[:5]}", not hits)


def test_skill_allowed_tools_are_on_the_surface() -> None:
    import re
    skills = ROOT / "plugins" / "memhub" / "skills"
    for skill in sorted(skills.glob("*/SKILL.md")):
        front = skill.read_text(encoding="utf-8").split("---", 2)[1]
        line = next((ln for ln in front.splitlines() if ln.startswith("allowed-tools:")), "")
        names = re.findall(r"mcp__plugin_(memhub|memhub-staging)_memhub__([a-z_]+)", line)
        off = sorted({t for _, t in names if t not in _MCP_SURFACE})
        check(f"{skill.parent.name} allowed-tools name only real tools {off}", not off)
        by_prefix = {p: {t for q, t in names if q == p} for p in ("memhub", "memhub-staging")}
        check(f"{skill.parent.name} allowed-tools grant both prefixes alike",
              by_prefix["memhub"] == by_prefix["memhub-staging"])


def test_search_memory_skill_teaches_the_ladder() -> None:
    skill = (ROOT / "plugins" / "memhub" / "skills" / "search-memory"
             / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(skill.split())
    body = skill.split("---", 2)[2]
    i_over, i_search, i_read = (body.find("`get_brain_overview(agent_brain_id)`"),
                                body.find("**Search for pointers.**"),
                                body.find("**Open the one you picked with `read_memory(id)`.**"))
    check("search-memory: overview → search → read_memory, in that order",
          -1 < i_over < i_search < i_read)
    for text in ('`kind`: `"artifact"`', "section_id", "POINTERS only",
                 "**Episodes are never searched with `agent_brain_id`**", "all_brains=true",
                 "list_sessions"):
        check(f"search-memory says {text!r}", text in flat)
    check("search-memory offers no facts to search", '"facts"' not in skill and '"fact"' not in skill)


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
