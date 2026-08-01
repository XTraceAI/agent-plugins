#!/usr/bin/env python3
"""Guard the shared core's boundary — the thing `git subtree` actually needs.

`plugins/memhub/core/` is vendored verbatim into the Codex repo
(https://github.com/XTraceAI/memhub-codex-plugin) as `memhub_core/`. Two
properties have to hold or that sync breaks, and both break SILENTLY:

1. **The core imports nothing from the Claude-only shell.** A single
   `from directive_recall import ...` would make the vendored copy fail at
   import in a repo that has no such file — and nothing in the plugin repo
   would notice, because there the file is right next door.
2. **`plugins/memhub/scripts/` reaches the core through symlinks, not
   copies.** Every hook command string, SKILL.md path, and sibling import
   still says `scripts/<name>.py`; those keep working only while they are
   symlinks. A real file there would shadow the core and drift from it —
   the same failure mode `plugins/memhub-staging/` guards against.

Also checks the contract the core imposes on any host repo: `_memhub_auth`
reads `<plugin_root>/.mcp.json` for the OAuth client, and `_plugin_root()`
falls back to `__file__.parent.parent` — so a vendoring repo MUST place a
plugin-shaped `.mcp.json` one directory above the core.

Run: python3 core_boundary_test.py  (stdlib only).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
PLUGIN_ROOT = CORE.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
REPO = PLUGIN_ROOT.parent.parent

#: The syncable unit. Adding a file here is a deliberate act: it widens what
#: every consumer of the subtree must carry, so it should show up in review.
CORE_FILES = {
    "_memhub_auth.py",
    "import_session.py",
    "room_map.py",
    "room_map_test.py",
    "core_boundary_test.py",
}

#: Non-Python files that legitimately ship with the core. Kept separate from
#: CORE_FILES because only the .py members are import-scanned — but BOTH are
#: enumerated, so a stray .sh/.json/.env dropped in here is caught rather than
#: silently published to every consumer repo.
CORE_NON_PY = {"README.md"}

#: Core modules may import each other, the stdlib, and the `mcp` SDK (the one
#: third-party dep, pulled ephemerally by `uv run --with mcp`). Anything else
#: is either a Claude-only shell module or a new dependency the Codex repo
#: would silently lack.
CORE_MODULES = {f.removesuffix(".py") for f in CORE_FILES}
ALLOWED_THIRD_PARTY = {"mcp"}

#: Sanctioned Claude coupling — pinned per-file so NEW coupling has to be
#: argued for in review rather than appearing by accident. Each entry is here
#: because the vendored copy still works without a Claude Code install:
#:
#: - `_memhub_auth.CLAUDE_PLUGIN_ROOT` — preferred when Claude Code sets it;
#:   `_plugin_root()` falls back to `__file__.parent.parent` otherwise, which
#:   is why a vendoring repo puts its .mcp.json at the core's parent.
#: - `import_session`'s `~/.claude` / `.claude/` — host session DISCOVERY, used
#:   only when `--session` is a bare id (the docstring form and the actual
#:   `Path.home().glob(".claude/projects/...")`). Codex passes an explicit
#:   transcript path, so `resolve_session_file()` returns at its `p.is_file()`
#:   branch and the glob is unreachable there. Cosmetic in a vendored copy,
#:   not functional.
ALLOWED_CLAUDE_REFS = {
    "_memhub_auth.py": {"CLAUDE_PLUGIN_ROOT"},
    "import_session.py": {"~/.claude", ".claude/"},
}

#: The core's own tests name these tokens in order to guard them; scanning
#: them for the tokens would be self-defeating.
TOKEN_SCAN_EXEMPT = {"core_boundary_test.py", "room_map_test.py"}

#: Any CLAUDE_* env var plus the Claude state dir. A regex rather than a fixed
#: list so a NEW variable (CLAUDE_CODE_*, CLAUDE_SESSION_*, ...) is caught the
#: day it appears instead of the day someone remembers to extend the list.
#:
#: This is a substring scan over the whole file, so it flags prose mentions
#: too — deliberately, since a docstring promising Claude behavior in
#: host-agnostic code is its own defect. It does NOT defeat a determined
#: evasion (`"CLAUDE_PLUGIN" + "_ROOT"`); it is a guard against the accidental
#: coupling that actually happens in review, not an adversary.
CLAUDE_TOKEN_RE = re.compile(r"CLAUDE_[A-Z_]+|~/\.claude|\.claude/")

#: This file travels WITH the core into every consumer repo, so it has to be
#: meaningful in both places. The boundary + host-contract checks are
#: universal; the symlink/staging/cutover checks describe the plugin repo's
#: own layout and are skipped elsewhere. Detected by the plugin manifest
#: rather than by a path name, so a renamed checkout still works.
IN_PLUGIN_REPO = (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").is_file()

failures: list[str] = []
checks = 0
skipped = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok  {label}")
    else:
        failures.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL {label}{': ' + detail if detail else ''}")


def skip(label: str, why: str) -> None:
    global skipped
    skipped += 1
    print(f"  --  {label}  (skipped: {why})")


def _imports(path: Path) -> set[str]:
    """Top-level module names imported by `path`, including deferred imports
    inside functions (room_map imports _memhub_auth lazily, and that edge is
    exactly the kind the subtree has to carry)."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.Call):
            # importlib.import_module("x") / __import__("x") bypass the plain
            # Import nodes above. Only literal names are resolvable here; a
            # computed one is flagged so a human looks at it.
            fn = node.func
            fname = (getattr(fn, "attr", None) or getattr(fn, "id", None) or "")
            if fname in {"import_module", "__import__"} and node.args:
                a = node.args[0]
                names.add(a.value.split(".")[0]
                          if isinstance(a, ast.Constant) and isinstance(a.value, str)
                          else "<dynamic-import>")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — impossible in a flat core
                names.add(f"<relative:{node.module}>")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def test_core_file_set() -> None:
    """The core dir holds exactly the syncable unit — no strays.

    Enumerates EVERY file, not just *.py: whatever lands here is vendored into
    a public consumer repo verbatim, so a stray .json/.sh/.env matters at
    least as much as a stray module.
    """
    expected = CORE_FILES | CORE_NON_PY
    actual = {p.name for p in CORE.iterdir()
              if p.is_file() and p.name != "__pycache__"}
    check("core contains exactly the declared file set",
          actual == expected,
          f"unexpected={sorted(actual - expected)} "
          f"missing={sorted(expected - actual)}")
    # A symlink here would vendor as a dangling link (or, worse, silently
    # capture whatever it points at). The core must be real files only.
    links = sorted(p.name for p in CORE.iterdir() if p.is_symlink())
    check("core holds no symlinks (subtree vendors real files)",
          not links, str(links))


