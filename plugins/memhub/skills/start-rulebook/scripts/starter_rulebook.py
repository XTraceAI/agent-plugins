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
GIT = CMD + r"(?:sudo\s+)?git\s+(?:-[cC]\s*\S+\s+)?"
RX_MAX = 400

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
    pkg = _json(repo / "package.json")
    scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
    makefile = _read(repo / "Makefile")
    make_targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile, re.M))
    pm = "pnpm" if "pnpm-lock.yaml" in fset else "yarn" if "yarn.lock" in fset else \
        "bun" if ("bun.lockb" in fset or "bun.lock" in fset) else "npm"

    test_rx, targeted, examples, slow_flags, slow_content = [], [], [], [], []
    py_pytest = False
    if "python" in chains:
        # A pyproject.toml makes a repo Python; it does not make its runner pytest. An ordering
        # gate is cleared only by a green run MATCHING its pattern, so a guessed `pytest` on a
        # unittest or tox repo is a gate nobody can ever satisfy. Name the runner from evidence,
        # and with none, leave the test rules out.
        py_cfg = "".join(_read(repo / n) for n in ("pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py", "pytest.ini")
                         if n in fset) + "".join(_read(repo / n) for n in fset if re.fullmatch(r"requirements[^/]*\.txt", n))
        runners = []
        if names["pytest.ini"] or names["conftest.py"] or re.search(r"\bpytest\b", py_cfg):
            runners.append((r"(?:uv run |poetry run |python3? -m )?pytest", "pytest"))
        if names["tox.ini"] or "[tool.tox" in py_cfg:
            runners.append((r"(?:uv run |python3? -m )?tox\b", "tox"))
        if names["noxfile.py"]:
            runners.append((r"(?:uv run |python3? -m )?nox\b", "nox"))
        if names["manage.py"] and not runners:
            runners.append((r"python3? manage\.py test", "python manage.py test"))
        if not runners:
            test_files = [f for f in live if re.search(r"(^|/)test_[^/]+\.py$", f)][:20]
            if any("unittest" in _read(repo / f, 4000) for f in test_files):
                runners.append((r"python3? -m unittest", "python -m unittest"))
        py_pytest = any(ex == "pytest" for _, ex in runners)
        test_rx += [rx for rx, _ in runners]
        if runners:
            targeted.append(r"\s\S*(?:tests?/|\.py\b|::)|\s-k\s|\s--lf\b|\s--last-failed\b|\s-e\s+\S")
            examples.append(runners[0][1])
        slow_content.append(r"time\.sleep\(|\b(?:requests|httpx)\.(?:get|post|put|delete|Client)\(")
    if "node" in chains:
        test_rx.append(r"(?:npm|pnpm|yarn|bun)(?: run)? test|(?:npx |pnpm exec )?(?:jest|vitest)")
        targeted.append(r"\s\S*\.(?:test|spec)\.|\s\S*(?:tests?|__tests__)/|\s-t\s|--testPathPattern")
        examples.append("%s test" % pm)
    if "go" in chains:
        test_rx.append(r"go test")
        targeted.append(r"\s-run\s|go test\s+\./(?!\.\.\.)\S")
        examples.append("go test ./...")
        slow_content.append(r"time\.Sleep\(|http\.(?:Get|Post)\(")
    if "rust" in chains:
        test_rx.append(r"cargo test")
        targeted.append(r"cargo test\s+[a-zA-Z_]")
        examples.append("cargo test")
    if "test" in make_targets:
        test_rx.append(r"make test")
    if chains:
        found("toolchain", ", ".join(chains) + ("" if test_rx else " — but no test runner it recognises, so the test rules are left out"),
              value=chains, test_runner=examples[0] if examples else None)
        if test_rx:                                 # no runner found → no slot → every rule that needs one is dropped
            slots["test_cmd_rx"] = _alt(test_rx)
            slots["targeted_rx"] = "|".join(targeted)
            slots["test_cmd_example"] = examples[0]
        slots["src_ext_rx"] = r"\.(?:%s)$" % "|".join(e for c in chains for e in _SRC_EXT[c])
        if slow_content:
            slots["test_slow_content_rx"] = "|".join(slow_content)
            slots["test_slow_example"] = "/repo/pkg/a_test.go::time.Sleep(5)" if chains[0] == "go" \
                else "/repo/tests/test_a.py::time.sleep(5)"
    else:
        missing("toolchain", "no pyproject.toml / package.json / go.mod / Cargo.toml")

    # markers / slow tier
    cfg = _read(repo / "pyproject.toml") + "\n" + _read(repo / "pytest.ini") + "\n" + _read(repo / "setup.cfg")
    block = re.search(r"markers\s*=\s*\[?(.*?)(?:\n\s*\]|\n\S|\Z)", cfg, re.S)
    marks = re.findall(r"^\s*[\"']?([A-Za-z_]\w*)\s*[:\"']", block.group(1), re.M) if block else []
    slow = [m for m in dict.fromkeys(marks)
            if re.search(r"slow|behavio|e2e|integration|perf|load|live|network|smoke", m)]
    if py_pytest:                                   # --cov and -m are pytest's flags, not tox's or unittest's
        slow_flags.append(r"--cov\b")
        if slow:
            slow_flags.append(r"-m\s+[\"']?(?:%s)" % "|".join(map(re.escape, slow)))
    if "node" in chains:
        slow_flags.append(r"--coverage\b")
    if "go" in chains:
        slow_flags.append(r"\s-(?:race|cover(?:profile)?)\b")
    if slow_flags:
        found("markers", ("markers: " + ", ".join(slow)) if slow else "no slow markers defined; coverage flags only",
              value=slow)
        slots["slow_flag_rx"] = "|".join(slow_flags)
        slots["slow_example"] = "pytest --cov=app" if py_pytest else \
            "%s test -- --coverage" % pm if "node" in chains else "go test -race ./..."
        slots["slow_words_rx"] = r"(?i:\b(?:%s)\b)" % "|".join(
            dict.fromkeys(slow + ["slow", "e2e", "coverage", "integration"]))
    else:
        missing("markers", "no toolchain with a known slow tier")

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
        lock_tool = [(n, rx, ex) for n, rx, ex in (
            ("uv.lock", r"uv lock", "uv lock"), ("poetry.lock", r"poetry lock", "poetry lock"),
            ("package-lock.json", r"npm install", "npm install"), ("pnpm-lock.yaml", r"pnpm install", "pnpm install"),
            ("yarn.lock", r"yarn(?: install)?\b", "yarn install"), ("go.sum", r"go mod tidy", "go mod tidy"),
            ("Cargo.lock", r"cargo (?:update|generate-lockfile|build|check)", "cargo update")) if names[n]]
        if lock_tool:
            slots["lock_cmd_rx"] = _alt([t[1] for t in lock_tool])
            slots["lock_cmd_example"] = lock_tool[0][2]
            slots["manifest_example"] = mans[0] if mans else reqs[0]
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
        mig = ("alembic", r"(?:alembic|migrations)/versions/.*\.py$", r"|alembic (?:downgrade|stamp)",
               "alembic revision --autogenerate", ["alembic", "alembic.ini"], "alembic/versions")
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
    extra = [d for d in ign_dirs if d not in ("dist", "build", "node_modules", "vendor", ".venv", "__pycache__", "target", ".next")
             and not d.startswith(".env")][:8]
    slots["never_read_rx"] = _fit(base_never, ["(?:^|/)%s/" % re.escape(d) for d in extra])
    sec_extra = [l.lstrip("/") for l in ignore if re.search(r"secret|credential|\.pem|\.key|token", l, re.I)
                 and re.fullmatch(r"[\w./-]+", l)][:6]
    base_secret = (r"(?:^|/)\.env(?:\.(?!example|sample|template|dist)[\w.-]+)?$|\.(?:pem|key|p12|pfx)$"
                   r"|(?:^|/)(?:credentials|secrets?)(?:\.(?:json|ya?ml|toml))?$|(?:^|/)id_(?:rsa|ed25519)$")
    slots["secrets_rx"] = _fit(base_secret, ["(?:^|/)%s$" % re.escape(s) for s in sec_extra])
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
               "lock_cmd_rx": "lockfile", "manifest_paths": "dependency manifest",
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
        body["source_ref"] = "starter-rulebook@%s#%s" % (catalog["version"], rule["id"])
        out.append({"id": rule["id"], "category": rule["category"], "designed_mode": rule.get("mode"),
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
            # `scope_paths` on an ordering is applied by the live lane before the
            # engine ever sees the edit, and the verifier replays the engine only.
            # So an edit outside the scope is decided here, by the hook's own
            # `path_in_scope`, and must never reach a --fires case.
            scoped_out = []
            if body.get("ordering") and body.get("scope_paths"):
                hook_rule = V.H.to_hook_rule(V._hook_row(body)) or {}

                def outside(case) -> bool:
                    edits = [st.strip()[5:] for st in str(case).split(">>") if st.strip().startswith("edit:")]
                    return bool(edits) and not any(V.H.path_in_scope(hook_rule, "/repo/" + e, "/repo") for e in edits)
                scoped_out = [c for c in silent if outside(c)]
                silent = [c for c in silent if not outside(c)]
                fires_out = [c for c in fires if outside(c)]
            ok, testable, lines = V.verify(body, fires, silent)
            lines += ["SILENT ok    %s  (outside scope_paths: never arms)" % c for c in scoped_out]
            if body.get("ordering") and body.get("scope_paths") and fires_out:
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
