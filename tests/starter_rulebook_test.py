"""starter-rulebook: the catalog is data, so this is what keeps it honest.

Every rule the catalog ships must, once seeded from a repo, LOAD in the live
hook and behave on the cases shipped beside it — for each toolchain the scan
knows, because a slot filled for Python proves nothing about the same rule
filled for Node. And a rule whose signal is missing must be dropped, never
filed with a guess: a bare repo seeds only the universal rules.

Run: python3 starter_rulebook_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "memhub" / "skills" / "start-rulebook"
SCRIPT = SKILL / "scripts" / "starter_rulebook.py"
CATALOG = json.loads((SKILL / "catalog.json").read_text(encoding="utf-8"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failures.append(label + (": " + detail if detail else ""))
    print(f"  {'ok ' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))


PYTHON_REPO = {
    "pyproject.toml": '[project]\nname = "svc"\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                      'markers = [\n  "slow: long",\n  "unit: fast",\n]\n[dependency-groups]\ndev = ["ruff", "mypy", "pytest"]\n',
    "uv.lock": "version = 1\n",
    ".gitignore": ".env\n.venv/\nhtmlcov/\nsecrets.json\n",
    ".env.example": "DATABASE_URL=\n",
    "alembic.ini": "[alembic]\n",
    "alembic/versions/0001_init.py": "revision = '0001'\n",
    "Dockerfile": 'FROM python:3.12\nCMD ["uvicorn", "app.main:app"]\n',
    ".github/workflows/deploy-production.yml": "jobs:\n  d:\n    steps:\n      - run: uv lock --check\n",
    "app/main.py": "x = 1\n", "app/a.py": "x = 1\n", "app/b.py": "x = 1\n",
    "app/big_service.py": "x = 1\n" * 1200,
    "tests/test_main.py": "def test_x():\n    assert True\n",
    "CLAUDE.md": "# rules\n",
}
NODE_REPO = {
    "package.json": json.dumps({"name": "web", "scripts": {"test": "vitest run", "lint": "eslint .", "dev": "vite"},
                                "devDependencies": {"eslint": "9", "vitest": "2", "typescript": "5"}}),
    "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    ".gitignore": "node_modules/\ndist/\n.env\n",
    "src/a.ts": "export {}\n", "src/b.ts": "export {}\n", "src/c.ts": "export {}\n",
    "src/a.test.ts": "import { it } from 'vitest'\n",
    "main.tf": 'provider "aws" {}\n',
}
BARE_REPO = {"README.md": "# nothing here yet\n"}


def _make(files: dict, where: Path) -> Path:
    for rel, text in files.items():
        p = where / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    for args in (["init", "-q", "-b", "trunk"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                 ["remote", "add", "origin", "https://example.com/acme/%s.git" % where.name],
                 ["update-ref", "refs/remotes/origin/trunk", "HEAD"],
                 ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"]):
        subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True, env=env)
    return where


def _run(repo: Path, out: Path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    p = subprocess.run([sys.executable, str(SCRIPT), "all", "--repo", str(repo), "--out", str(out)],
                       capture_output=True, text=True, env=env, timeout=300)
    load = lambda n: json.loads((out / n).read_text())
    return p, load("signals.json"), load("candidates.json"), load("dropped.json"), load("verified.json")


def test_the_catalog_is_well_formed() -> None:
    ids = [r["id"] for r in CATALOG["rules"]]
    titles = [r["title"] for r in CATALOG["rules"]]
    cats = {c["id"] for c in CATALOG["categories"]}
    check("rule ids are unique", len(ids) == len(set(ids)))
    check("titles are unique (the server's re-import identity includes the title)", len(titles) == len(set(titles)))
    check("every rule sits in a declared category", all(r["category"] in cats for r in CATALOG["rules"]))
    check("every category has a plain-language `ask`", all(c.get("ask") for c in CATALOG["categories"]))
    for r in CATALOG["rules"]:
        blocks = [b for b in (r.get("matcher"), r.get("ordering")) if b]
        text = json.dumps(blocks)
        # `^` inside a character class or a lookaround is fine; a pattern that OPENS with it is not.
        anchored = [v for b in blocks for k, v in b.items()
                    if k in ("command_rx", "required_command_rx", "gated_command_rx") and str(v).startswith("^")]
        check(f"{r['id']}: no command pattern is anchored at the start of the string", not anchored)
        check(f"{r['id']}: no path pattern assumes a repo-relative path",
              not re.search(r'"path(?:_not)?_rx": "\^(?!/)[\w(]', text))
        if r.get("delivery") == "agent_hook":
            check(f"{r['id']}: ships a fires case", bool((r.get("cases") or {}).get("fires")))
            check(f"{r['id']}: ships a silent case", bool((r.get("cases") or {}).get("silent")))
        check(f"{r['id']}: title is 3-8 words", 3 <= len(r["title"].split()) <= 8, r["title"])


def test_a_python_service_seeds_every_rule_and_all_of_them_verify() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(PYTHON_REPO, Path(tmp) / "svc")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        bad = [r["id"] for r in rows if not r["ok"]]
        check("exit 0", p.returncode == 0, p.stdout[-600:] + p.stderr[-600:])
        check("every seeded rule verifies", not bad, ", ".join(bad))
        check("repo name comes from the remote, not the directory", signals["repo"] == "svc")
        check("default branch comes from origin/HEAD", signals["slots"]["default_branch_rx"] == "^(trunk)$")
        by_id = {c["id"]: c["body"] for c in cands}
        for rid in ("push-main", "migration-heads", "lockfile-drift-uv", "suite-before-push", "anchor-heavy",
                    "docker-destructive", "slow-only-asked", "no-dev-server"):
            check(f"seeds {rid}", rid in by_id)
        check("the slow tier is the repo's own marker, and only the slow one",
              "slow" in by_id["slow-only-asked"]["matcher"]["command_rx"]
              and "unit" not in by_id["slow-only-asked"]["matcher"]["command_rx"])
        check("the heavy-file anchor is the repo's own file", by_id["anchor-heavy"]["anchors"] == ["big_service.py"])
        check("migration scope is a glob that can match a file", by_id["migration-heads"]["scope_paths"] == ["alembic/versions/*"])
        check("the production workflow reaches the prod gate",
              bool(re.search(by_id["prod-commands"]["matcher"]["command_rx"], "gh workflow run deploy-production.yml")))
        check("infra pack stays out of a repo with no Terraform/k8s/AWS", "infra-destroy" in {d["id"] for d in dropped})
        check("rules are scoped to the repo", all(c["body"]["scope_repos"] == ["svc"] for c in cands))
        never = by_id["read-never"]["matcher"]["path_rx"]
        check("an ignore-template line for a directory that is not here never becomes a read gate",
              "htmlcov" not in never and not re.search(never, "/repo/src/lib/parser.py"), never)
        check("notes and anchors carry no mode (they cannot gate; advise is the default)",
              all("mode" not in c["body"] for c in cands if c["body"]["delivery"] != "agent_hook"))
        check("every statement fits the server's 400-character cap", all(len(c["body"]["statement"]) <= 400 for c in cands))
        check("source_ref names the rule and catalog after '#'", by_id["push-main"]["source_ref"] == "starter-rulebook#push-main|catalog %s" % CATALOG["version"])
        # The server's re-file key base (reimport.source_ref_base): strip a hex @sha, cut at '#'. A version
        # in the base would make every catalog update file a twin of each rule instead of superseding it.
        base = lambda ref: re.sub(r"@[0-9a-fA-F]{7,64}(?=#|$)", "", ref, count=1).split("#", 1)[0].strip()
        check("the re-file key base is the same for every rule and every catalog version",
              {base(c["body"]["source_ref"]) for c in cands} == {"starter-rulebook"})
        check("nothing was written into the scanned repo",
              subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == "")


def test_a_node_repo_gets_node_commands_and_no_python_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(NODE_REPO, Path(tmp) / "web")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        bad = [r["id"] for r in rows if not r["ok"]]
        check("exit 0", p.returncode == 0, p.stdout[-600:] + p.stderr[-600:])
        check("every seeded rule verifies", not bad, ", ".join(bad))
        by_id = {c["id"]: c["body"] for c in cands}
        gone = {d["id"] for d in dropped}
        check("the test command is the package manager's", "pnpm" in by_id["suite-before-push"]["ordering"]["required_command_rx"])
        check("lint prefers the repo's own entrypoint", "run lint" in by_id["lint-before-push"]["ordering"]["required_command_rx"])
        check("the lock tool is pnpm", bool(re.search(by_id["lockfile-drift-pnpm"]["ordering"]["required_command_rx"], "pnpm install")))
        check("migration rules are dropped", {"migration-heads", "migration-handwritten", "anchor-migrations"} <= gone)
        check("the infra pack arrives with Terraform", "infra-destroy" in by_id)
        check("a dropped rule says why in the client's words",
              all("_rx" not in d["reason"] for d in dropped), "; ".join(d["reason"] for d in dropped))


def test_a_bare_repo_gets_only_the_universal_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(BARE_REPO, Path(tmp) / "bare")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        bad = [r["id"] for r in rows if not r["ok"]]
        check("exit 0 with no toolchain at all", p.returncode == 0, p.stdout[-600:] + p.stderr[-600:])
        check("every seeded rule verifies", not bad, ", ".join(bad))
        ids = {c["id"] for c in cands}
        check("safety survives with nothing to seed from", {"git-irreversible", "rm-rf-wipe", "secrets-read", "push-main"} <= ids)
        check("nothing that needs a test command is filed on a guess",
              not ids & {"suite-before-push", "test-targeted", "reproduce-before-fix", "lint-before-push"})


def test_same_as_names_a_hypothesis_the_session_miner_still_ships() -> None:
    """`same_as` is the only thing standing between a team and the same rule
    filed twice under two titles: the patterns differ, so the deterministic
    conflict check is blind to the pair. A renamed hypothesis on the miner's
    side would silently break that, so both ends are pinned here — the title
    must still exist in mine_sessions.py, and the skill must still name the
    pair to whoever is reading it, since the conflict script never will."""
    miner = (ROOT / "plugins/memhub/skills/start-rulebook/scripts/mine_sessions.py").read_text(encoding="utf-8")
    miner_skill = (ROOT / "plugins/memhub/skills/start-rulebook/SKILL.md").read_text(encoding="utf-8")
    pairs = [(r, t) for r in CATALOG["rules"] for t in r.get("same_as", [])]
    check("the catalog declares its overlaps with the session miner", len(pairs) >= 3)
    for rule, title in pairs:
        check(f"{rule['id']}: the miner still ships `{title}`", '"title": "%s"' % title in miner)
        check(f"{rule['id']}: the skill names the pair", title in miner_skill and "`%s`" % rule["id"] in miner_skill)


def test_the_miner_reads_a_month_unless_asked_for_more() -> None:
    """The default window is the promise the skill makes out loud ("your last
    30 days"). An old transcript is skipped on its mtime before it is parsed,
    `--all` is the only way to read everything, and a --baseline-date widens
    the window by itself so "did friction shrink?" still has a BEFORE."""
    import time
    miner = SKILL / "scripts" / "mine_sessions.py"
    with tempfile.TemporaryDirectory() as home:
        proj = Path(home) / ".claude" / "projects" / "-repo"
        proj.mkdir(parents=True)
        for name, age_days in (("fresh", 2), ("old", 75)):
            f = proj / f"{name}.jsonl"
            f.write_text("{}\n")
            t = time.time() - age_days * 86400
            os.utime(f, (t, t))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "MEMHUB_PLUGIN_SCRIPTS")}
        env.update(HOME=home, USERPROFILE=home)

        def window(*flags):
            p = subprocess.run([sys.executable, str(miner), "--out", str(Path(home) / "o"), "--digest-top", "0", *flags],
                               capture_output=True, text=True, env=env, cwd=home, timeout=120)
            return next((l for l in p.stdout.splitlines() if l.startswith("window:")), p.stderr[-300:])
        check("default: the last 30 days, and the old transcript is not read",
              "last 30 days" in window() and "1 older" in window(), window())
        check("--days widens it", "0 older" in window("--days", "90"), window("--days", "90"))
        check("--all reads everything", "every session" in window("--all"), window("--all"))
        long_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 60 * 86400))
        # /insights facets carry no date: one whose session this run did not read is outside the window
        fdir = Path(home) / ".claude" / "usage-data" / "facets"
        fdir.mkdir(parents=True)
        (fdir / "gone.json").write_text(json.dumps({"session_id": "0ld5e55i-0000-4000-8000-000000000000", "outcome": "not",
                                                     "friction_counts": {"wrong_approach": 1}, "friction_detail": "from the spring"}))

        def out(*flags):
            return subprocess.run([sys.executable, str(miner), "--out", str(Path(home) / "o"), "--digest-top", "0", *flags],
                                  capture_output=True, text=True, env=env, cwd=home, timeout=120).stdout
        check("an /insights facet for a session outside the window is left out, and the report says so",
              "1 /insights facets left out" in out() and "from the spring" not in out())
        check("--all holds /insights facets to nothing, as before", "from the spring" in out("--all"))
        check("but --all with --repo still keeps another repo's facets out",
              "from the spring" not in out("--all", "--repo", "some-repo"))
        check("a baseline keeps 30 days of BEFORE without being asked",
              "last 90 days" in window("--baseline-date", long_ago), window("--baseline-date", long_ago))


def test_the_skill_asks_before_it_reads_and_warns_before_it_waits() -> None:
    """Written for someone who installed MemHub this week: the three-way choice
    comes first, the slow path says it is slow and why, and nobody is left
    thinking a filed rule is a live one."""
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    first_run = skill.index("## S1.")
    ask = skill.index("## 0a.")
    check("the question comes before any scan or session read", ask < first_run < skill.index("## 1. First pass"))
    for label in ("**Starter rules**", "**Rules from my own work**", "**Both**"):
        check(f"offers {label}", label in skill[ask:first_run])
    check("says the mined path takes time, and why", "going through your" in skill[ask:first_run] and "10–20" in skill[ask:first_run])
    check("says nothing turns on by itself", "nothing\nI file turns on by itself" in skill[ask:first_run] or "turns on by itself" in skill[ask:first_run])
    check("--all is only ever the person's ask", "only when they ask for all of" in skill)
    check("a new book is the repo's own, named as create-rule names it, so both skills share one",
          'create_rulebook(name: "Rulebook: <repo>"' in skill)
    check("a book that binds nobody is a failure, not a success", "member_count: 0` as a FAILURE" in skill)


def test_an_unknown_default_branch_drops_the_push_rules_instead_of_guessing() -> None:
    """`main|master` on a repo whose default is `trunk` is a verified gate that
    guards nothing, and it reads as protection. No origin/HEAD, no rule."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(BARE_REPO, Path(tmp) / "nohead")
        subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
                       check=True, capture_output=True)
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        ids, gone = {c["id"] for c in cands}, {d["id"]: d["reason"] for d in dropped}
        check("exit 0", p.returncode == 0, p.stdout[-400:])
        check("neither default-branch rule is seeded", not ids & {"push-main", "push-main-refspec"})
        check("and the client is told why", "origin/HEAD" in gone.get("push-main", ""), str(gone.get("push-main")))
        check("the scan says how to fix it", "set-head" in signals["signals"]["branch"]["evidence"])
        check("no slot carries a guessed branch", "default_branch_rx" not in signals["slots"])


