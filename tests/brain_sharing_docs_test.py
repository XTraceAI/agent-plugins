"""A brain is reachable by its creator and explicit grants — nothing else.

Since ENG-901, membership of the workspace a brain lives in grants no access to
it. The plugin's docs were written against the older model and told the agent
the opposite, which failed in the direction nobody notices: the room resolution
ladder in `references/repo-brain.md` says to reuse a teammate's repo room "if
found", but both of its detection steps (`list_agent_brains`, `search_brains`)
only ever see brains the caller can already reach. An unshared teammate's room
is invisible to both, so every teammate fell through to step 4 and minted a
private room of the same name — the exact duplicate-brain degradation the same
file warns about four lines later.

These checks pin the two halves of the fix so it cannot rot back:

* no plugin-facing doc claims workspace membership reaches a brain, and every
  place that says a lookup may find a teammate's room also says a miss proves
  nothing;
* the skills can actually CALL the bulk share. `allowed-tools` is an allowlist
  — an MCP tool absent from it is invisible inside the skill, however clearly
  the prose asks for it — and the two plugins ship one symlinked skills tree,
  so every tool must be listed under BOTH server prefixes or the staging build
  silently loses it.

Run: python3 brain_sharing_docs_test.py  (stdlib only).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "plugins" / "memhub" / "skills").glob("*/SKILL.md"))
REPO_BRAIN = ROOT / "plugins" / "memhub" / "references" / "repo-brain.md"
README = ROOT / "README.md"
BULK_SHARE = "share_agent_brain_with_workspace"

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def _prose_files() -> list[Path]:
    """Everything an agent or a user reads: skills, references, the README."""
    return SKILLS + sorted((ROOT / "plugins" / "memhub" / "references").glob("*.md")) + [README]


def _normalized(path: Path) -> str:
    """Collapse newlines — these claims wrap, so a raw substring match is vacuous."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def _allowed_tools(path: Path) -> list[tuple[str, str]]:
    """(server, tool) pairs from a SKILL.md's frontmatter allowlist."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^allowed-tools:(.*?)^---$", text, re.M | re.S)
    if not match:
        return []
    return re.findall(r"mcp__plugin_(memhub(?:-staging)?)_memhub__(\w+)", match.group(1))


def test_no_doc_promises_access_through_a_workspace() -> None:
    # Phrasings that assert reach a workspace does not confer. Each is the
    # shape of a claim, not one file's wording, so a reworded relapse is caught.
    retired = (
        "everyone in that workspace can then contribute",
        "everyone in the workspace can contribute",
        "workspace can then contribute to the agent brain",
        "you keep the contributor access that sharing requires",
        "access inherited implicitly from workspace membership",
        "workspace grants are implicit",
    )
    for path in _prose_files():
        text = _normalized(path).lower()
        for claim in retired:
            check(f"{path.relative_to(ROOT)} drops {claim!r}", claim not in text)


def test_a_lookup_miss_is_never_left_looking_conclusive() -> None:
    # Any file inviting reuse of a teammate's room must also say that finding
    # nothing proves nothing — that asymmetry is the whole bug.
    for path in _prose_files():
        text = _normalized(path).lower()
        if "teammate may have created" not in text:
            continue
        hedged = (
            "not proof" in text
            or "does not prove" in text
            or "means only that no room" in text
        )
        check(f"{path.relative_to(ROOT)} says a miss proves nothing", hedged)


def test_repo_brain_reference_carries_the_sharing_rule() -> None:
    text = _normalized(REPO_BRAIN)
    check("repo-brain names the bulk share tool", BULK_SHARE in text)
    # Deterministic selector, not "the workspace that looks like the team's".
    check("repo-brain identifies the default workspace by flag",
          "is_default: true" in text and "is_personal: false" in text)
    check("repo-brain requires confirmation before granting",
          "Confirm before granting" in text)
    check("repo-brain warns the share is a snapshot", "snapshot" in text.lower())
    check("repo-brain keeps the omit-workspace_id rule",
          "Omit `workspace_id`" in text)
    # Degrading matters more than the happy path: prod runs a backend without
    # the bulk tool until 1144 ships, and the two plugins share this file.
    check("repo-brain names the per-person fallback",
          "list_teammates" in text and "`share_agent_brain`" in text)
    check("repo-brain forbids failing the caller's task over sharing",
          "never fail the caller's task over it" in text)


def test_onboard_offers_the_share_and_degrades() -> None:
    onboard = next(p for p in SKILLS if p.parent.name == "onboard")
    text = _normalized(onboard)
    check("onboard calls the bulk share", BULK_SHARE in text)
    check("onboard only shares a room it created",
          "Only do this when you CREATED the room" in text)
    check("onboard asks before granting the org access",
          "Ask before granting" in text)
    check("onboard names both permission levels",
          "`contributor`" in text and "`viewer`" in text)
    check("onboard never fails onboarding over sharing",
          "never fail onboarding over this" in text.lower())


def test_a_skill_that_shares_can_call_every_tool_that_takes() -> None:
    """The whole sharing path must be reachable, fallback included.

    `allowed-tools` is an allowlist: a tool absent from it does not exist
    inside the skill, so prose instructing a call it cannot make is a silent
    no-op at runtime — the same shape of failure as the wording bug above.

    Scoped to the sharing flow on purpose, NOT generalized to "every tool a
    body names". `save-artifact` and `spec` name `save_artifact` precisely to
    forbid calling it (they ship bytes through a helper script instead), so a
    blanket mention⇒allowlist rule would assert something false.
    """
    needed = {
        BULK_SHARE,          # the share itself
        "list_workspaces",   # the only source of a workspace_id
        "list_teammates",    # fallback: resolve each teammate
        "share_agent_brain", # fallback: grant them one at a time
    }
    sharers = [p for p in SKILLS
               if BULK_SHARE in p.read_text(encoding="utf-8").split("\n---\n", 2)[-1]]
    check("some skill actually performs the workspace share", bool(sharers))
    for path in sharers:
        listed = {tool for _, tool in _allowed_tools(path)}
        for tool in sorted(needed):
            check(f"{path.parent.name} can call {tool}", tool in listed)


def test_only_a_flow_with_a_user_present_performs_the_share() -> None:
    """The shared reference and the skills that inline it must not disagree.

    `repo-brain.md` is what `spec` and `pr-babysit` point at for "the
    create-time rules", so if it told every creator to share, those skills
    would read a rule contradicting their own instruction not to grant
    mid-task. Granting a whole org access needs someone present to approve it.
    """
    reference = _normalized(REPO_BRAIN)
    check("repo-brain says who performs the share",
          "Who actually performs the share" in reference)
    check("repo-brain exempts the mid-task callers",
          "must NOT share on its own" in reference)

    for name in ("spec", "pr-babysit"):
        path = next(p for p in SKILLS if p.parent.name == name)
        body = path.read_text(encoding="utf-8").split("\n---\n", 2)[-1]
        check(f"{name} does not perform the workspace share",
              BULK_SHARE not in body)
        check(f"{name} points at onboard for it", "/memhub:onboard" in body)


def test_every_allowlisted_tool_is_paired_across_both_plugins() -> None:
    # memhub and memhub-staging share one symlinked skills/ tree but register
    # different server prefixes; a tool listed for one is missing on the other.
    for path in SKILLS:
        pairs = _allowed_tools(path)
        if not pairs:
            continue
        prod = {tool for server, tool in pairs if server == "memhub"}
        staging = {tool for server, tool in pairs if server == "memhub-staging"}
        check(f"{path.parent.name} pairs every tool across both plugins",
              prod == staging)


if __name__ == "__main__":
    print("brain sharing docs")
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("\nall brain-sharing doc checks passed")