def test_no_shell_imports() -> None:
    """No core module imports the Claude-only shell."""
    stdlib = set(sys.stdlib_module_names)
    for name in sorted(CORE_FILES):
        mods = _imports(CORE / name)
        foreign = {
            m for m in mods
            if m not in stdlib
            and m not in CORE_MODULES
            and m not in ALLOWED_THIRD_PARTY
            and m != "__future__"
        }
        check(f"{name}: imports only stdlib/mcp/core", not foreign,
              f"foreign imports {sorted(foreign)}")


def test_no_relative_imports() -> None:
    """Flat co-location: the core resolves siblings via sys.path, not
    packages. A relative import would require __init__.py and change every
    import site — explicitly out of scope for the subtree."""
    for name in sorted(CORE_FILES):
        rel = {m for m in _imports(CORE / name) if m.startswith("<relative:")}
        check(f"{name}: no package-relative imports", not rel, str(sorted(rel)))


def test_claude_coupling_is_pinned() -> None:
    """Claude-specific identifiers appear only where sanctioned."""
    for name in sorted(CORE_FILES - TOKEN_SCAN_EXEMPT):
        text = (CORE / name).read_text()
        found = set(CLAUDE_TOKEN_RE.findall(text))
        allowed = ALLOWED_CLAUDE_REFS.get(name, set())
        check(f"{name}: Claude coupling within the allowed set",
              found <= allowed,
              f"unsanctioned {sorted(found - allowed)} "
              "(add to ALLOWED_CLAUDE_REFS only if the vendored copy still works)")