def test_a_push_that_names_the_default_branch_is_caught_from_any_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(BARE_REPO, Path(tmp) / "bare")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        rx = {c["id"]: c["body"] for c in cands}["push-main-refspec"]["matcher"]["command_rx"]
        for cmd in ("git push origin HEAD:trunk", "git push origin feat:refs/heads/trunk", "cd x && git push origin trunk",
                    "git push origin --delete trunk", "git push -d origin trunk", "git push origin :trunk"):
            check(f"fires on `{cmd}`", bool(re.search(rx, cmd)))
        for cmd in ("git push origin feat/x", "git push origin trunk-hotfix", "git push origin HEAD:feat/trunk"):
            check(f"silent on `{cmd}`", not re.search(rx, cmd))


def test_no_statement_promises_a_full_suite_the_engine_cannot_check() -> None:
    """An ordering is discharged by ANY green command matching its pattern, a
    targeted run included. A statement saying "the full suite" would describe
    behaviour the rule does not implement."""
    for r in CATALOG["rules"]:
        if r.get("ordering") and "test_cmd_rx" in json.dumps(r["ordering"].get("required_command_rx", "")):
            check(f"{r['id']}: does not claim a full suite ran", "full suite" not in r["statement"].lower(), r["statement"])


def test_the_python_runner_is_named_from_evidence_never_assumed() -> None:
    """An ordering gate is cleared only by a green run matching its pattern.
    `pytest` guessed onto a tox or unittest repo is a gate nobody can satisfy."""
    base = {"pyproject.toml": '[project]\nname = "lib"\n', "lib/a.py": "x = 1\n", "lib/b.py": "x = 1\n", "lib/c.py": "x = 1\n"}
    cases = (("tox", {**base, "tox.ini": "[tox]\nenvlist = py312\n[testenv]\ncommands = python -m unittest\n"}, "tox", True),
             ("unittest", {**base, "tests/test_a.py": "import unittest\nclass T(unittest.TestCase):\n    pass\n"}, "python -m unittest", True),
             ("no runner at all", base, None, False))
    for label, files, runner, has_rules in cases:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make(files, Path(tmp) / "lib")
            p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
            by_id = {c["id"]: c["body"] for c in cands}
            check(f"{label}: exit 0 and everything seeded verifies", p.returncode == 0 and all(r["ok"] for r in rows), p.stdout[-300:])
            check(f"{label}: the scan names the runner it found", signals["signals"]["toolchain"].get("test_runner") == runner)
            check(f"{label}: the push gate {'exists' if has_rules else 'is left out'}", ("suite-before-push" in by_id) == has_rules)
            if has_rules:
                rx = by_id["suite-before-push"]["ordering"]["required_command_rx"]
                check(f"{label}: the gate is cleared by the repo's own runner", bool(re.search(rx, runner)), rx)
                check(f"{label}: and not by a pytest the repo does not have", not re.search(rx, "pytest"), rx)
            check(f"{label}: no pytest-only flag leaks into a non-pytest repo",
                  "--cov" not in json.dumps(by_id.get("slow-only-asked", {})))


