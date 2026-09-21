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
        for rid in ("push-main", "migration-heads", "lockfile-drift", "suite-before-push", "anchor-heavy",
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
        check("notes and anchors carry no mode (the server refuses one)",
              all("mode" not in c["body"] for c in cands if c["body"]["delivery"] != "agent_hook"))
        check("every statement fits the server's 400-character cap", all(len(c["body"]["statement"]) <= 400 for c in cands))
        check("source_ref is stable across runs", by_id["push-main"]["source_ref"] == "starter-rulebook@%s#push-main" % CATALOG["version"])
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
        check("the lock tool is pnpm", "pnpm install" in by_id["lockfile-drift"]["ordering"]["required_command_rx"])
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