def test_scripts_symlinks() -> None:
    """The runtime core is reachable at its historical scripts/ path, as a
    symlink — hook command strings and SKILL.md paths depend on it."""
    if not IN_PLUGIN_REPO:
        skip("scripts/ symlinks", "not the plugin repo (vendored copy)")
        return
    for name in sorted(CORE_FILES - {"core_boundary_test.py"}):
        link = SCRIPTS / name
        check(f"scripts/{name} is a symlink", link.is_symlink(),
              "missing or a real file (a real file SHADOWS the core)")
        if link.is_symlink():
            check(f"scripts/{name} -> ../core/{name}",
                  Path(link.readlink()).as_posix() == f"../core/{name}",
                  f"points at {link.readlink()}")
            check(f"scripts/{name} resolves into core/",
                  link.resolve() == CORE / name)


def test_staging_shares_the_core() -> None:
    """Prod/staging parity: staging must reach the same core through its
    symlinked scripts/ dir, never through a file of its own."""
    staging = REPO / "plugins" / "memhub-staging"
    if not IN_PLUGIN_REPO:
        skip("prod/staging parity", "not the plugin repo (vendored copy)")
        return
    if not staging.exists():
        check("memhub-staging present", False, "plugin dir missing")
        return
    check("memhub-staging/scripts is a symlink",
          (staging / "scripts").is_symlink())
    for name in sorted(CORE_FILES - {"core_boundary_test.py"}):
        via_staging = (staging / "scripts" / name)
        check(f"staging reaches core/{name} (no shadow copy)",
              via_staging.resolve() == CORE / name,
              f"resolves to {via_staging.resolve() if via_staging.exists() else 'MISSING'}")
    # The two builds must differ ONLY in .mcp.json + plugin.json.
    real_files = [p for p in staging.rglob("*")
                  if p.is_file() and not p.is_symlink()
                  and staging in p.parents]
    unexpected = [p.relative_to(staging).as_posix() for p in real_files
                  if p.relative_to(staging).as_posix()
                  not in {".mcp.json", ".claude-plugin/plugin.json"}]
    check("memhub-staging holds no real file beyond .mcp.json + plugin.json",
          not unexpected, str(unexpected))


def _mcp_entry(cfg: Path) -> dict:
    servers = json.loads(cfg.read_text()).get("mcpServers", {})
    name = next((k for k in servers if k.lower().startswith("memhub")), None)
    return servers.get(name, {}) if name else {}


def test_prod_and_staging_point_at_different_backends() -> None:
    """The ONE thing the two builds are allowed to differ in — and therefore
    the one thing worth machine-checking.

    Everything else is symlinked, so drift is structurally impossible. The
    backend config is hand-maintained in two files, and the Auth0 tenant names
    read crossed (prod is `memhub-prod.us.auth0.com`, staging is plain
    `memhub.us.auth0.com`), which is exactly the shape of thing a human
    transposes. A staging build silently talking to production is the worst
    outcome this repo can ship: it writes test junk into a real team's brain.
    """
    if not IN_PLUGIN_REPO:
        skip("prod/staging backend split", "not the plugin repo (vendored copy)")
        return
    prod_cfg = REPO / "plugins" / "memhub" / ".mcp.json"
    stg_cfg = REPO / "plugins" / "memhub-staging" / ".mcp.json"

    # Each build must own its config outright. A symlink to the other's would
    # slip past a file-set check while collapsing the two backends into one.
    check("staging owns a real .mcp.json (not a symlink to prod's)",
          stg_cfg.is_file() and not stg_cfg.is_symlink())
    check("prod owns a real .mcp.json (not a symlink)",
          prod_cfg.is_file() and not prod_cfg.is_symlink())
    if not (prod_cfg.is_file() and stg_cfg.is_file()):
        return

    prod, stg = _mcp_entry(prod_cfg), _mcp_entry(stg_cfg)
    p_url, s_url = prod.get("url", ""), stg.get("url", "")
    p_oauth, s_oauth = prod.get("oauth", {}), stg.get("oauth", {})

    check("prod points at the production backend",
          "api.memhub.xtrace.ai" in p_url and "staging" not in p_url, p_url)
    check("staging points at the staging backend",
          "api.staging.memhub.xtrace.ai" in s_url, s_url)
    check("the two urls differ", p_url != s_url, f"both {p_url}")
    check("prod uses the prod Auth0 tenant",
          "memhub-prod.us.auth0.com" in p_oauth.get("authServerMetadataUrl", ""),
          p_oauth.get("authServerMetadataUrl", ""))
    check("staging uses the staging Auth0 tenant",
          "//memhub.us.auth0.com" in s_oauth.get("authServerMetadataUrl", ""),
          s_oauth.get("authServerMetadataUrl", ""))
    check("the two OAuth clients differ",
          bool(p_oauth.get("clientId")) and bool(s_oauth.get("clientId"))
          and p_oauth["clientId"] != s_oauth["clientId"],
          "same clientId — one build authenticates against the wrong tenant")