def test_every_mix_of_toolchains_scans_seeds_and_verifies() -> None:
    """Review kept finding one bug at a time in one family: a slot filled from
    the wrong toolchain, or from none (pytest for any pyproject.toml, `npm test`
    for any package.json, a Python case against a Go pattern, an IndexError on a
    Makefile-only repo). Those only show up in COMBINATIONS, so this walks them:
    every mix must scan without raising, verify everything it seeds, and seed
    test rules only where a runner is evidenced — with a case that runner clears."""
    import itertools
    PY = {None: {}, "pytest": {"pyproject.toml": '[project]\nname="p"\n[dependency-groups]\ndev=["pytest"]\n', "tests/test_a.py": "def test_a(): pass\n"},
          "tox": {"pyproject.toml": '[project]\nname="p"\n', "tox.ini": "[tox]\n"},
          "make-only": {"pyproject.toml": '[project]\nname="p"\n', "Makefile": "test:\n\tpython -m unittest\n"},
          "no-runner": {"pyproject.toml": '[project]\nname="p"\n'}}
    NODE = {None: {}, "script": {"package.json": json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "2"}})},
            "placeholder": {"package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})},
            "dep-only": {"package.json": json.dumps({"devDependencies": {"jest": "29"}})}}
    GO = {False: {}, True: {"go.mod": "module m\n", "pkg/a.go": "package pkg\n", "pkg/a_test.go": "package pkg\n"}}
    RUST = {False: {}, True: {"Cargo.toml": '[package]\nname="r"\n', "src/lib.rs": "\n"}}
    evidenced = {"py": {"pytest", "tox", "make-only"}, "node": {"script", "dep-only"}}
    bad = []
    for py, node, go, rust in itertools.product(PY, NODE, GO, RUST):
        files = {"README.md": "x\n", "app/a.py": "x=1\n", **PY[py], **NODE[node], **GO[go], **RUST[rust]}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mix"
            for rel, text in files.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(text)
            out = Path(tmp) / "out"
            p = subprocess.run([sys.executable, str(SCRIPT), "all", "--repo", str(repo), "--out", str(out)],
                               capture_output=True, text=True, timeout=120)
            label = f"py={py} node={node} go={go} rust={rust}"
            if p.returncode != 0 or not (out / "verified.json").is_file():
                bad.append(f"{label}: rc={p.returncode} {(p.stderr or p.stdout)[-160:].strip()}"); continue
            slots = json.loads((out / "signals.json").read_text())["slots"]
            ids = {c["id"] for c in json.loads((out / "candidates.json").read_text())}
            want = py in evidenced["py"] or node in evidenced["node"] or go or rust
            if ("suite-before-push" in ids) != bool(want):
                bad.append(f"{label}: test rules {'missing' if want else 'seeded with no evidenced runner'}")
            if want and not re.search(slots["test_cmd_rx"], slots["test_cmd_example"]):
                bad.append(f"{label}: example {slots['test_cmd_example']!r} does not clear its own gate")
            if "slow_example" in slots and not re.search(slots["test_cmd_rx"], slots["slow_example"]):
                bad.append(f"{label}: slow example {slots['slow_example']!r} is from a runner this repo does not have")
    check("all 80 toolchain mixes scan, seed and verify", not bad, " | ".join(bad[:6]))


