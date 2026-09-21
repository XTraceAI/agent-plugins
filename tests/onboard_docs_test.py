"""Self-test for onboard_docs.py — the repo-agnostic document scan + upload.

- The scan assumes no layout: documents are found wherever the repo keeps them
  (here `handbook/` and `services/pay/`, never `docs/`), in a git repo and in a
  plain directory alike.
- Agent instruction files, licences/changelogs, vendored trees and stubs are
  left out, and the reason is counted rather than dropped silently.
- A shelved folder (`archive/`) is listed but never scores as important.
- Names come from automatic capture's `derive_name`, so lineages match.
- `upload` selects by folder / path / score, reports a failed save BY PATH and
  exits non-zero, and keeps going after a failure.

Run: python3 tests/onboard_docs_test.py  (stdlib only; save_artifact is faked).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import onboard_docs as od  # noqa: E402
from md_capture_flush import derive_name  # noqa: E402

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        FAILS += 1


BODY = "\n".join(f"## Section {i}\n" + "words " * 60 for i in range(6))


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build(root: Path) -> None:
    write(root, "README.md", "# Acme Pay\nSee [the ledger design](handbook/ledger-design.md).\n" + BODY)
    write(root, "handbook/ledger-design.md", "# Ledger design\n" + BODY)
    write(root, "handbook/lunch-menu.md", "# Lunch\n" + "soup " * 200)
    write(root, "handbook/archive/old-ledger-spec.md", "# Old ledger spec\n" + BODY)
    write(root, "services/pay/RUNBOOK.md", "---\ntitle: Pay service runbook\n---\n" + BODY)
    write(root, "services/pay/stub.md", "# todo\n")
    write(root, "CLAUDE.md", "# Instructions\n" + BODY)
    write(root, ".claude/notes/plan.md", "# Plan\n" + BODY)
    write(root, "CHANGELOG.md", "# Changelog\n" + BODY)
    write(root, "node_modules/lib/README.md", "# lib\n" + BODY)


print("scan — a plain directory with its own layout")
with tempfile.TemporaryDirectory() as td:
    root = Path(td).resolve()
    build(root)
    result = od.scan(root)
    by_path = {d["path"]: d for d in result["docs"]}
    check(set(by_path) == {"README.md", "handbook/ledger-design.md", "handbook/lunch-menu.md",
                           "handbook/archive/old-ledger-spec.md", "services/pay/RUNBOOK.md"},
          f"found the repo's own documents, wherever they live: {sorted(by_path)}")
    sk = result["skipped"]
    check(any("agent instructions" in k for k in sk) and sk.get("stub") == 1
          and sk.get("not system knowledge") == 1,
          f"agent files, stubs and changelogs are counted as skipped: {sk}")
    check(by_path["handbook/ledger-design.md"]["score"] >= 3
          and "linked from README" in by_path["handbook/ledger-design.md"]["why"],
          "a design doc the README links to scores as important")
    check(by_path["handbook/lunch-menu.md"]["score"] < 3, "an unrelated note does not")
    check(by_path["handbook/archive/old-ledger-spec.md"]["score"] <= 2,
          "a shelved folder is listed but never 'likely important'")
    check(by_path["services/pay/RUNBOOK.md"]["name"] == "Pay service runbook"
          and by_path["services/pay/RUNBOOK.md"]["type"] == "runbook",
          "frontmatter title is the name; type inferred from the document")
    p = root / "handbook/ledger-design.md"
    check(by_path["handbook/ledger-design.md"]["name"] == derive_name(p, p.read_text(), root),
          "names match automatic capture's derive_name (one lineage, not two)")
    check(not any(d["in_spec_dir"] for d in result["docs"]), "no spec dir in this repo → none flagged")

print("scan — the same tree as a git repo lists only TRACKED documents")
with tempfile.TemporaryDirectory() as td:
    root = Path(td).resolve()
    build(root)
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    ok = subprocess.run(git + ["init", "-q"], capture_output=True).returncode == 0
    if ok:
        subprocess.run(git + ["add", "README.md", "handbook/ledger-design.md"], capture_output=True)
        paths = {d["path"] for d in od.scan(root)["docs"]}
        check(paths == {"README.md", "handbook/ledger-design.md"}, f"untracked documents are not offered: {sorted(paths)}")
    else:
        print("  skip (git unavailable)")

print("upload — selection, and failures reported by path")
with tempfile.TemporaryDirectory() as td:
    root = Path(td).resolve()
    build(root)
    manifest = Path(td) / "manifest.json"
    manifest.write_text(json.dumps(od.scan(root)), encoding="utf-8")
    runs: list[list[str]] = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        bad = "lunch-menu" in " ".join(cmd)
        return types.SimpleNamespace(returncode=1 if bad else 0, stdout="",
                                     stderr="ERROR: tags required" if bad else "")

    real_run, od.subprocess.run = od.subprocess.run, fake_run
    try:
        def upload(*argv: str) -> int:
            runs.clear()
            sys.argv = ["onboard_docs.py", "upload", "--manifest", str(manifest), *argv]
            return od.main()

        rc = upload("--only-folder", "handbook", "--min-score", "3")
        files = [c[c.index("--file") + 1] for c in runs]
        check(rc == 0 and files == [str(root / "handbook/ledger-design.md")],
              f"folder + score floor picks the design doc only: {files}")

        rc = upload("--only-folder", "handbook")
        check(rc == 1 and len(runs) == 3, f"one failed save → exit 1, and the others still ran ({len(runs)} runs)")

        rc = upload("--path", "handbook/lunch-menu.md", "--min-score", "9")
        check(len(runs) == 1, "a path the user named is uploaded whatever its score")

        rc = upload("--only-folder", "handbook", "--exclude", "handbook/lunch-menu.md")
        check(rc == 0 and len(runs) == 2, "--exclude drops one document from a folder")

        rc = upload("--min-score", "3")
        files = sorted(Path(c[c.index("--file") + 1]).relative_to(root).as_posix() for c in runs)
        check(rc == 0 and files == ["README.md", "handbook/ledger-design.md", "services/pay/RUNBOOK.md"],
              f"no folder named + score floor = every important document, shelved ones left out: {files}")

        rc = upload("--min-score", "3", "--exclude-folder", "handbook")
        files = sorted(Path(c[c.index("--file") + 1]).relative_to(root).as_posix() for c in runs)
        check(files == ["README.md", "services/pay/RUNBOOK.md"], "--exclude-folder drops a folder and its subfolders")

        rc = upload("--only-folder", "nope")
        check(rc == 2 and not runs, "a selection matching nothing is an error, not a silent success")

        rc = upload("--only-folder", ".", "--dry-run")
        check(rc == 0 and not runs, "--dry-run sends nothing")

        upload("--path", "services/pay/RUNBOOK.md")
        cmd = runs[-1]
        check(cmd[cmd.index("--name") + 1] == "Pay service runbook"
              and cmd[cmd.index("--tags") + 1] == "runbook,pay"
              and "--agent-brain-id" not in cmd,
              "name/type/tags come from the manifest; routing is left to the room cache")
    finally:
        od.subprocess.run = real_run

print()
print("ALL PASSED (0 failures)" if not FAILS else f"{FAILS} FAILURE(S)")
sys.exit(1 if FAILS else 0)
