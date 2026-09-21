#!/usr/bin/env python3
"""Seed the starter rulebook for a repo nobody has written rules for yet.

Mining sessions needs a history and `create-rule` needs a rule
in somebody's head. A new team has neither. What it does have is a repo, and
most of what a universal rule needs to know is sitting in it: the default
branch, the test command, the manifests, where migrations live, which files
are too big to read whole.

Three steps, each a subcommand, each writing a file the next one reads:

  scan    read the checkout (read-only; `git ls-files`, no network) and write
          signals.json: what was found, where, and the slot values it fills.
  seed    fill catalog.json's `{{slots}}` from those signals and write
          candidates.json (create_rule bodies + their cases) and dropped.json
          (every rule left out, with the signal it was missing). A rule whose
          signal is absent is DROPPED, never filed with a guessed value.
  verify  run every seeded candidate through `rulebook_verify.verify` — the
          live hook's own engine — against the cases the catalog ships, and
          write verified.json. Exit 1 if any candidate misbehaves.

  starter_rulebook.py all --repo . --out starter-out

Stdlib only; never writes inside the repo it scans.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE.parent / "catalog.json"
PLUGIN_SCRIPTS = HERE.parents[2] / "scripts"

# Command position, not `^`: real commands arrive as `cd x && git push`.
# A start-anchored pattern matches none of them. `(` is deliberately NOT a
# command position: it is far more often a conventional-commit scope inside a
# quoted message (`-m "fix(alembic): …"`) than a subshell. A pipe counts only
# with a space after it: `grep -E 'run:|pytest'` is an alternation, not a pipe.
CMD = r"(?:^|[;&]\s*|\|\s+)"
# One `-C <dir>` / `-c k=v` is the evasion Anthropic's permission docs name.
# Written without a quantified group: the hook's load lint drops those.
# `/usr/bin/git` is the same program: a path in front of it is not a way round every git rule.
GIT = CMD + r"(?:sudo\s+)?(?:\S*/)?git\s+(?:-[cC]\s*\S+\s+)?"
RX_MAX = 400
# Appended to a command pattern to make it a RECEIPT: the command, but not an invocation that exits 0
# having done none of the work — `pytest --version`, `eslint --help`, `pytest --collect-only`.
_RAN_NOTHING = (r"\b(?![^|;&]*\s(?:--version|-V|--help|-h|--collect-only|--co|--fixtures|--markers|--setup-plan"
                r"|--listenvs|--list|-list|--showconfig|--show-settings|--print-config|--no-run)(?:\s|=|$))")

_SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".venv/", "venv/",
              "__pycache__/", ".git/", "target/", ".next/", "site-packages/")
_TEXT_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".kt",
             ".c", ".h", ".cc", ".cpp", ".cs", ".php", ".swift", ".scala", ".sh",
             ".md", ".sql", ".yml", ".yaml", ".toml", ".html", ".css", ".vue", ".svelte"}
_GENERIC_NAMES = {"index", "main", "__init__", "app", "utils", "types", "models", "readme",
                  "conftest", "setup", "config", "settings", "changelog"}
_SRC_EXT = {"python": ["py"], "node": ["js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte"],
            "go": ["go"], "rust": ["rs"]}


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _tracked(repo: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                             text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return [l for l in out.stdout.splitlines() if l]
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d + "/" not in _SKIP_DIRS and not d.startswith(".git")]
        files += [os.path.relpath(os.path.join(root, n), repo) for n in names]
    return files


def _alt(parts) -> str:
    parts = [p for p in dict.fromkeys(parts) if p]
    return "(?:%s)" % "|".join(parts) if parts else ""


def _fit(prefix: str, extras: list[str], suffix: str = "") -> str:
    """Append alternatives while the whole pattern stays loadable. The hook
    drops a rule whose pattern passes 400 characters, silently, so a long
    repo-derived list is cut here rather than shipped dead."""
    rx = prefix
    for e in extras:
        if len(rx) + len(e) + len(suffix) + 1 > RX_MAX - 40:
            break
        rx += "|" + e
    return rx + suffix


# ───────────────────────────── scan ─────────────────────────────

def scan(repo: Path) -> dict:
    files = _tracked(repo)
    fset = set(files)
    live = [f for f in files if not any(("/" + f).find("/" + d) >= 0 for d in _SKIP_DIRS)]
    names = Counter(os.path.basename(f) for f in live)
    tops = {f.split("/", 1)[0] for f in live if "/" in f}
    sig: dict[str, dict] = {}
    slots: dict[str, object] = {"CMD": CMD, "GIT": GIT}

    def found(key: str, evidence: str, **more) -> None:
        sig[key] = {"found": True, "evidence": evidence, **more}

    def missing(key: str, why: str) -> None:
        sig[key] = {"found": False, "evidence": why}

    # repo name + default branch
    remote = _git(repo, "remote", "get-url", "origin")
    slots["repo"] = re.sub(r"\.git$", "", remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) \
        if remote else repo.resolve().name
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        branch = head.split("/", 1)[-1]
        found("branch", "origin/HEAD → %s" % branch, value=branch)
        slots["default_branch_rx"] = "^(%s)$" % re.escape(branch)
        slots["default_branch"] = branch
        slots["default_branch_esc"] = re.escape(branch)
    else:
        # No guess. `main|master` on a repo whose default is `trunk` yields a verified gate
        # that guards nothing — worse than no rule, because it reads as protection. The two
        # default-branch rules are dropped, and the report says how to get them back.
        missing("branch", "origin/HEAD is not set, so the default branch is unknown — run "
                          "`git remote set-head origin --auto` and scan again to get the push rules")

    # toolchains
    chains = []
    if names["pyproject.toml"] or names["setup.py"] or any(n.startswith("requirements") and n.endswith(".txt") for n in names):
        chains.append("python")
    if names["package.json"]:
        chains.append("node")
    if names["go.mod"]:
        chains.append("go")
    if names["Cargo.toml"]:
        chains.append("rust")
    # Every tracked package.json, not only the root one: in a monorepo the Node project lives under
    # `web/` or `packages/*`, and a root-only read finds `{}` — the repo is classed as Node and then
    # loses every test rule for want of evidence that is sitting one directory down.
    pkg_files = [f for f in sorted(live, key=lambda f: f.count("/")) if os.path.basename(f) == "package.json"][:25]
    pkgs = [j for j in (_json(repo / f) for f in pkg_files) if isinstance(j, dict)]
    pkg = {"scripts": {k: v for j in reversed(pkgs) for k, v in (j.get("scripts") or {}).items()},
           "dependencies": {k: v for j in pkgs for k, v in (j.get("dependencies") or {}).items()},
           "devDependencies": {k: v for j in pkgs for k, v in (j.get("devDependencies") or {}).items()}}
    scripts = pkg["scripts"]
    makefile = _read(repo / "Makefile")
    make_targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile, re.M))
    pm = "pnpm" if names["pnpm-lock.yaml"] else "yarn" if names["yarn.lock"] else \
        "bun" if (names["bun.lockb"] or names["bun.lock"]) else "npm"     # the lockfile beside whichever package.json

    # One record per test runner the repo gives EVIDENCE of. Every slot below is derived from these
    # records and nothing else, so a pattern can never be seeded without the example that proves it, an
    # example can never come from a toolchain that contributed no pattern, and a repo with no evidenced
    # runner gets no test rules at all. An ordering gate is cleared only by a green run MATCHING its
    # pattern: a runner guessed from a manifest (`pytest` for any pyproject.toml, `npm test` for any
    # package.json) is a gate nobody can ever satisfy.
    runners = []   # {"name", "rx", "example", "targeted", "slow_flag", "slow_example"}

    def runner(name, rx, example, targeted="", slow_flag="", slow_example=""):
        runners.append({"name": name, "rx": rx, "example": example, "targeted": targeted,
                        "slow_flag": slow_flag, "slow_example": slow_example})

    cfg = _read(repo / "pyproject.toml") + "\n" + _read(repo / "pytest.ini") + "\n" + _read(repo / "setup.cfg")
    block = re.search(r"markers\s*=\s*\[?(.*?)(?:\n\s*\]|\n\S|\Z)", cfg, re.S)
    marks = re.findall(r"^\s*[\"']?([A-Za-z_]\w*)\s*[:\"']", block.group(1), re.M) if block else []
    slow = [m for m in dict.fromkeys(marks)
            if re.search(r"slow|behavio|e2e|integration|perf|load|live|network|smoke", m)]

    if "python" in chains:
        py_cfg = "".join(_read(repo / f) for f in [f for f in live if os.path.basename(f) in (
            "pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py", "pytest.ini")
            or re.fullmatch(r"requirements[^/]*\.txt", os.path.basename(f))][:25])     # nested projects count too
        py_targeted = r"\s\S*(?:tests?/|\.py\b|::)|\s-k\s|\s--lf\b|\s--last-failed\b|\s-e\s+\S"
        before = len(runners)
        if names["pytest.ini"] or names["conftest.py"] or re.search(r"\bpytest\b", py_cfg):
            flags = [r"--cov\b"] + ([r"-m\s+[\"']?(?:%s)" % "|".join(map(re.escape, slow))] if slow else [])
            runner("pytest", r"(?:uv run |poetry run |python3? -m )?pytest", "pytest", py_targeted,
                   "|".join(flags), "pytest --cov=app")          # --cov and -m are pytest's flags, nobody else's
        if names["tox.ini"] or "[tool.tox" in py_cfg:
            runner("tox", r"(?:uv run |python3? -m )?tox\b", "tox", py_targeted)
        if names["noxfile.py"]:
            runner("nox", r"(?:uv run |python3? -m )?nox\b", "nox", py_targeted)
        if names["manage.py"] and len(runners) == before:
            runner("django", r"python3? manage\.py test", "python manage.py test", py_targeted)
        if len(runners) == before:
            test_files = [f for f in live if re.search(r"(^|/)test_[^/]+\.py$", f)][:20]
            if any("unittest" in _read(repo / f, 4000) for f in test_files):
                runner("unittest", r"python3? -m unittest", "python -m unittest", py_targeted)
    if "node" in chains:
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else {}
        node_targeted = r"\s\S*\.(?:test|spec)\.|\s\S*(?:tests?|__tests__)/|\s-t\s|--testPathPattern"
        script = str(scripts.get("test") or "")
        if script and "no test specified" not in script:          # npm init's placeholder is not a test script
            runner("%s test" % pm, r"(?:npm|pnpm|yarn|bun)(?: run)? test", "%s test" % pm, node_targeted,
                   r"--coverage\b", "%s test -- --coverage" % pm)
        direct = [d for d in ("jest", "vitest", "mocha", "ava") if d in deps]
        if direct:
            runner(direct[0], r"(?:npx |pnpm exec |yarn )?(?:%s)" % "|".join(direct), "npx %s" % direct[0], node_targeted,
                   r"--coverage\b", "npx %s --coverage" % direct[0])
    if "go" in chains:                                            # the toolchain IS the runner
        runner("go test", r"go test", "go test ./...", r"\s-run\s|go test\s+\./(?!\.\.\.)\S",
               r"\s-(?:race|cover(?:profile)?)\b", "go test -race ./...")
    if "rust" in chains:
        runner("cargo test", r"cargo test", "cargo test", r"cargo test\s+[a-zA-Z_]")
    if "test" in make_targets:                                    # the repo's own entrypoint: evidence on its own
        runner("make test", r"make test", "make test")

    # content an edit rule looks for in a unit test, paired with a case that proves it matches
    slow_content = []
    if "python" in chains:
        slow_content.append((r"time\.sleep\(|\b(?:requests|httpx)\.(?:get|post|put|delete|Client)\(",
                             "/repo/tests/test_a.py::time.sleep(5)"))
    if "go" in chains:
        slow_content.append((r"time\.Sleep\(|http\.(?:Get|Post)\(", "/repo/pkg/a_test.go::time.Sleep(5)"))

    if chains:
        found("toolchain", ", ".join(chains) + ("" if runners else " — but no test runner it can find evidence of, so the test rules are left out"),
              value=chains, test_runner=runners[0]["example"] if runners else None,
              test_runners=[r["name"] for r in runners])
        slots["src_ext_rx"] = r"\.(?:%s)$" % "|".join(e for c in chains for e in _SRC_EXT[c])
    else:
        missing("toolchain", "no pyproject.toml / package.json / go.mod / Cargo.toml")
    if runners:                                     # no evidenced runner → no slot → every rule that needs one is dropped
        slots["test_cmd_rx"] = _alt(r["rx"] for r in runners)
        slots["targeted_rx"] = "|".join(r["targeted"] for r in runners if r["targeted"]) or r"\s--this-repo-has-no-targeted-form\b"
        slots["test_cmd_example"] = runners[0]["example"]
        # What CLEARS a "tests ran" obligation. A targeted run does, on purpose; an invocation that runs no
        # test does not — `pytest --version`, `--help`, `--collect-only` all exit 0 having tested nothing.
        slots["test_receipt_rx"] = slots["test_cmd_rx"] + _RAN_NOTHING
    if slow_content:
        slots["test_slow_content_rx"] = "|".join(c for c, _ in slow_content)
        slots["test_slow_example"] = slow_content[0][1]

    # markers / slow tier — only from a runner that has one, with that same runner's example
    tiered = [r for r in runners if r["slow_flag"]]
    if tiered:
        found("markers", ("markers: " + ", ".join(slow)) if slow else "no slow markers defined; coverage flags only",
              value=slow)
        slots["slow_flag_rx"] = "|".join(dict.fromkeys(r["slow_flag"] for r in tiered))
        slots["slow_example"] = tiered[0]["slow_example"]
        slots["slow_words_rx"] = r"(?i:\b(?:%s)\b)" % "|".join(
            dict.fromkeys(slow + ["slow", "e2e", "coverage", "integration"]))
    else:
        missing("markers", "no test runner with a known slow or coverage tier")

    # lint tooling (repo entrypoint first, raw tool names second)
    dev_text = (cfg + _read(repo / ".pre-commit-config.yaml") + json.dumps(pkg)
                + "".join(_read(repo / n) for n in fset if re.fullmatch(r"requirements[^/]*\.txt", n)))
    lint, lint_ex = [], []
    if "lint" in make_targets:
        lint.append("make lint"); lint_ex.append("make lint")
    if "lint" in scripts:
        lint.append(r"(?:npm|pnpm|yarn|bun) run lint"); lint_ex.append("%s run lint" % pm)
    for tool, rx, ex in (("ruff", r"(?:uv run )?ruff (?:check|format)", "ruff check ."), ("black", "black", "black --check ."),
                         ("flake8", "flake8", "flake8"), ("mypy", "mypy", "mypy ."), ("pylint", "pylint", "pylint src"),
                         ("eslint", r"(?:npx )?eslint", "npx eslint ."), ("prettier", r"(?:npx )?prettier", "npx prettier --check ."),
                         ("biome", r"(?:npx )?biome", "npx biome check"), ("typescript", r"(?:npx )?tsc", "npx tsc --noEmit"),
                         ("golangci-lint", "golangci-lint", "golangci-lint run")):
        if re.search(r"\b%s\b" % re.escape(tool), dev_text):
            lint.append(rx); lint_ex.append(ex)
    if "go" in chains:
        lint.append(r"go vet|gofmt"); lint_ex.append("go vet ./...")
    if "rust" in chains:
        lint.append(r"cargo (?:clippy|fmt)"); lint_ex.append("cargo clippy")
    if lint:
        found("lint", ", ".join(lint_ex), value=lint_ex)
        slots["lint_cmd_rx"] = _alt(lint)
        slots["lint_receipt_rx"] = slots["lint_cmd_rx"] + _RAN_NOTHING     # `eslint --version` checked no code
        slots["lint_cmd_example"] = lint_ex[0]
    else:
        missing("lint", "no linter or formatter in dev dependencies, pre-commit, Makefile or package scripts")

    # manifests + lock tool
    manifest_names = ["pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile"]
    lock_names = ["uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                  "go.sum", "Cargo.lock", "Gemfile.lock", "bun.lockb", "bun.lock"]
    mans = [n for n in manifest_names if names[n]]
    reqs = sorted(n for n in names if re.fullmatch(r"requirements[^/]*\.txt", n))
    locks = [n for n in lock_names if names[n]]
    if mans or reqs:
        found("manifests", ", ".join(mans + reqs + locks), value=mans + reqs + locks)
        every = [re.escape(n) for n in mans + locks] + ([r"requirements[^/]*\.txt"] if reqs else [])
        slots["manifest_rx"] = r"(?:^|/)%s$" % _alt(every)
        slots["manifest_only_rx"] = r"(?:^|/)%s$" % _alt(
            [re.escape(n) for n in mans] + ([r"requirements[^/]*\.txt"] if reqs else []))
        slots["manifest_paths"] = mans + reqs       # the hook also tries `*/<glob>`, so bare names reach subpackages
        installs = {"python": r"pip3? install|uv (?:sync|pip install)|poetry install",
                    "node": r"(?:npm|pnpm|yarn|bun) (?:ci|install)\b", "go": r"go mod download",
                    "rust": r"cargo fetch"}
        slots["install_cmd_rx"] = _alt([installs[c] for c in chains])
        slots["install_example"] = {"python": "pip install -r requirements.txt", "node": "%s install" % pm,
                                    "go": "go mod download", "rust": "cargo fetch"}.get(chains[0] if chains else "", "")
        # One (manifest, lockfile, tool) record each — never one shared receipt. In a polyglot repo a single
        # rule over every lock tool lets `pnpm install` clear the obligation an edit to pyproject.toml armed,
        # and the push goes out with uv.lock stale. The rule is seeded once per pair (`for_each`).
        #
        # Paired by DIRECTORY, not by basename: `a/package-lock.json` belongs to `a/package.json` and nothing
        # else. A basename match gives both of a monorepo's packages the scope `package.json`, so editing one
        # arms the other's gate too, and the right install still leaves the wrong gate blocking the push.
        # Every verb that REWRITES the lock is a receipt, not only the one named `lock`: `uv add requests`
        # updates uv.lock, and a gate that still blocks the push after it teaches people to override.
        tools = (("uv", "uv.lock", "pyproject.toml", r"uv (?:lock|add|remove|sync)\b", "uv lock"),
                 ("poetry", "poetry.lock", "pyproject.toml", r"poetry (?:lock|add|remove|update)\b", "poetry lock"),
                 ("npm", "package-lock.json", "package.json", r"npm (?:install|i)\b", "npm install"),
                 ("pnpm", "pnpm-lock.yaml", "package.json", r"pnpm (?:install|i)\b", "pnpm install"),
                 ("bun", "bun.lock", "package.json", r"bun (?:install|i)\b", "bun install"),
                 ("bun", "bun.lockb", "package.json", r"bun (?:install|i)\b", "bun install"),
                 # bare `yarn` IS the install alias; `yarn test`, `yarn lint`, `yarn --version` are not
                 ("yarn", "yarn.lock", "package.json",
                  r"yarn(?:\s+install)?(?=\s*(?:$|[;&|])|\s+--(?!version|help))", "yarn install"),
                 ("go", "go.sum", "go.mod", r"go mod tidy\b", "go mod tidy"),
                 ("cargo", "Cargo.lock", "Cargo.toml", r"cargo (?:update|generate-lockfile|build|check)\b", "cargo update"))
        live_set = set(live)
        pairs = []
        for key, lock, man, rx, ex in tools:
            for lock_path in sorted((f for f in live if os.path.basename(f) == lock), key=lambda f: (f.count("/"), f)):
                where = os.path.dirname(lock_path)
                man_path = (where + "/" if where else "") + man
                if man_path not in live_set:
                    continue                        # a lockfile with no manifest beside it guards nothing we can name
                pairs.append({"lock_key": key + ("-" + re.sub(r"[^A-Za-z0-9]+", "-", where).strip("-") if where else ""),
                              "lock_name": lock_path, "lock_manifest": man_path, "lock_cmd_rx": rx, "lock_cmd_example": ex})
        for pr in pairs:
            # The hook also tries `*/<glob>`, so a ROOT manifest's scope would swallow every nested manifest of
            # the same name. Hand each nested pair's manifest to the root rule as an exclusion — computed over
            # EVERY pair, before the cap below: a nested package that gets no rule of its own must still not
            # arm the root's, or the right install for it can never clear the obligation.
            pr["lock_exclude"] = [o["lock_manifest"] for o in pairs
                                  if o is not pr and o["lock_manifest"].endswith("/" + pr["lock_manifest"])]
        pairs = pairs[:8]
        for pr in pairs:
            # `uv lock --dry-run` exits 0 and leaves uv.lock untouched, and so do `uv sync --frozen` and
            # `--locked`: a preview or a read-only install is not a receipt
            pr["lock_cmd_rx"] = "(?:%s)(?![^|;&]*\\s--(?:dry-run|frozen|locked)\\b)" % pr["lock_cmd_rx"]
        if pairs:
            slots["lock_pairs"] = pairs
    else:
        missing("manifests", "no dependency manifest found")

    # test layout
    test_dirs = sorted(t for t in tops if t in ("tests", "test", "spec", "__tests__"))
    has_tests = bool(test_dirs) or any(re.search(r"(_test\.go|\.(test|spec)\.[jt]sx?|(^|/)test_[^/]+\.py)$", f) for f in live)
    if has_tests:
        slots["test_path_rx"] = r"(?:^|/)(?:tests?|spec|__tests__)/|_test\.go$|\.(?:test|spec)\.[jt]sx?$"
        slots["integration_path_rx"] = r"(?:^|/)(?:e2e|integration|behavioral|functional|acceptance)/"
        slots["tests_diff_rx"] = r"(?:^|/)(?:tests?|spec|__tests__)/|_test\.go$|\.(?:test|spec)\.[jt]sx?$"
        src_tops = Counter(f.split("/", 1)[0] for f in live if "/" in f
                           and f.split("/", 1)[0] not in set(test_dirs) | {"docs", "scripts", "examples", "alembic", "migrations", "docker", "infra", "deploy", "tools", "evals", "benchmarks"}
                           and re.search(str(slots.get("src_ext_rx", r"\.\w+$")), f)).most_common(3)
        src = [t for t, n in src_tops if n >= 3 and n * 5 >= src_tops[0][1]]
        if src and slots.get("src_ext_rx"):
            slots["src_diff_rx"] = "^%s/" % _alt(map(re.escape, src))
            slots["src_example"] = next((f for f in live if f.startswith(src[0] + "/")
                                         and re.search(str(slots["src_ext_rx"]), f)
                                         and not re.search(str(slots["tests_diff_rx"]), f)), src[0] + "/module.x")
        found("tests", "tests: %s; source roots: %s" % (", ".join(test_dirs) or "co-located", ", ".join(src) or "none found"),
              value={"test_dirs": test_dirs, "src_roots": src})
    else:
        missing("tests", "no test directory or test files found")

    # migrations
    mig = None
    if any(f.endswith("alembic.ini") for f in live) or any("/versions/" in f and "alembic" in f for f in live):
        # Where the revisions actually live, from the tracked files: `<dir>/env.py` beside `<dir>/versions/`.
        # alembic.ini's script_location is often `migrations`, and a scope of `alembic/versions/*` there
        # never arms the pre-push check. Falls back to the default only when nothing is tracked yet.
        env_dirs = [os.path.dirname(f) for f in live if os.path.basename(f) == "env.py"
                    and any(g.startswith(os.path.dirname(f) + "/versions/") for g in live)]
        versions = (sorted(env_dirs, key=lambda d: (d.count("/"), d))[0] + "/versions") if env_dirs else "alembic/versions"
        mig_dirs = dict.fromkeys(["alembic", "migrations", re.escape(os.path.dirname(versions))])
        mig = ("alembic", r"(?:%s)/versions/.*\.py$" % "|".join(mig_dirs),
               r"|alembic (?:downgrade|stamp)", "alembic revision --autogenerate", ["alembic", "alembic.ini"], versions)
    elif any(f.startswith("prisma/migrations/") or "/prisma/migrations/" in f for f in live):
        mig = ("prisma", r"prisma/migrations/.*\.sql$", r"|prisma migrate reset|prisma db push[^|;&]*--force-reset",
               "prisma migrate dev", ["prisma migrate", "schema.prisma"], "prisma/migrations")
    elif any(f.startswith("db/migrate/") for f in live):
        mig = ("rails", r"db/migrate/.*\.rb$", r"|(?:rails|rake) db:(?:drop|reset|rollback)",
               "rails generate migration", ["db:migrate", "schema.rb"], "db/migrate")
    elif names["manage.py"] and any(re.search(r"/migrations/\d+", f) for f in live):
        mig = ("django", r"/migrations/\d[^/]*\.py$", r"|manage\.py (?:flush|sqlflush)|manage\.py migrate \S+ zero",
               "python manage.py makemigrations", ["makemigrations", "manage.py migrate"], "**/migrations")
    if mig:
        found("migrations", "%s (%s)" % (mig[0], mig[5]), value=mig[0])
        slots.update(mig_tool=mig[0], mig_versions_path_rx=mig[1], mig_destructive_alt=mig[2],
                     mig_generate_hint=mig[3], mig_anchors=mig[4],
                     mig_scope_paths=[mig[5] + "/*"],   # fnmatch: a bare directory matches no file in it
                     mig_example_path="/repo/%s/0001_example.%s" % (mig[5].replace("**/", "app/"), "sql" if mig[0] == "prisma" else "rb" if mig[0] == "rails" else "py"))
        slots["mig_example_rel"] = mig[5].replace("**/", "app/") + "/0001_add_users.py"
        if mig[0] == "alembic":
            slots["alembic"] = True
    else:
        missing("migrations", "no alembic / prisma / rails / django migrations directory")
    slots.setdefault("mig_destructive_alt", "")
    slots["db_client_rx"] = _alt(["psql", "mysql", "redis-cli", "mongosh"] + ([re.escape(mig[0])] if mig and mig[0] in ("alembic", "prisma") else []))

    # dev server
    serve_text = "".join(_read(repo / f) for f in live if os.path.basename(f) in ("Dockerfile", "Procfile")
                         or re.search(r"(docker-)?compose[^/]*\.ya?ml$", f))[:200_000]
    dev = []                                        # (what a person would call it, its pattern, a command that runs it)
    for needle, rx, example in (("uvicorn", "uvicorn", "uvicorn app.main:app --reload"), ("gunicorn", "gunicorn", "gunicorn app:app"),
                                ("flask", r"flask run", "flask run"),
                                ("runserver", r"python3? manage\.py runserver", "python manage.py runserver"),
                                ("rails", r"rails s(?:erver)?\b", "rails server")):
        if needle in serve_text or needle in dev_text:
            dev.append((needle, rx, example))
    if "dev" in scripts or "start" in scripts:
        dev.append(("%s run dev" % pm, r"(?:npm|pnpm|yarn|bun)(?: run)? (?:dev|start)\b", "%s run dev" % pm))
    if any(re.search(r"(docker-)?compose[^/]*\.ya?ml$", f) for f in live):
        dev.append(("docker compose up", r"docker[ -]compose up(?![^|;&]*\s-d\b)", "docker compose up"))
    if dev:
        found("devserver", ", ".join(d[0] for d in dev), value=[d[0] for d in dev])
        slots["devserver_rx"] = _alt(d[1] for d in dev)
        slots["devserver_example"] = dev[0][2]
    else:
        missing("devserver", "no Dockerfile CMD, compose file, Procfile or dev script")

    # generated files (header scan is the reliable signal; suffixes are the floor)
    generated = []
    for f in live[:6000]:
        if os.path.splitext(f)[1] in _TEXT_EXT and not f.endswith(".md"):
            if re.search(r"(?i)(code generated|generated by|@generated|do not edit)", _read(repo / f, 400)):
                generated.append(f)
    gen_dirs = [d for d, n in Counter(os.path.dirname(g) for g in generated).most_common(6) if n >= 3 and d]
    base_gen = r"_pb2(?:_grpc)?\.pyi?$|\.pb\.go$|\.generated\.|/generated/|(?:^|/)openapi\.json$|(?:^|/)schema\.graphql$"
    slots["generated_rx"] = _fit(base_gen, ["(?:^|/)%s/" % re.escape(d) for d in gen_dirs])
    (found if generated else missing)("generated", "%d files with a generated-by header%s" % (
        len(generated), (" (dirs: %s)" % ", ".join(gen_dirs)) if gen_dirs else "") if generated
        else "no generated-by headers; known suffixes only")

    # largest files → read threshold + anchors
    sizes = []
    gen_set = set(generated)
    for f in live:
        if os.path.splitext(f)[1] in _TEXT_EXT and f not in gen_set and not re.search(r"\.min\.|\.lock$|-lock\.", f):
            try:
                if (repo / f).stat().st_size < 3_000_000:
                    with open(repo / f, "rb") as fh:
                        sizes.append((sum(1 for _ in fh), f))
            except OSError:
                pass
    sizes.sort(reverse=True)
    threshold = 350
    if len(sizes) >= 50:
        p95 = sorted(n for n, _ in sizes)[int(len(sizes) * 0.95)]
        threshold = max(350, min(500, p95))
    slots["read_threshold"] = threshold
    heavy = []
    for n, f in sizes:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if n >= 1000 and stem not in _GENERIC_NAMES and names[os.path.basename(f)] == 1 \
                and not re.search(r"(^|/)(tests?|spec|__tests__|fixtures|data)/|_test\.|\.test\.|(^|/)test_|\.html$"
                                  r"|(^|/)(CLAUDE|AGENTS|CHANGELOG|README)\.md$", f):
            heavy.append((os.path.basename(f), n))
        if len(heavy) == 4:
            break
    if heavy:
        found("largest", "; ".join("%s (%d lines)" % h for h in heavy), value=heavy, threshold=threshold)
        slots["heavy_anchors"] = [h[0] for h in heavy]
    else:
        missing("largest", "no uniquely-named tracked file over 1,000 lines; read threshold %d" % threshold)

    # .gitignore → never-read + secrets
    ignore = [l.strip() for l in _read(repo / ".gitignore").splitlines() if l.strip() and not l.startswith(("#", "!"))]
    ign_dirs = [l.strip("/") for l in ignore if l.endswith("/") and re.fullmatch(r"[\w.-]+/?", l)]
    base_never = (r"(?:\.lock|-lock\.(?:json|yaml)|\.min\.(?:js|css)|\.map)$"
                  r"|(?:^|/)(?:dist|build|node_modules|vendor|\.venv|__pycache__|target|\.next)/")
    # Only an ignored directory that EXISTS here. A .gitignore is mostly a language template — Python's
    # ships `lib/`, `var/`, `parts/`, `env/` — and turning each line into a read gate blocks ordinary source
    # under `src/lib/` in every repo the rule reaches. (Found by the first replay that counted reads.)
    extra = [d for d in ign_dirs if d not in ("dist", "build", "node_modules", "vendor", ".venv", "__pycache__", "target", ".next")
             and not d.startswith(".env") and (repo / d).is_dir()
             and not any(f.startswith(d + "/") for f in live)][:8]
    slots["never_read_rx"] = _fit(base_never, ["(?:^|/)%s/" % re.escape(d) for d in extra])
    sec_extra = [l.lstrip("/") for l in ignore if re.search(r"secret|credential|\.pem|\.key|token", l, re.I)
                 and re.fullmatch(r"[\w./-]+", l)][:6]
    base_secret = (r"(?:^|/)\.env(?:rc|\.(?!example|sample|template|dist)[\w.-]+)?$|\.(?:pem|key|p12|pfx)$"
                   r"|(?:^|/)(?:credentials|secrets?)(?:\.(?:json|ya?ml|toml))?$|(?:^|/)id_(?:rsa|ed25519)$")
    slots["secrets_rx"] = _fit(base_secret, ["(?:^|/)%s$" % re.escape(s) for s in sec_extra])
    # The SAME set, shaped for a command line instead of a path: a token that starts after whitespace or a
    # `/` and ends at whitespace or a separator. One list, two shapes — a rule about staging secrets that
    # knows fewer secrets than the rule about reading them protects less than its title says.
    slots["secrets_cmd_rx"] = _fit(
        r"\.env(?:rc|\.(?!example|sample|template|dist)[\w.-]+)?|[^\s/]*\.(?:pem|key|p12|pfx)"
        r"|(?:credentials|secrets?)(?:\.(?:json|ya?ml|toml))?|id_(?:rsa|ed25519)",
        [re.escape(os.path.basename(x)) for x in sec_extra])
    found("gitignore", "%d ignore entries; %d extra read exclusions, %d secret patterns" % (len(ignore), len(extra), len(sec_extra))) \
        if ignore else missing("gitignore", "no .gitignore; defaults only")
    templates = [f for f in live if re.search(r"\.env\.(example|sample|template)$|\.env\.dist$", f)]
    (found if templates else missing)("secrets", ", ".join(templates[:4]) if templates else "no .env.example-style template")

    # protected paths + infra packs + prod workflows
    prot = [d for d in (".github", "infra", "terraform", "k8s", "helm", "deploy", "docker", ".circleci") if d in tops]
    if mig and "/" not in mig[5].replace("/versions", "").replace("**/", ""):
        prot.append(mig[5].split("/")[0])
    prot = list(dict.fromkeys(prot))
    slots["protected_rx"] = r"(?:^|/)(?:%s)/|(?:^|/)Dockerfile[^/]*$" % "|".join(map(re.escape, prot or [".github"]))
    slots["protected_example"] = "/repo/%s/ci.yml" % (prot[0] if prot else ".github")
    (found if prot else missing)("protected", ", ".join(prot) if prot else "none found; .github/ and Dockerfile assumed")
    wf = [f for f in live if f.startswith(".github/workflows/")]
    wf_text = "".join(_read(repo / f, 60_000) for f in wf[:40])
    prod_wf = [os.path.splitext(os.path.basename(f))[0] for f in wf if re.search(r"prod|deploy|release", f, re.I)][:5]
    slots["prod_workflow_alt"] = ("|gh workflow run[^|;&]*%s" % _alt(map(re.escape, prod_wf))) if prod_wf else ""
    (found if wf else missing)("ci", "%d workflows%s" % (len(wf), ("; production: " + ", ".join(prod_wf)) if prod_wf else "")
                               if wf else "no .github/workflows")
    sig["ci"]["local_checks"] = sorted({m.strip() for m in re.findall(
        r"^\s*(?:-\s*)?run:\s*(.+)$", wf_text, re.M) if re.match(
        r"(uv lock --check|alembic (heads|check)|npm run (lint|typecheck)|make \w+|ruff |mypy |tsc |cargo (fmt|clippy)|go vet)", m.strip())})[:8]
    if any(f.endswith(".tf") for f in live):
        slots["terraform"] = True
    if any(re.search(r"(^|/)(k8s|helm|charts|kustomize)/", f) for f in live) or "kubectl" in wf_text:
        slots["k8s"] = True
    if re.search(r"\baws\s+\w", wf_text) or re.search(r'provider\s+"aws"', "".join(_read(repo / f, 20_000) for f in live if f.endswith(".tf"))[:200_000]):
        slots["aws"] = True
    if any(os.path.basename(f).startswith("Dockerfile") or re.search(r"compose[^/]*\.ya?ml$", f) for f in live):
        slots["docker"] = True
    packs = [k for k in ("docker", "terraform", "k8s", "aws") if slots.get(k)]
    if slots.get("terraform") or slots.get("k8s") or slots.get("aws"):
        slots["infra"] = True
    (found if packs else missing)("infra", ", ".join(packs) if packs else "no Docker / Terraform / Kubernetes / AWS usage found")

    runner = [r for r in ("Makefile", "justfile", "Taskfile.yml") if r in fset] + (["package.json scripts"] if scripts else [])
    (found if runner else missing)("runner", ", ".join(runner) if runner else "no task runner; raw tool names used")
    agentmd = [f for f in live if os.path.basename(f) in ("CLAUDE.md", "AGENTS.md", ".cursorrules", "CONTRIBUTING.md")][:6]
    (found if agentmd else missing)("agentmd", ", ".join(agentmd) if agentmd else "no agent instruction files")

    return {"repo": slots["repo"], "root": str(repo.resolve()), "signals": sig, "slots": slots}


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _json(path: Path):
    try:
        return json.loads(_read(path)) if path.is_file() else {}
    except ValueError:
        return {}


# ───────────────────────────── seed ─────────────────────────────

_SLOT = re.compile(r"\{\{(\w+)\}\}")


class MissingSlot(KeyError):
    pass


def _fill(node, slots: dict):
    if isinstance(node, str):
        whole = _SLOT.fullmatch(node)
        if whole:                                   # "{{read_threshold}}" → 350, a list stays a list
            if whole.group(1) not in slots:
                raise MissingSlot(whole.group(1))
            return slots[whole.group(1)]

        def sub(m):
            if m.group(1) not in slots:
                raise MissingSlot(m.group(1))
            return str(slots[m.group(1)])
        return _SLOT.sub(sub, node)
    if isinstance(node, list):
        return [_fill(n, slots) for n in node]
    if isinstance(node, dict):
        return {k: _fill(v, slots) for k, v in node.items()}
    return node


_BODY_KEYS = ("title", "statement", "delivery", "mode", "matcher", "ordering", "anchors",
              "scope_paths", "scope_exclude_paths", "min_hook_version")

# dropped.json is read out to the client, so a missing slot is named in their words.
_SLOT_WORDS = {"test_cmd_rx": "recognised test command", "lint_cmd_rx": "linter or formatter",
               "src_ext_rx": "recognised toolchain", "test_slow_content_rx": "Python or Go test suite",
               "mig_tool": "migrations directory", "mig_versions_path_rx": "migrations directory",
               "mig_anchors": "migrations directory", "alembic": "alembic migrations",
               "devserver_rx": "dev server", "docker": "Dockerfile or compose file",
               "infra": "Terraform, Kubernetes or AWS usage",
               "heavy_anchors": "uniquely-named source file over 1,000 lines",
               "lock_cmd_rx": "lockfile", "lock_pairs": "lockfile beside its manifest", "manifest_paths": "dependency manifest",
               "manifest_rx": "dependency manifest", "install_cmd_rx": "dependency manifest",
               "slow_flag_rx": "slow or coverage test tier", "test_path_rx": "test directory",
               "src_diff_rx": "clear source root", "tests_diff_rx": "test directory",
               "default_branch_rx": "known default branch (origin/HEAD is not set)",
               "default_branch_esc": "known default branch (origin/HEAD is not set)"}


def seed(signals: dict, catalog: dict, scope_repo: bool = True) -> tuple[list, list]:
    slots = signals["slots"]
    out, dropped = [], []
    for rule in catalog["rules"]:
        need = [r for r in rule.get("requires", []) if not slots.get(r)]
        if need:
            dropped.append({"id": rule["id"], "category": rule["category"], "title": rule["title"],
                            "reason": "this repo has no %s" % " / ".join(
                                dict.fromkeys(_SLOT_WORDS.get(n, n) for n in need))})
            continue
        if rule.get("for_each"):                   # one candidate per record, each with its own slots
            for item in slots[rule["for_each"]]:
                one = seed({"slots": {**slots, **item, rule["for_each"]: None}},
                           {"version": catalog["version"], "rules": [{k: v for k, v in rule.items() if k != "for_each"
                                                                      and not (k == "requires")}]}, scope_repo)
                out += one[0]; dropped += one[1]
            continue
        try:
            filled = _fill({k: rule[k] for k in rule if k not in ("requires",)}, slots)
        except MissingSlot as exc:
            dropped.append({"id": rule["id"], "category": rule["category"], "title": rule["title"],
                            "reason": "the scan could not fill `%s`" % exc.args[0]})
            continue
        statement = "%s Why: %s" % (filled["statement"].rstrip(), filled["why"].strip())
        if len(statement) > 400:                    # create_rule refuses, it does not truncate
            statement = filled["statement"].rstrip()
        body = {k: filled[k] for k in _BODY_KEYS if filled.get(k) is not None}
        body["statement"] = statement
        if body.get("delivery") != "agent_hook":
            body.pop("mode", None)                  # the server refuses a mode on notes and anchors
        body["scope_repos"] = [slots["repo"]] if scope_repo else []
        body["source"] = "authored"
        body["source_ref"] = "starter-rulebook@%s#%s" % (catalog["version"], filled["id"])
        engine = body.get("matcher") or body.get("ordering") or {}
        out.append({"id": filled["id"], "category": rule["category"], "designed_mode": rule.get("mode"),
                    # A transcript has no branch, diff, dirty flag, file size or agent identity, so the
                    # session replay evaluates the PATTERN alone. For these rows its count is a ceiling
                    # on how often the pattern is even in play — never a fire rate.
                    "replay_is_ceiling": bool(engine.get("given") or body.get("scope_paths")),
                    "seeded_from": rule.get("seeded_from"), "evidence": rule.get("evidence"),
                    "cases": filled.get("cases") or {}, "body": body})
    return out, dropped


# ───────────────────────────── verify ─────────────────────────────

def verify(candidates: list) -> tuple[list, bool]:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))
    import rulebook_verify as V                     # the live hook's own engine, never a copy

    rows, all_ok = [], True
    with tempfile.TemporaryDirectory() as tmp:
        def materialise(case):
            """`{"case": "read:@TMP@/big.json", "tmp_bytes": 60000}` — a read rule
            on `bytes_gt` measures the file on disk, so the case needs one."""
            if isinstance(case, dict) and case.get("tmp_bytes"):
                case = dict(case)
                path = case["case"].split(":", 1)[1].split("@TMP@/", 1)[1]
                target = Path(tmp) / path
                target.write_text("x" * int(case.pop("tmp_bytes")))
                case["case"] = case["case"].replace("@TMP@", tmp)
            return case

        for cand in candidates:
            body, cases = cand["body"], cand["cases"]
            fires = [materialise(c) for c in cases.get("fires", [])]
            silent = [materialise(c) for c in cases.get("silent", [])] + V._self_mention(body)
            # `scope_paths` / `scope_exclude_paths` on an ordering are applied by the
            # live lane before the engine ever sees the edit, and the verifier
            # replays the engine only. So an edit outside the scope is decided
            # here, by the hook's own `path_in_scope`, and must never reach a
            # --fires case.
            scoped_out = []
            scoped = bool(body.get("scope_paths") or body.get("scope_exclude_paths"))
            if body.get("ordering") and scoped:
                hook_rule = V.H.to_hook_rule(V._hook_row(body)) or {}

                def outside(case) -> bool:
                    edits = [st.strip()[5:] for st in str(case).split(">>") if st.strip().startswith("edit:")]
                    return bool(edits) and not any(V.H.path_in_scope(hook_rule, "/repo/" + e, "/repo") for e in edits)
                scoped_out = [c for c in silent if outside(c)]
                silent = [c for c in silent if not outside(c)]
                fires_out = [c for c in fires if outside(c)]
            ok, testable, lines = V.verify(body, fires, silent)
            lines += ["SILENT ok    %s  (outside scope_paths: never arms)" % c for c in scoped_out]
            if body.get("ordering") and scoped and fires_out:
                ok, lines = False, lines + ["FIRES  FAIL  %s  (outside scope_paths: can never arm)" % c for c in fires_out]
            if body.get("delivery") != "agent_hook":
                testable = False                    # a note or an anchor: the server judges relevance
            if testable and not fires:
                ok, lines = False, lines + ["FIRES  FAIL  the catalog ships no --fires case for this rule"]
            long_rx = [k for blk in (body.get("matcher"), body.get("ordering")) if blk
                       for k, v in blk.items() if k.endswith("_rx") and len(str(v)) > RX_MAX]
            if long_rx:
                ok, lines = False, lines + ["LOAD   FAIL  %s is over %d characters after seeding" % (", ".join(long_rx), RX_MAX)]
            all_ok &= ok
            rows.append({"id": cand["id"], "ok": ok, "fire_testable": testable, "report": lines})
    return rows, all_ok


# ───────────────────────────── cli ─────────────────────────────

def _summary(signals, cands, dropped, rows, catalog) -> str:
    bad = {r["id"] for r in rows if not r["ok"]}
    names = {c["id"]: c["name"] for c in catalog["categories"]}
    lines = ["STARTER RULEBOOK — %s" % signals["repo"], "", "WHAT THE SCAN FOUND"]
    for key, s in signals["signals"].items():
        lines.append("  %-11s %s %s" % (key, "✓" if s["found"] else "–", s["evidence"]))
    lines += ["", "RULES SEEDED, BY CATEGORY"]
    for cat in catalog["categories"]:
        mine = [c for c in cands if c["category"] == cat["id"]]
        if not mine:
            continue
        gates = sum(1 for c in mine if c["designed_mode"] == "gate")
        lines.append("  %s — %d rules (%d stop the command, %d advise)" % (cat["name"], len(mine), gates, len(mine) - gates))
        for c in mine:
            lines.append("    %s %-26s %s" % ("✗" if c["id"] in bad else "·", c["id"], c["body"]["title"]))
    if dropped:
        lines += ["", "LEFT OUT (nothing in this repo for them to guard)"]
        lines += ["  %-26s %s" % (d["id"], d["reason"]) for d in dropped]
    if bad:
        lines += ["", "FAILED VERIFICATION — not to be filed: " + ", ".join(sorted(bad))]
    lines += ["", "%d seeded · %d left out · %d failed verification" % (len(cands), len(dropped), len(bad))]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["scan", "seed", "verify", "all"])
    ap.add_argument("--repo", default=".", help="the checkout to scan (read-only)")
    ap.add_argument("--out", default="starter-out", help="where signals/candidates/verified land")
    ap.add_argument("--catalog", default=str(CATALOG))
    ap.add_argument("--all-repos", action="store_true",
                    help="leave scope_repos empty so the rules bind every repo the rulebook's members work in")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))

    if args.step in ("scan", "all"):
        signals = scan(Path(args.repo))
        (out / "signals.json").write_text(json.dumps(signals, indent=2))
    else:
        signals = json.loads((out / "signals.json").read_text())
    if args.step == "scan":
        print(json.dumps(signals["signals"], indent=2))
        return 0

    if args.step in ("seed", "all"):
        cands, dropped = seed(signals, catalog, scope_repo=not args.all_repos)
        (out / "candidates.json").write_text(json.dumps(cands, indent=2))
        (out / "dropped.json").write_text(json.dumps(dropped, indent=2))
    else:
        cands = json.loads((out / "candidates.json").read_text())
        dropped = json.loads((out / "dropped.json").read_text())
    if args.step == "seed":
        print("%d seeded, %d left out → %s" % (len(cands), len(dropped), out / "candidates.json"))
        return 0

    rows, ok = verify(cands)
    (out / "verified.json").write_text(json.dumps(rows, indent=2))
    for r in rows:
        if not r["ok"]:
            print("── %s" % r["id"])
            print("\n".join("   " + l for l in r["report"]))
    print(_summary(signals, cands, dropped, rows, catalog))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