def test_a_polyglot_repo_gets_one_lockfile_rule_per_ecosystem() -> None:
    """One shared rule over every lock tool lets `pnpm install` clear the
    obligation an edit to pyproject.toml armed, and uv.lock ships stale."""
    files = {**PYTHON_REPO, "package.json": NODE_REPO["package.json"], "pnpm-lock.yaml": NODE_REPO["pnpm-lock.yaml"]}
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "poly")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        check("exit 0 and everything verifies", p.returncode == 0 and all(r["ok"] for r in rows), p.stdout[-400:])
        check("one rule per pair", {"lockfile-drift-uv", "lockfile-drift-pnpm"} <= set(by_id))
        uv, pnpm = by_id["lockfile-drift-uv"], by_id["lockfile-drift-pnpm"]
        check("each is scoped to its own manifest", uv["scope_paths"] == ["pyproject.toml"] and pnpm["scope_paths"] == ["package.json"])
        check("uv.lock's rule is cleared by uv lock", bool(re.search(uv["ordering"]["required_command_rx"], "uv lock")))
        check("and NOT by another ecosystem's install", not re.search(uv["ordering"]["required_command_rx"], "pnpm install"))
        check("titles differ (the server's re-import identity includes the title)", uv["title"] != pnpm["title"])
        check("source_refs differ", uv["source_ref"] != pnpm["source_ref"])