def test_plugin_versions_are_in_lockstep() -> None:
    """The two manifests bump together; a version skew is how the marketplace
    ends up offering two builds that are not, in fact, the same plugin."""
    if not IN_PLUGIN_REPO:
        skip("plugin version lockstep", "not the plugin repo (vendored copy)")
        return
    def _ver(name: str) -> str:
        p = REPO / "plugins" / name / ".claude-plugin" / "plugin.json"
        return json.loads(p.read_text()).get("version", "") if p.is_file() else ""
    prod_v, stg_v = _ver("memhub"), _ver("memhub-staging")
    check("memhub and memhub-staging versions match",
          bool(prod_v) and prod_v == stg_v, f"memhub={prod_v} staging={stg_v}")


def test_host_repo_contract() -> None:
    """`_memhub_auth` needs a plugin-shaped .mcp.json at <core>/.. — the
    contract a vendoring repo (memhub-codex-plugin) must also satisfy."""
    cfg = PLUGIN_ROOT / ".mcp.json"
    check(".mcp.json exists one dir above the core", cfg.is_file(),
          f"expected {cfg}")
    if not cfg.is_file():
        return
    servers = json.loads(cfg.read_text()).get("mcpServers", {})
    name = next((k for k in servers if k.lower().startswith("memhub")), None)
    check(".mcp.json declares a memhub server", name is not None)
    if name:
        entry = servers[name]
        check(".mcp.json carries url + oauth.clientId",
              bool(entry.get("url")) and bool(entry.get("oauth", {}).get("clientId")),
              "build_oauth() reads clientId from here and does NOT catch failure")


def test_codex_glue_is_gone() -> None:
    """The in-repo codex/ copy was removed at cutover — two live copies of the
    core is the exact trap the split exists to avoid. Checked by SOURCE files,
    not directory existence: a stray __pycache__/ is gitignored noise, not a
    second consumer."""
    if not IN_PLUGIN_REPO:
        skip("no in-repo codex/ glue", "not the plugin repo (vendored copy)")
        return
    codex = REPO / "codex"
    sources = sorted(p.relative_to(REPO).as_posix()
                     for p in codex.rglob("*")
                     if p.is_file() and p.suffix in {".py", ".md"}) \
        if codex.is_dir() else []
    check("no in-repo codex/ glue", not sources,
          f"{sources} — the core now has two live consumers in one tree")


if __name__ == "__main__":
    print("core boundary checks\n")
    for fn in (test_core_file_set, test_no_shell_imports,
               test_no_relative_imports, test_claude_coupling_is_pinned,
               test_scripts_symlinks, test_staging_shares_the_core,
               test_prod_and_staging_point_at_different_backends,
               test_plugin_versions_are_in_lockstep,
               test_host_repo_contract, test_codex_glue_is_gone):
        print(f"{fn.__name__}:")
        fn()
    where = "plugin repo" if IN_PLUGIN_REPO else "vendored copy"
    print(f"\n{where}: {checks} checks, {skipped} skipped, "
          f"{len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1 if failures else 0)