def test_the_skill_passes_one_session_selection_to_every_miner_call() -> None:
    """A second pass without --repo / --days / --baseline-date rebuilds the
    report from a different corpus than the digests the user already read."""
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    calls = re.findall(r"mine_sessions\.py\"[^`]*?(?=\n```|\n#|\npython3)", skill, re.S)
    mining = [c for c in calls if "--skills-file" in c]
    check("found the first and second pass", len(mining) == 2, str(len(mining)))
    check("both carry the same selection", all('"${SEL[@]}"' in c for c in mining))
    check("and the skill says why", "selection drifted" in skill)


def test_a_cursor_session_is_aged_by_its_activity_not_its_main_db_file() -> None:
    """SQLite keeps recent writes in store.db-wal, and Cursor records recency in
    meta.json: a session worked in today can have a months-old store.db."""
    import time
    miner = SKILL / "scripts" / "mine_sessions.py"
    with tempfile.TemporaryDirectory() as home:
        chat = Path(home) / ".cursor" / "chats" / "ws" / "c1"
        chat.mkdir(parents=True)
        old = time.time() - 80 * 86400
        (chat / "store.db").write_text(""); os.utime(chat / "store.db", (old, old))
        (chat / "meta.json").write_text(json.dumps({"updatedAtMs": int(time.time() * 1000)})); os.utime(chat / "meta.json", (old, old))
        stale = Path(home) / ".cursor" / "chats" / "ws" / "c2"
        stale.mkdir(parents=True)
        (stale / "store.db").write_text(""); os.utime(stale / "store.db", (old, old))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "MEMHUB_PLUGIN_SCRIPTS")}
        env.update(HOME=home, USERPROFILE=home)
        out = subprocess.run([sys.executable, str(miner), "--out", str(Path(home) / "o"), "--digest-top", "0"],
                             capture_output=True, text=True, env=env, cwd=home, timeout=120).stdout
        line = next((l for l in out.splitlines() if l.startswith("window:")), out[-300:])
        check("the live session is kept and only the truly stale one is skipped", "— 1 older" in line, line)


def test_the_replay_sees_reads_so_a_read_rule_is_never_a_false_zero() -> None:
    """The replay used to skip every Read call, so the whole read lane came back
    0 — and the skill reads 0 on a budget rule as "they do not have this
    problem". Both forms the live hook sees must count: the Read tool, and a
    Bash call that prints the file."""
    miner = SKILL / "scripts" / "mine_sessions.py"
    def tool_use(name, inp, i):
        return json.dumps({"type": "assistant", "timestamp": "2026-09-01T00:00:00Z", "sessionId": "s1", "cwd": "/repo",
                           "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
    with tempfile.TemporaryDirectory() as home:
        proj = Path(home) / ".claude" / "projects" / "-repo"
        proj.mkdir(parents=True)
        (proj / "s1.jsonl").write_text("\n".join([
            json.dumps({"type": "user", "timestamp": "2026-09-01T00:00:00Z", "sessionId": "s1", "cwd": "/repo", "message": {"role": "user", "content": "check the config"}}),
            tool_use("Read", {"file_path": "/repo/.env"}, 1),
            tool_use("Bash", {"command": "cd /repo && cat uv.lock"}, 2),
            tool_use("Read", {"file_path": "/repo/src/app.py"}, 3)]) + "\n")
        cands = Path(home) / "c.json"
        cands.write_text(json.dumps([
            {"title": "secrets", "delivery": "agent_hook", "matcher": {"event": "read", "path_rx": "(?:^|/)\\.env$"}},
            {"title": "lockfiles", "delivery": "agent_hook", "matcher": {"event": "read", "path_rx": "\\.lock$"}},
            {"title": "never", "delivery": "agent_hook", "matcher": {"event": "read", "path_rx": "(?:^|/)nothing-like-this$"}}]))
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "MEMHUB_PLUGIN_SCRIPTS")}
        env.update(HOME=home, USERPROFILE=home)
        out = Path(home) / "o"
        p = subprocess.run([sys.executable, str(miner), "--out", str(out), "--all", "--digest-top", "0", "--candidates", str(cands)],
                           capture_output=True, text=True, env=env, cwd=home, timeout=120)
        rows = {r["title"]: r for r in json.loads((out / "proposals.json").read_text()) if r.get("title") in ("secrets", "lockfiles", "never")}
        check("ran", p.returncode == 0 and len(rows) == 3, p.stderr[-300:])
        check("a Read tool call fires a read rule", rows["secrets"]["fired_n"] == 1, str(rows["secrets"].get("fired_n")))
        check("a Bash `cat` of the file fires one too", rows["lockfiles"]["fired_n"] == 1, str(rows["lockfiles"].get("fired_n")))
        check("and a read rule nothing matches is an honest zero", rows["never"]["fired_n"] == 0)


def test_bulk_staging_is_gated_by_what_the_branch_holds_not_by_the_command_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(BARE_REPO, Path(tmp) / "bare")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        c = {x["id"]: x for x in cands}["stage-secrets-bulk"]
        check("seeded and verified", next(r["ok"] for r in rows if r["id"] == "stage-secrets-bulk"))
        check("it reads the branch's changed paths", bool(c["body"]["matcher"]["given"]["repo"]["diff_paths_rx"]))
        check("so its replay count is marked a ceiling", c["replay_is_ceiling"] is True)
        check("while a pure pattern rule is not", {x["id"]: x for x in cands}["rm-rf-wipe"]["replay_is_ceiling"] is False)


def test_only_a_real_install_clears_a_yarn_lock_obligation() -> None:
    files = {"package.json": json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}}), "yarn.lock": "# yarn\n", "src/a.js": "\n"}
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "y")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        rx = {c["id"]: c["body"] for c in cands}["lockfile-drift-yarn"]["ordering"]["required_command_rx"]
        for cmd in ("yarn", "yarn install", "yarn install --frozen-lockfile", "cd web && yarn"):
            check(f"`{cmd}` clears it", bool(re.search(rx, cmd)))
        for cmd in ("yarn test", "yarn lint", "yarn --version", "yarn run build"):
            check(f"`{cmd}` does not", not re.search(rx, cmd))


def test_a_monorepo_is_read_below_its_root() -> None:
    """`web/package.json` makes the repo Node; a root-only read then finds no
    test script and drops every test rule despite the evidence one level down."""
    files = {"README.md": "x\n", "web/package.json": json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}, "devDependencies": {"vitest": "2", "eslint": "9"}}),
             "web/pnpm-lock.yaml": "lockfileVersion: '9.0'\n", "web/src/a.ts": "export {}\n", "web/src/b.ts": "export {}\n", "web/src/c.ts": "export {}\n",
             "api/pyproject.toml": '[project]\nname="api"\n[dependency-groups]\ndev=["pytest"]\n', "api/app/a.py": "x=1\n"}
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "mono")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        check("exit 0 and everything verifies", p.returncode == 0 and all(r["ok"] for r in rows), p.stdout[-400:])
        check("both nested runners are found", set(signals["signals"]["toolchain"]["test_runners"]) >= {"pytest", "pnpm test"},
              str(signals["signals"]["toolchain"].get("test_runners")))
        check("so the push gate exists", "suite-before-push" in by_id)
        check("and the lock rule is pnpm's, scoped to the manifest in web/",
              by_id.get("lockfile-drift-pnpm-web", {}).get("scope_paths") == ["web/package.json"],
              str(sorted(k for k in by_id if k.startswith("lockfile"))))


def test_monorepo_lockfiles_pair_with_the_manifest_in_their_own_directory() -> None:
    """Paired by basename, `a/` (npm) and `b/` (yarn) both scope to every
    package.json: editing a/package.json arms yarn's gate, and the right
    `npm install` leaves it blocking the push."""
    pj = json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}})
    files = {"README.md": "x\n", "a/package.json": pj, "a/package-lock.json": "{}\n", "a/src/i.js": "\n",
             "b/package.json": pj, "b/yarn.lock": "# yarn\n", "b/src/i.js": "\n",
             "package.json": pj, "pnpm-lock.yaml": "lockfileVersion: '9.0'\n", "orphan/yarn.lock": "# no manifest here\n"}
    sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))
    import rulebook_hook as H
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "mono")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        check("exit 0 and everything verifies", p.returncode == 0 and all(r["ok"] for r in rows), p.stdout[-500:])
        check("one rule per lockfile that has a manifest beside it",
              {k for k in by_id if k.startswith("lockfile-drift")} == {"lockfile-drift-pnpm", "lockfile-drift-npm-a", "lockfile-drift-yarn-b"},
              str(sorted(k for k in by_id if k.startswith("lockfile-drift"))))
        def armed_by(rule_id, path):
            b = by_id[rule_id]
            return H.path_in_scope({"_scope_paths": b["scope_paths"], "_scope_exclude_paths": b.get("scope_exclude_paths") or []},
                                   "/repo/" + path, "/repo")
        check("a/package.json arms a's rule", armed_by("lockfile-drift-npm-a", "a/package.json"))
        check("and NOT b's", not armed_by("lockfile-drift-yarn-b", "a/package.json"))
        check("and NOT the root's, though the hook also tries */package.json", not armed_by("lockfile-drift-pnpm", "a/package.json"))
        check("the root manifest arms the root rule", armed_by("lockfile-drift-pnpm", "package.json"))
        titles = [by_id[k]["title"] for k in by_id if k.startswith("lockfile-drift")]
        check("titles are distinct", len(set(titles)) == 3, str(titles))


def test_alembic_revisions_are_found_where_they_live() -> None:
    files = {**{k: v for k, v in PYTHON_REPO.items() if not k.startswith("alembic/")},
             "migrations/env.py": "# alembic env\n", "migrations/versions/0001_init.py": "revision = '0001'\n"}
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "svc")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        check("exit 0 and everything verifies", p.returncode == 0 and all(r["ok"] for r in rows), p.stdout[-400:])
        check("the pre-push check is scoped to the real directory", by_id["migration-heads"]["scope_paths"] == ["migrations/versions/*"],
              str(by_id["migration-heads"]["scope_paths"]))
        check("and the edit rule matches a revision there",
              bool(re.search(by_id["migration-handwritten"]["matcher"]["path_rx"], "/repo/migrations/versions/0002_x.py")))


def test_a_nested_package_past_the_cap_still_does_not_arm_the_root_rule() -> None:
    pj = json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}})
    files = {"package.json": pj, "package-lock.json": "{}\n"}
    for i in range(10):
        files[f"pkgs/p{i:02d}/package.json"] = pj; files[f"pkgs/p{i:02d}/yarn.lock"] = "# yarn\n"
    sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))
    import rulebook_hook as H
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(files, Path(tmp) / "big")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        root = by_id["lockfile-drift-npm"]
        check("the emitted rules are capped", len([k for k in by_id if k.startswith("lockfile-drift")]) == 8)
        check("but the root rule excludes EVERY nested manifest, emitted or not", len(root["scope_exclude_paths"]) == 10,
              str(len(root["scope_exclude_paths"])))
        last = "pkgs/p09/package.json"
        check("so a package with no rule of its own cannot arm the root's",
              not H.path_in_scope({"_scope_paths": root["scope_paths"], "_scope_exclude_paths": root["scope_exclude_paths"]}, "/repo/" + last, "/repo"))


def test_bun_lockfiles_are_paired_like_every_other_tool() -> None:
    for lock in ("bun.lock", "bun.lockb"):
        files = {"package.json": json.dumps({"scripts": {"test": "bun test"}}), lock: "x\n", "src/a.ts": "\n"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make(files, Path(tmp) / "b")
            p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
            by_id = {c["id"]: c["body"] for c in cands}
            check(f"{lock}: seeded and verified", p.returncode == 0 and "lockfile-drift-bun" in by_id, p.stdout[-300:])
            rx = by_id.get("lockfile-drift-bun", {}).get("ordering", {}).get("required_command_rx", "$^")
            check(f"{lock}: `bun install` clears it and `bun test` does not", bool(re.search(rx, "bun install")) and not re.search(rx, "bun test"))


def test_a_preview_never_counts_as_the_real_thing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(PYTHON_REPO, Path(tmp) / "svc")
        p, signals, cands, dropped, rows = _run(repo, Path(tmp) / "out")
        by_id = {c["id"]: c["body"] for c in cands}
        lock = by_id["lockfile-drift-uv"]["ordering"]["required_command_rx"]
        check("`uv lock` clears the lockfile obligation", bool(re.search(lock, "uv lock")))
        check("`uv lock --check` does too — green means the lock is current", bool(re.search(lock, "uv lock --check")))
        check("`uv lock --dry-run` does not — it writes nothing", not re.search(lock, "uv lock --dry-run"))
        push = by_id["push-main"]["matcher"]
        check("a dry-run push on the default branch is not gated", bool(re.search(push["command_not_rx"], "git push --dry-run")))
        bulk = by_id["stage-secrets-bulk"]["matcher"]["command_rx"]
        check("`git add -A` is a bulk stage", bool(re.search(bulk, "git add -A")))
        check("`git add -u` and `git commit -a` cannot stage an untracked file, so they are not",
              not re.search(bulk, "git add -u") and not re.search(bulk, "git commit -am 'x: y'"))
        dot = by_id["stage-secrets-dot"]["matcher"]
        check("`git add .` is its own rule, and only where `.` is as wide as the repo",
              bool(re.search(dot["command_rx"], "git add .")) and not re.search(dot["command_not_rx"], "git add ."))
        check("`cd api && git add .` reaches only api/, so a secret elsewhere does not gate it",
              bool(re.search(dot["command_not_rx"], "cd api && git add .")) and not re.search(bulk, "cd api && git add ."))
        receipt = by_id["suite-before-push"]["ordering"]["required_command_rx"]
        check("a targeted test run clears the push gate, on purpose", bool(re.search(receipt, "pytest tests/test_a.py -q")))
        for info in ("pytest --version", "pytest --help", "pytest --collect-only -q", "uv run pytest --co"):
            check(f"`{info}` runs no test, so it clears nothing", not re.search(receipt, info))
        check("reproduce-before-fix uses the same receipt", by_id["reproduce-before-fix"]["ordering"]["required_command_rx"] == receipt)
        lint = by_id["lint-before-push"]["ordering"]["required_command_rx"]
        check("a real lint run clears the lint gate", bool(re.search(lint, "ruff check .")) and bool(re.search(lint, "mypy app")))
        for info in ("ruff --version", "mypy --help", "ruff check --show-settings"):
            check(f"`{info}` checks no code, so it clears nothing", not re.search(lint, info))
        pm = by_id["push-main"]["matcher"]["command_rx"]
        for cmd in ("git push", "git push origin", "git push -u origin HEAD", "cd x && git push --force-with-lease"):
            check(f"on the default branch `{cmd}` pushes it", bool(re.search(pm, cmd)))
        for cmd in ("git push origin feat/x", "git push origin HEAD:feat/x", "git push upstream v1.2.0"):
            check(f"`{cmd}` names another ref, so the current-branch rule leaves it alone", not re.search(pm, cmd))
        stage = by_id["stage-secrets"]["matcher"]["command_rx"]
        for cmd in ("git add .env", "git add certs/server.key", "git add secrets.json", "git add id_ed25519", "git add a.py config/credentials.yml"):
            check(f"`{cmd}` stages a secret the read gate also knows", bool(re.search(stage, cmd)))
        for cmd in ('git add ".env"', "git add 'certs/server.key'", 'git add -- "secrets.json"'):
            check(f"`{cmd}` — the shell strips the quotes, git still stages the secret", bool(re.search(stage, cmd)))
        for cmd in ("git add .env.example", "git add src/secrets.py", "git add docs/keys.md", "git add src/environment.py", 'git add "src/secrets.py"'):
            check(f"`{cmd}` does not", not re.search(stage, cmd))


def test_a_rule_that_fails_verification_fails_the_run() -> None:
    """The exit code is the gate the skill reads. A catalog whose rule cannot
    fire must not come back 0 — that is how an unverified rule gets filed."""
    broken = json.loads(json.dumps(CATALOG))
    rule = next(r for r in broken["rules"] if r["id"] == "sleep-poll")
    rule["matcher"]["command_rx"] = "{{CMD}}this-never-matches-anything"
    with tempfile.TemporaryDirectory() as tmp:
        repo = _make(BARE_REPO, Path(tmp) / "bare")
        cat = Path(tmp) / "catalog.json"
        cat.write_text(json.dumps(broken))
        p = subprocess.run([sys.executable, str(SCRIPT), "all", "--repo", str(repo), "--out", str(Path(tmp) / "out"),
                            "--catalog", str(cat)], capture_output=True, text=True, timeout=300)
        rows = json.loads((Path(tmp) / "out" / "verified.json").read_text())
        check("exit 1", p.returncode == 1)
        check("and it names the rule", [r["id"] for r in rows if not r["ok"]] == ["sleep-poll"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print("\n%d failure(s)" % len(failures))
    for f in failures:
        print("  -", f)
    sys.exit(1 if failures else 0)
