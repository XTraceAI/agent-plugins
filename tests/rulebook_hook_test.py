"""Self-test for the rulebook hook's three delivery lanes.

Covers the properties that make this safe to ship in everyone's harness:

* every failure path is SILENT and exit-0 — a broken hook must never touch a
  tool call or a session (missing rulebook, corrupt JSON, garbage stdin,
  non-git cwd);
* the session lane serves posture rules in full and everything else as one
  index line — never the whole book;
* the pre lane matches the shell-only segment (heredoc bodies stripped,
  shell after terminators kept), honors not_rx, and dedupes per
  fire_scope=session (the habituation guard);
* `shell_only` + `evaluate()` are pure and importable — the tests replay
  them, so what is tested is what runs;
* ordering rules arm on edits, discharge on a GREEN receipt only, gate the
  push, and keep state per (worktree, branch) — shared by sibling sessions and
  subagents, never leaking across branches;
* the post lane fires on failing result text, gated by cmd_rx;
* repo_scope filters rules to the repo the session is in;
* MEMHUB_RULEBOOK_BASE relocates the book cache, state and ledger together, so
  tests never touch the developer's real state.

Run: python3 rulebook_hook_test.py  (stdlib only).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HOOK = os.path.join(os.path.dirname(__file__), "..",
                    "plugins", "memhub", "scripts", "rulebook_hook.py")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run(mode: str, payload: dict, env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ, **env_extra)
    p = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


def portability_check() -> None:
    """The hook must import on native Windows, where ``fcntl`` is absent."""
    with open(HOOK, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    check(
        "portability: rulebook hook uses the shared lock shim, never fcntl directly",
        "fcntl" not in imports,
        str(sorted(imports)),
    )

    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "portable_lock.py"), "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('loaded untrusted cwd module')\n")
        probe = (
            "import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('rulebook_probe', {HOOK!r}); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module); "
            "print(module.portable_lock.__file__)"
        )
        imported = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=td,
            capture_output=True,
            text=True,
            timeout=30,
        )
        check(
            "portability: embedded import pins the packaged portable_lock",
            imported.returncode == 0
            and os.path.realpath(imported.stdout.strip())
            == os.path.realpath(os.path.join(os.path.dirname(HOOK), "portable_lock.py")),
            imported.stderr or imported.stdout,
        )

        incomplete = os.path.join(td, "incomplete")
        os.mkdir(incomplete)
        incomplete_hook = os.path.join(incomplete, "rulebook_hook.py")
        shutil.copy2(HOOK, incomplete_hook)
        incomplete_repo = os.path.join(td, "incomplete-repo")
        os.makedirs(os.path.join(incomplete_repo, ".git"))
        with open(
            os.path.join(incomplete_repo, ".git", "HEAD"), "w", encoding="utf-8"
        ) as f:
            f.write("ref: refs/heads/main\n")
        incomplete_base = os.path.join(td, "incomplete-base")
        seed_book(
            incomplete_base,
            "incomplete-repo",
            [
                {
                    "id": "missing-shim-gate",
                    "on": "bash",
                    "rx": r"git\s+push",
                    "mode": "gate",
                    "fire_scope": "call",
                    "repo_scope": "any",
                    "status": "active",
                    "text": "Do not push yet",
                    "why": "partial installs must not disable matcher gates",
                }
            ],
        )
        missing_shim = subprocess.run(
            [sys.executable, incomplete_hook, "pre"],
            input=json.dumps(
                {
                    "cwd": incomplete_repo,
                    "session_id": "missing-shim",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push origin main"},
                }
            ),
            cwd=td,
            capture_output=True,
            text=True,
            env=dict(
                os.environ,
                MEMHUB_RULEBOOK_BASE=incomplete_base,
                MEMHUB_RULEBOOK_FETCH="0",
            ),
            timeout=30,
        )
        try:
            missing_output = json.loads(missing_shim.stdout)
        except Exception:
            missing_output = {}
        check(
            "portability: a missing lock shim preserves matcher gates",
            missing_shim.returncode == 0
            and missing_shim.stderr == ""
            and missing_output.get("hookSpecificOutput", {}).get(
                "permissionDecision"
            )
            == "deny",
            missing_shim.stderr or missing_shim.stdout,
        )
        check(
            "portability: a missing lock shim does not create undrainable telemetry",
            not os.path.exists(
                os.path.join(incomplete_base, "ledger", "fires.jsonl")
            ),
        )
        missing_flush = subprocess.run(
            [sys.executable, incomplete_hook, "flush"],
            input="",
            cwd=td,
            capture_output=True,
            text=True,
            env=dict(os.environ, MEMHUB_RULEBOOK_BASE=incomplete_base),
            timeout=30,
        )
        check(
            "portability: missing lock shim skips the lock-dependent flush",
            missing_flush.returncode == 0
            and missing_flush.stdout == ""
            and missing_flush.stderr == "",
            missing_flush.stderr or missing_flush.stdout,
        )


def seed_book(base, repo_name, rules):
    """Write a cached server book for `repo_name` under `base` (what the fetch
    lane would have cached). Rows in the pilot shape (an `on` key) pass
    straight through to_hook_rule."""
    import datetime as _dt
    import hashlib
    d = os.path.join(base, "book")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)[:60]
    h = hashlib.sha1(repo_name.encode("utf-8")).hexdigest()[:8]
    with open(os.path.join(d, f"{safe}-{h}.json"), "w", encoding="utf-8") as f:
        json.dump({"etag": "seed", "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                   "rules": rules}, f)


def ctx(out: str) -> str:
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _row(rid, matcher=None, **extra):
    """A server-shape (`?view=hook`) row — the shape a `given` block and path
    scope arrive in; pilot-shape rows (an `on` key) never carry either."""
    row = {"rule_id": rid, "title": rid, "statement": f"{rid} text", "delivery": "agent_hook",
           "mode": "advise", "version": 1, "status": "active", "scope_repos": []}
    if matcher is not None:
        row["matcher"] = matcher
    row.update(extra)
    return row


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@x", HOME=cwd, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=env, timeout=30)


def branch_name_checks() -> None:
    """A branch name is whatever follows `refs/heads/`, slashes included.
    `given.repo.branch_rx` decides whether a rule fires, so truncating
    `feat/x` to `x` silently broke every rule keyed on a branch prefix."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(HOOK)))
    import rulebook_hook as H
    with tempfile.TemporaryDirectory() as td:
        def head(text):
            p = os.path.join(td, "HEAD")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return H._branch(p)
        check("branch: a slashed name survives whole",
              head("ref: refs/heads/feat/x\n") == "feat/x", head("ref: refs/heads/feat/x\n"))
        check("branch: a deep name survives whole",
              head("ref: refs/heads/user/feat/deep-thing\n") == "user/feat/deep-thing")
        check("branch: a plain name is unchanged", head("ref: refs/heads/main\n") == "main")
        check("branch: a detached HEAD reads detached",
              head("9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c\n") == "detached")
        check("branch: an unreadable HEAD is empty, never an exception",
              H._branch(os.path.join(td, "nope")) == "")
        # the predicate that made this load-bearing
        p = H.Probes("", "feat/x")
        check("given.repo.branch_rx ^feat/ matches a slashed branch",
              H.given_ok({"given": {"repo": {"branch_rx": r"^feat/"}}}, p))
        check("given.repo.branch_rx ^(main|master)$ does not",
              not H.given_ok({"given": {"repo": {"branch_rx": r"^(main|master)$"}}}, p))


def repo_identity_checks() -> None:
    """The repo a session is IN, not the directory it sits in.

    A worktree directory is named after the branch. Claude Code Desktop makes
    one per session under `<root>/.claude/worktrees/<name>`, so keying the
    book on the basename fetched a separate (empty) book per branch and
    matched `scope_repos: ["<repo>"]` — an exact string on the server — in
    none of them."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(HOOK)))
    import repo_identity as RI
    import rulebook_hook as H

    def worktree(root, wt, commondir="../.."):
        """A linked worktree of `root` at `wt`, laid out as git lays one out."""
        gitdir = os.path.join(root, ".git", "worktrees", os.path.basename(wt))
        os.makedirs(gitdir, exist_ok=True)
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(gitdir, "commondir"), "w", encoding="utf-8") as f:
            f.write(commondir + "\n")
        with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/fm-fix/thing\n")
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {gitdir}\n")
        return gitdir

    with tempfile.TemporaryDirectory() as td:
        # A repo whose directory name is NOT its remote name.
        odd = os.path.join(td, "checked-out-as-something-else")
        os.makedirs(odd)
        _git(odd, "init", "-q")
        _git(odd, "remote", "add", "origin", "https://github.com/Org/canonical.git")
        RI._CACHE.clear()
        check("repo: the origin remote names the repo, not the directory",
              RI.repo_name(odd) == "canonical", RI.repo_name(odd))

        # No remote, no worktree: exactly what every caller did before.
        plain = os.path.join(td, "plainrepo")
        os.makedirs(os.path.join(plain, ".git"))
        RI._CACHE.clear()
        check("repo: a remote-less checkout still reads its own basename",
              RI.repo_name(plain, os.path.join(plain, ".git")) == "plainrepo")

        # The bug: a linked worktree, offline (no remote to ask).
        root = os.path.join(td, "myrepo")
        os.makedirs(os.path.join(root, ".git"))
        wt = os.path.join(td, "fm-fix-some-branch")
        gd = worktree(root, wt)
        RI._CACHE.clear()
        check("repo: a linked worktree resolves to the repo it belongs to",
              RI.repo_name(wt, gd) == "myrepo", RI.repo_name(wt, gd))

        # The Desktop shape: the worktree lives INSIDE the project root.
        dwt = os.path.join(root, ".claude", "worktrees", "session-abc")
        dgd = worktree(root, dwt, commondir=os.path.join(root, ".git"))
        RI._CACHE.clear()
        check("repo: a Desktop `.claude/worktrees/<session>` resolves to the project",
              RI.repo_name(dwt, dgd) == "myrepo", RI.repo_name(dwt, dgd))

        # A submodule's commondir is `<super>/.git/modules/<sub>`; its parent
        # is the meaningless `modules`, so the walk must decline it.
        sub = os.path.join(td, "sub")
        subgit = os.path.join(root, ".git", "modules", "sub")
        os.makedirs(subgit)
        os.makedirs(sub)
        with open(os.path.join(subgit, "commondir"), "w", encoding="utf-8") as f:
            f.write(".\n")
        check("repo: a submodule never resolves to `modules`",
              RI._main_worktree_basename(subgit) == "",
              RI._main_worktree_basename(subgit))

        # `git worktree --relative-paths` writes a relative gitdir pointer.
        rel = os.path.join(td, "relwt")
        relgd = worktree(root, rel)
        with open(os.path.join(rel, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: " + os.path.relpath(relgd, rel) + "\n")
        RI._CACHE.clear()
        check("repo: a relative gitdir pointer resolves too",
              H.repo_info(rel)[0] == "myrepo", H.repo_info(rel)[0])

        # Nothing readable at all: the old behaviour, never an exception.
        RI._CACHE.clear()
        check("repo: an unresolvable directory falls back to its basename",
              RI.repo_name(os.path.join(td, "gone", "leaf")) == "leaf")

        # repo_info keeps every other field PHYSICAL: path scope and the diff
        # probes measure this checkout, and ordering state must not be shared
        # with the sibling worktree on another branch.
        name, wroot, wgitdir, branch = H.repo_info(wt)
        check("repo_info: root stays the worktree, not the main repo",
              wroot == wt and wgitdir == gd and branch == "fm-fix/thing",
              f"{wroot} {wgitdir} {branch}")

    # End to end: a repo-scoped rule reaches a session running in a worktree.
    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "scopedrepo")
        os.makedirs(os.path.join(root, ".git"))
        wt = os.path.join(td, "fm-feat-branch-dir")
        worktree(root, wt)
        seed_book(td, "scopedrepo", [
            _row("repo-scoped", {"event": "bash", "command_rx": r"\bcurl\b"},
                 scope_repos=["scopedrepo"]),
        ])
        env = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        _, out = run("pre", {"session_id": "s-wt", "cwd": wt, "tool_name": "Bash",
                             "tool_input": {"command": "curl https://example.com"}}, env)
        check("repo scope: a worktree session gets the main repo's rules",
              "repo-scoped text" in ctx(out), out[:200])


def given_and_scope_checks() -> None:
    """The two exemption keys the pre-0.42 hook parsed and never read, the
    §3.1 path scope it dropped on load, and the `given` block: facts a matched
    rule must also satisfy, answered by read-only git and the local transcript."""
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "scoperepo")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/test-branch\n")
        seed_book(td, "scoperepo", [
            _row("no-bare-ignore", {"event": "edit", "path_rx": r"\.py$",
                                    "content_rx": r"type:\s*ignore",
                                    "content_not_rx": r"type:\s*ignore\[[^\]]+\]\s*#"}),
            _row("post-exempt", {"event": "output", "command_rx": r"\bpytest\b",
                                 "command_not_rx": r"--collect-only", "content_rx": r"FAILED"}),
            _row("src-only", {"event": "edit", "path_rx": r".*"},
                 scope_paths=["src/*"], scope_exclude_paths=["src/vendor/*"]),
            _row("bash-path-scoped", {"event": "bash", "command_rx": r"scoped-cmd"},
                 scope_paths=["src/*"]),
            _row("push-from-main", {"event": "bash", "command_rx": r"^git\s+push\b",
                                    "given": {"repo": {"branch_rx": r"^test-branch$"}}}),
            _row("push-from-other", {"event": "bash", "command_rx": r"^git\s+push\b",
                                     "given": {"repo": {"branch_rx": r"^main$"}}}),
            _row("commit-unasked", {"event": "bash", "command_rx": r"^git\s+commit\b",
                                    "given": {"user": {"not_said_rx": r"\bcommit\b"}}}),
            _row("bad-given", {"event": "bash", "command_rx": r"bad-given-cmd",
                               "given": {"repo": {"nope": 1}}}),
            _row("bad-given-kind", {"event": "bash", "command_rx": r"bad-given-cmd",
                                    "given": {"repo": {"diff_lines_gt": "500"}}}),
        ])
        env = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        n = [0]

        def pre(tool, inp, **kw):
            n[0] += 1
            payload = {"cwd": repo, "session_id": f"gs{n[0]}", "tool_name": tool, "tool_input": inp}
            payload.update(kw)
            return ctx(run("pre", payload, env)[1])

        def post(inp, resp):
            n[0] += 1
            return ctx(run("post", {"cwd": repo, "session_id": f"gs{n[0]}", "tool_name": "Bash",
                                    "tool_input": inp, "tool_response": resp}, env)[1])

        # --- content_not_rx on an edit rule (was parsed, never read) ---------
        c = pre("Edit", {"file_path": f"{repo}/src/a.py", "new_string": "x = f()  # type: ignore"})
        check("edit: content_rx fires on the bare suppression", "[no-bare-ignore]" in c, c)
        c = pre("Edit", {"file_path": f"{repo}/src/a.py",
                         "new_string": "x = f()  # type: ignore[attr-defined]  # stub lacks it"})
        check("edit: content_not_rx exempts the complied-with form", "[no-bare-ignore]" not in c, c)

        # --- command_not_rx on an output rule (was mapped to cmd_not_rx, never read)
        c = post({"command": "pytest -x tests/"}, {"stdout": "FAILED tests/a.py::t", "exit_code": 1})
        check("output: fires on a failing run", "[post-exempt]" in c, c)
        c = post({"command": "pytest --collect-only -q"}, {"stdout": "FAILED to import", "exit_code": 1})
        check("output: command_not_rx exempts the named form", "[post-exempt]" not in c, c)

        # --- scope_paths / scope_exclude_paths (were dropped on load) --------
        c = pre("Edit", {"file_path": f"{repo}/src/a.py", "new_string": "x"})
        check("scope_paths: an edit inside the scope fires", "[src-only]" in c, c)
        c = pre("Edit", {"file_path": f"{repo}/docs/a.md", "new_string": "x"})
        check("scope_paths: an edit outside the scope is silent", "[src-only]" not in c, c)
        c = pre("Edit", {"file_path": f"{repo}/src/vendor/x.py", "new_string": "x"})
        check("scope_exclude_paths: an excluded path is silent", "[src-only]" not in c, c)
        c = pre("Bash", {"command": "scoped-cmd"})
        check("scope_paths: a Bash call carries no path, so an include-scoped rule never fires there",
              "[bash-path-scoped]" not in c, c)

        # --- given.repo.branch_rx: from .git/HEAD, no git needed --------------
        c = pre("Bash", {"command": "git push origin HEAD"})
        check("given.repo.branch_rx: fires when the checked-out branch matches",
              "[push-from-main]" in c, c)
        check("given.repo.branch_rx: silent when it does not", "[push-from-other]" not in c, c)

        # --- given.user: only what the person typed counts -------------------
        def transcript(name, records):
            p = os.path.join(td, name)
            with open(p, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            return p
        prompt = lambda t: {"type": "user", "message": {"role": "user", "content": t}}
        tool_result = {"type": "user", "toolUseResult": {"stdout": "x"},
                       "message": {"role": "user", "content": [
                           {"type": "tool_result", "content": "please commit this"}]}}
        meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": [
            {"type": "text", "text": "injected: commit now"}]}}
        assistant = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I will commit"}]}}
        t_unasked = transcript("t1.jsonl", [prompt("fix the bug in foo"), tool_result, meta, assistant])
        t_asked = transcript("t2.jsonl", [prompt("fix it"), tool_result,
                                          prompt([{"type": "text", "text": "then commit it"}])])
        c = pre("Bash", {"command": "git commit -m x"}, transcript_path=t_unasked)
        check("given.user.not_said_rx: fires when no user turn said it — tool results, meta rows "
              "and assistant turns do not count", "[commit-unasked]" in c, c)
        c = pre("Bash", {"command": "git commit -m x"}, transcript_path=t_asked)
        check("given.user.not_said_rx: silent once the user asked (text-block prompt)",
              "[commit-unasked]" not in c, c)
        c = pre("Bash", {"command": "git commit -m x"})
        check("given.user: no transcript = no fact = silent", "[commit-unasked]" not in c, c)

        # --- a bad given drops the RULE at load, never the hook --------------
        c = pre("Bash", {"command": "bad-given-cmd"})
        check("given: an unknown key drops the rule", "[bad-given]" not in c, c)
        check("given: a wrong value kind drops the rule", "[bad-given-kind]" not in c, c)

        # --- given.repo diff probes against a real repository ----------------
        gr = os.path.join(td, "gitrepo")
        os.makedirs(os.path.join(gr, "src"))
        ok = _git(gr, "-c", "init.defaultBranch=main", "init", "-q").returncode == 0
        if ok:
            with open(os.path.join(gr, "src", "a.py"), "w", encoding="utf-8") as f:
                f.write("a = 1\n")
            ok = _git(gr, "add", ".").returncode == 0 and _git(gr, "commit", "-qm", "base").returncode == 0 \
                and _git(gr, "checkout", "-qb", "feat").returncode == 0
        if not ok:
            check("given.repo diff probes (git unavailable here — skipped)", True)
            return
        seed_book(td, "gitrepo", [
            _row("pr-needs-test", {"event": "bash", "command_rx": r"gh\s+pr\s+create",
                                   "given": {"repo": {"diff_paths_rx": r"^src/",
                                                      "diff_paths_none_rx": r"^tests/"}}}),
            _row("pr-too-big", {"event": "bash", "command_rx": r"gh\s+pr\s+create",
                                "given": {"repo": {"diff_lines_gt": 3}}}),
            # its own trigger: three fires on one call would meet the per-call
            # advisory cap (MAX_ADVISE) and the third would be cut, not silent
            _row("pr-dirty", {"event": "bash", "command_rx": r"gh\s+pr\s+ready",
                              "given": {"repo": {"dirty": True}}}),
        ])
        genv = dict(env)

        def gpre(sid, command="gh pr create --fill"):
            return ctx(run("pre", {"cwd": gr, "session_id": sid, "tool_name": "Bash",
                                   "tool_input": {"command": command}}, genv)[1])

        c = gpre("d0")
        check("given.repo: nothing changed against main → diff_paths_rx unmet, silent",
              "[pr-needs-test]" not in c and "[pr-too-big]" not in c, c)
        check("given.repo.dirty: a fresh checkout is not dirty", "[pr-dirty]" not in gpre("d0r", "gh pr ready"))
        with open(os.path.join(gr, "src", "a.py"), "a", encoding="utf-8") as f:
            f.write("b = 2\nc = 3\nd = 4\ne = 5\n")          # uncommitted: the working tree counts
        c = gpre("d1")
        check("given.repo.diff_paths_rx: an uncommitted source change fires the needs-a-test rule",
              "[pr-needs-test]" in c, c)
        check("given.repo.diff_lines_gt: four added lines exceed 3", "[pr-too-big]" in c, c)
        c = gpre("d1r", "gh pr ready")
        check("given.repo.dirty: an uncommitted change is dirty", "[pr-dirty]" in c, c)
        os.makedirs(os.path.join(gr, "tests"))
        with open(os.path.join(gr, "tests", "test_a.py"), "w", encoding="utf-8") as f:
            f.write("def test_a(): pass\n")                     # untracked: still a changed path
        c = gpre("d2")
        check("given.repo.diff_paths_none_rx: an UNTRACKED test file satisfies the rule",
              "[pr-needs-test]" not in c, c)
        _git(gr, "add", ".")
        _git(gr, "commit", "-qm", "feat")
        c = gpre("d3")
        check("given.repo: committed changes still count against the merge-base",
              "[pr-too-big]" in c and "[pr-needs-test]" not in c, c)
        c = gpre("d3r", "gh pr ready")
        check("given.repo.dirty: a clean tree is not dirty", "[pr-dirty]" not in c, c)


def bash_edit_checks() -> None:
    """A Bash call that writes files is an edit. The post lane reads what the
    command left on disk since the pre lane stamped it and feeds each file
    through the edit matcher and the ordering engine as a Write — so a
    `cat > f <<EOF`, a `python - <<PY … write_text()` and a `sed -i` reach
    an `event: edit` rule, which until 0.43 only the Edit/Write tools did
    (and in auto mode the model is told not to use them)."""
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "bashrepo")
        os.makedirs(os.path.join(repo, "alembic", "versions"))
        os.makedirs(os.path.join(repo, "src"))
        assert _git(repo, "-c", "init.defaultBranch=main", "init", "-q").returncode == 0
        with open(os.path.join(repo, "src", "old.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(".venv/\n")
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
        seed_book(td, "bashrepo", [
            _row("new-table-retention", {"event": "edit", "path_rx": r"alembic/versions/[^/]*\.py$",
                                         "content_rx": r"create_table\("}),
            _row("no-bare-ignore", {"event": "edit", "path_rx": r"\.py$",
                                    "content_rx": r"type:\s*ignore(?!\[)"}),
            {"id": "tests-before-push", "on": "ordering", "repo_scope": "any",
             "ordering": {"required_command_rx": r"pytest", "gated_command_rx": r"git\s+push",
                          "armed_by_events": ["edit", "write"], "min_edits": 1,
                          "display_name": "the suite"},
             "text": "Run the suite before pushing", "why": "w"},
        ])
        env = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        n = [0]

        def write_file(path, text, *, append=False):
            target = path if os.path.isabs(path) else os.path.join(repo, path)
            with open(target, "a" if append else "w", encoding="utf-8") as f:
                f.write(text)

        def replace_file(path, old, new):
            target = path if os.path.isabs(path) else os.path.join(repo, path)
            with open(target, encoding="utf-8") as f:
                text = f.read()
            with open(target, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))

        def bash(command, *, session="b1", run_it=True, pre=True, resp=None, mutate=None):
            """pre → apply the command's file effect → post, the way a session does.

            The hook receives the real shell syntax under test. Applying its
            expected file effect in Python keeps this fixture independent of
            whether the test host's ``shell=True`` means sh or cmd.exe.
            """
            n[0] += 1
            tid = f"tu{n[0]}"
            ev = {"cwd": repo, "session_id": session, "tool_name": "Bash", "tool_use_id": tid,
                  "tool_input": {"command": command}}
            if pre:
                run("pre", ev, env)
            if run_it:
                if mutate is not None:
                    mutate()
                    resp = resp or {"stdout": "", "stderr": "", "exit_code": 0}
                else:
                    r = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True)
                    resp = resp or {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}
            return ctx(run("post", dict(ev, tool_response=resp or {"stdout": "", "exit_code": 0}), env)[1])

        mig = "alembic/versions/20260902_user_logins.py"
        c = bash(f"cat > {mig} <<'EOF'\ndef upgrade():\n    op.create_table('user_logins')\nEOF",
                 mutate=lambda: write_file(mig, "def upgrade():\n    op.create_table('user_logins')\n"))
        check("bash-edit: a heredoc-created migration reaches the edit rule",
              "[new-table-retention]" in c, c)
        check("bash-edit: the fire names the file the command wrote", mig in c, c)

        c = bash("python3 - <<'PY'\nimport pathlib\np = pathlib.Path('src/old.py')\n"
                 "p.write_text(p.read_text() + 'y = f()  # type: ignore\\n')\nPY",
                 mutate=lambda: write_file("src/old.py", "y = f()  # type: ignore\n", append=True))
        check("bash-edit: a python write_text() edit reaches the edit rule",
              "[no-bare-ignore]" in c, c)

        c = bash("sed -i.bak 's/ignore/ignore[x]/' src/old.py && rm -f src/old.py.bak", session="b2",
                 mutate=lambda: replace_file("src/old.py", "ignore", "ignore[x]"))
        check("bash-edit: sed -i is an edit too — and the complied-with form does not fire",
              "[no-bare-ignore]" not in c and "[new-table-retention]" not in c, c)
        c = bash("sed -i.bak 's/ignore\\[x\\]/ignore/' src/old.py && rm -f src/old.py.bak", session="b2",
                 mutate=lambda: replace_file("src/old.py", "ignore[x]", "ignore"))
        check("bash-edit: sed -i that reintroduces the pattern fires", "[no-bare-ignore]" in c, c)
        # git decides what is a candidate, so a write that leaves the file byte-identical
        # to HEAD is not an edit — nothing changed, nothing to advise on
        _git(repo, "commit", "-qam", "committed bare ignore")
        c = bash("sed -i.bak 's/ignore/ignore[x]/' src/old.py && sed -i.bak 's/ignore\\[x\\]/ignore/' src/old.py && rm -f src/old.py.bak",
                 session="b2b", mutate=lambda: (
                     replace_file("src/old.py", "ignore", "ignore[x]"),
                     replace_file("src/old.py", "ignore[x]", "ignore")))
        check("bash-edit: a write that leaves the file identical to HEAD is not an edit", c.strip() == "", c)

        # a modified file is read as its ADDED lines, a new file whole — what the
        # Edit and Write tools hand the matcher respectively
        with open(os.path.join(repo, "src", "big.py"), "w", encoding="utf-8") as f:
            f.write("a = 1  # type: ignore\nb = 2\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "big has a pre-existing hit")
        c = bash("printf 'c = 3\\n' >> src/big.py", session="b2c",
                 mutate=lambda: write_file("src/big.py", "c = 3\n", append=True))
        check("bash-edit: a modified file is read as its added lines — a pre-existing hit does not fire",
              "[no-bare-ignore]" not in c, c)
        c = bash("printf 'd = 4  # type: ignore\\n' >> src/big.py", session="b2c",
                 mutate=lambda: write_file("src/big.py", "d = 4  # type: ignore\n", append=True))
        check("bash-edit: an added line that hits does fire", "[no-bare-ignore]" in c, c)
        c = bash("printf 'e = 5  # type: ignore\\n' > src/fresh.py", session="b2d",
                 mutate=lambda: write_file("src/fresh.py", "e = 5  # type: ignore\n"))
        check("bash-edit: a new file is read whole", "[no-bare-ignore]" in c, c)

        c = bash("echo hello && ls src", session="b3", mutate=lambda: None)
        check("bash-edit: a command that wrote nothing is silent", c.strip() == "", c)

        c = bash(f"cat > {mig} <<'EOF'\ndef upgrade():\n    op.create_table('again')\nEOF",
                 mutate=lambda: write_file(mig, "def upgrade():\n    op.create_table('again')\n"))
        check("bash-edit: the second write of a session-scoped rule is deduped like a Write",
              "[new-table-retention]" not in c, c)

        # no pre stamp (a host that only wires the post lane) → silent, never a crash
        c = bash("printf 'z = 1  # type: ignore\\n' > src/nopre.py", session="b4", pre=False,
                 mutate=lambda: write_file("src/nopre.py", "z = 1  # type: ignore\n"))
        check("bash-edit: without a pre stamp the post lane reads nothing", c.strip() == "", c)

        # a tree rewrite touches files nobody edited
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "wip")
        _git(repo, "checkout", "-qb", "other")
        with open(os.path.join(repo, "src", "theirs.py"), "w", encoding="utf-8") as f:
            f.write("q = 1  # type: ignore\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "theirs")
        _git(repo, "checkout", "-q", "main")
        c = bash("git checkout -q other", session="b5",
                 mutate=lambda: _git(repo, "checkout", "-q", "other"))
        check("bash-edit: a checkout is not an edit (files change, nobody wrote them)",
              "[no-bare-ignore]" not in c, c)

        # ordering: a Bash-written file arms the obligation, like a Write would
        wt2 = os.path.join(td, "orepo")
        os.makedirs(os.path.join(wt2, "src"))
        assert _git(wt2, "-c", "init.defaultBranch=main", "init", "-q").returncode == 0
        _git(wt2, "commit", "-q", "--allow-empty", "-m", "base")
        seed_book(td, "orepo", [
            {"id": "tests-before-push", "on": "ordering", "repo_scope": "any",
             "ordering": {"required_command_rx": r"pytest", "gated_command_rx": r"git\s+push",
                          "armed_by_events": ["edit", "write"], "min_edits": 1,
                          "display_name": "the suite"},
             "text": "Run the suite before pushing", "why": "w"}])
        push = {"cwd": wt2, "session_id": "o1", "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"}}
        rc, out = run("pre", push, env)
        check("bash-edit ordering: nothing armed → push silent", out.strip() == "", out)
        ev = {"cwd": wt2, "session_id": "o1", "tool_name": "Bash", "tool_use_id": "w1",
              "tool_input": {"command": "cat > src/gate.py <<'EOF'\nx=1\nEOF"}}
        run("pre", ev, env)
        with open(os.path.join(wt2, "src", "gate.py"), "w", encoding="utf-8") as f:
            f.write("x=1\n")
        run("post", dict(ev, tool_response={"stdout": "", "exit_code": 0}), env)
        rc, out = run("pre", push, env)
        check("bash-edit ordering: a heredoc write arms the obligation and the gate names the file",
              "[tests-before-push]" in ctx(out) and "gate.py" in ctx(out), ctx(out))
        ev2 = {"cwd": wt2, "session_id": "o1", "tool_name": "Bash", "tool_use_id": "w2",
               "tool_input": {"command": "printf 'y=2\\n' > src/gate.py && pytest -q"}}
        run("pre", ev2, env)
        with open(os.path.join(wt2, "src", "gate.py"), "w", encoding="utf-8") as f:
            f.write("y=2\n")
        run("post", dict(ev2, tool_response={"stdout": "1 passed", "exit_code": 0}), env)
        rc, out = run("pre", push, env)
        check("bash-edit ordering: edit-then-green-receipt in ONE call discharges (edits are read first)",
              out.strip() == "", ctx(out))

        # a sibling worktree named in the command is scanned; one that is not, is not
        sib = os.path.join(td, "sibling-wt")
        assert _git(repo, "worktree", "add", "-q", "-b", "sib", sib).returncode == 0
        c = bash(f"cat > {sib}/{mig} <<'EOF'\ndef upgrade():\n    op.create_table('t')\nEOF",
                 session="b6", mutate=lambda: write_file(
                     os.path.join(sib, mig), "def upgrade():\n    op.create_table('t')\n"))
        check("bash-edit: a write into a sibling worktree the command names is seen",
              "[new-table-retention]" in c, c)


def diff_base_checks() -> None:
    """What the diff probes measure: WHICH tree, and against WHICH base.

    Both were guesses once, and both guessed wrong in the same session: the
    base was `origin/main` in a repo whose PRs target `staging` (so a 260-line
    PR measured the whole staging-vs-main delta), and the tree was the
    session's cwd while the command ran in another worktree. Either one alone
    makes `diff_lines_gt` fire on every PR.
    """
    sys.path.insert(0, os.path.dirname(HOOK))
    import rulebook_hook as H  # noqa: E402

    def git(root, *args):
        subprocess.run(["git", "-C", root, *args], check=True,
                       capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as td:
        # A repo whose long-lived base is `staging`, far ahead of `main`.
        repo = os.path.join(td, "svc")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t.t")
        git(repo, "config", "user.name", "t")
        with open(os.path.join(repo, "seed.txt"), "w") as f:
            f.write("seed\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "seed")
        git(repo, "checkout", "-qb", "staging")
        with open(os.path.join(repo, "big.txt"), "w") as f:
            f.write("".join(f"line {i}\n" for i in range(900)))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "900 lines of staging")
        git(repo, "checkout", "-qb", "feature")
        with open(os.path.join(repo, "small.txt"), "w") as f:
            f.write("a\nb\nc\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "3 lines")
        # A named base must be a real REMOTE branch, so the fixture has remotes.
        for br in ("main", "staging", "feature"):
            sha = subprocess.run(["git", "-C", repo, "rev-parse", br],
                                 capture_output=True, text=True).stdout.strip()
            git(repo, "update-ref", f"refs/remotes/origin/{br}", sha)

        def lines(command):
            return H.Probes(repo, "feature", command=command).diff_lines()

        check("diff base: without the command's --base, the guess is `main` — "
              "the branch measures the whole staging-vs-main delta",
              lines("gh pr create") > 500, str(lines("gh pr create")))
        n = lines("gh pr create --base staging --title x")
        check("diff base: `--base staging` measures the branch, not the base branch",
              n == 3, str(n))
        check("diff base: `--base=staging` (equals form) reads the same",
              lines("gh pr create --base=staging") == 3)
        check("diff base: a quoted base reads the same",
              lines("gh pr create --base 'staging'") == 3)

        # MEMHUB_RULEBOOK_BASE_BRANCH still wins over the command.
        os.environ["MEMHUB_RULEBOOK_BASE_BRANCH"] = "main"
        try:
            check("diff base: the env override still outranks the command",
                  lines("gh pr create --base staging") > 500)
        finally:
            del os.environ["MEMHUB_RULEBOOK_BASE_BRANCH"]

        # --- a named base is CHECKED, because the gated party writes it ------
        # Every refusal falls through to the remote default, which OVER-measures:
        # the safe direction for a size gate.
        check("named base: your own branch is refused — `merge-base(base, HEAD) == HEAD` "
              "measures zero, and a PR onto it would be empty",
              lines("gh pr create --base feature") > 500, str(lines("gh pr create --base feature")))
        for rev, why in [("HEAD", "rev, not a branch"),
                         ("staging~1", "rev arithmetic reaches elsewhere in history"),
                         ("../../etc/passwd", "path traversal"),
                         ("-oProxyCommand=x", "leading dash")]:
            check(f"named base: `{rev}` is refused ({why})",
                  lines(f"gh pr create --base {rev}") > 500)
        check("named base: a branch that is not on the remote is refused",
              lines("gh pr create --base no-such-branch") > 500)
        check("named base: two different bases in one command are refused — "
              "`--base <mine> || --base staging` would probe one and open the other",
              lines("gh pr create --base staging || gh pr create --base feature") > 500)
        check("named base: the same base named twice is still honoured",
              lines("gh pr create --base staging --base staging") == 3)

        # --- which tree: a leading `cd` redirects the whole command ----------
        other = os.path.join(td, "other")
        os.makedirs(other)
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.email", "t@t.t")
        git(other, "config", "user.name", "t")
        with open(os.path.join(other, "a.txt"), "w") as f:
            f.write("a\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "seed")

        check("probe root: a leading `cd <repo> &&` resolves to that worktree",
              H.command_root(td, f"cd {other} && gh pr create") == other)
        check("probe root: a relative `cd` resolves against the session cwd",
              H.command_root(td, "cd other && gh pr create") == other)
        check("probe root: a quoted path resolves",
              H.command_root(td, f"cd '{other}' ; gh pr create") == other)
        check("probe root: only a Bash call redirects it — another tool's input may hold a "
              "field called `command` meaning something else, and reading it as shell would "
              "point the probes at a tree the call never touches",
              H.command_root(td, "") == "")
        check("probe root: no `cd` keeps the session's tree",
              H.command_root(td, "gh pr create") == "")
        check("probe root: a `cd` buried mid-pipeline is not honoured — the "
              "command may never reach it",
              H.command_root(td, f"ls && cd {other} && gh pr create") == "")
        check("probe root: a `cd` to somewhere that is not a repo keeps the session's tree",
              H.command_root(td, f"cd {td} && gh pr create") == "")
        check("probe root: a `cd` to a missing directory keeps the session's tree",
              H.command_root(td, "cd /nope/nowhere && gh pr create") == "")


def main() -> int:
    portability_check()
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "xmem")           # fake git repo named xmem
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/test-branch\n")
        other = os.path.join(td, "otherrepo")
        os.makedirs(os.path.join(other, ".git"))

        rules = {"version": 1, "rules": [
            {"id": "posture-one", "on": "session", "fire_scope": "session",
             "repo_scope": "xmem", "text": "Posture text", "why": "posture why"},
            {"id": "bash-rule", "on": "bash", "rx": r"forbidden-cmd",
             "not_rx": r"allowed-context", "fire_scope": "session",
             "repo_scope": "any", "text": "Bash advisory", "why": "w"},
            {"id": "xmem-only", "on": "bash", "rx": r"xmem-only-cmd",
             "fire_scope": "session", "repo_scope": "xmem",
             "text": "Xmem advisory", "why": "w"},
            {"id": "post-rule", "on": "result", "rx": r"BOOM-ERROR",
             "cmd_rx": r"pytest", "fire_scope": "session", "repo_scope": "any",
             "text": "Post advisory", "why": "w"},
            {"id": "draft-rule", "on": "bash", "rx": r"forbidden-cmd", "status": "draft",
             "fire_scope": "session", "repo_scope": "any", "text": "DRAFT TEXT", "why": "w"},
        ]}
        for r in rules["rules"]:
            r["version"] = 1
        seed_book(td, "xmem", rules["rules"])
        seed_book(td, "otherrepo", rules["rules"])
        # FETCH=0: the session lane otherwise spawns a DETACHED network fetch
        # that races the test and overwrites the seeded book with an empty one.
        env = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}

        # --- fail-open properties -----------------------------------------
        rc, out = run("pre", {"cwd": "/", "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}}, env)
        check("non-git cwd is silent", rc == 0 and out.strip() == "")

        rc, out = run("session", {"cwd": repo},
                      {"MEMHUB_RULEBOOK_BASE": os.path.join(td, "empty-base")})
        check("no cached book is silent exit-0", rc == 0 and out.strip() == "")

        badbase = os.path.join(td, "bad-base")
        seed_book(badbase, "xmem", [])
        with open(os.path.join(badbase, "book", os.listdir(os.path.join(badbase, "book"))[0]), "w", encoding="utf-8") as f:
            f.write("{not json")
        rc, out = run("pre", {"cwd": repo, "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}},
                      {"MEMHUB_RULEBOOK_BASE": badbase})
        check("corrupt cached book is silent exit-0", rc == 0 and out.strip() == "")

        p = subprocess.run([sys.executable, HOOK, "pre"], input="}}garbage",
                           capture_output=True, text=True,
                           env=dict(os.environ, **env), timeout=30)
        check("garbage stdin is silent exit-0",
              p.returncode == 0 and p.stdout.strip() == "")

        # --- session lane --------------------------------------------------
        rc, out = run("session", {"cwd": repo, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: posture rule served in full", "Posture text" in c)
        check("session: active rules -> index line, not full text",
              "3 rules armed" in c and "Bash advisory" not in c, c)

        rc, out = run("session", {"cwd": other, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: repo_scope filters posture + count",
              "Posture text" not in c and "2 rules armed" in c, c)

        # --- pre lane ------------------------------------------------------
        base = {"cwd": repo, "session_id": "s2", "tool_name": "Bash"}
        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: bash rule fires", "[bash-rule]" in ctx(out))
        check("pre: a status=draft rule never fires (not activated = unarmed)", "[draft-rule]" not in ctx(out))

        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: fire_scope=session dedupes the second call", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s3",
                                  tool_input={"command": "forbidden-cmd in allowed-context"}), env)
        check("pre: not_rx exempts", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s4",
                                  tool_input={"command": "cat > f <<'EOF'\nforbidden-cmd\nEOF"}), env)
        check("pre: heredoc body is not matched by default", out.strip() == "")

        rc, out = run("pre", {"cwd": other, "session_id": "s5", "tool_name": "Bash",
                              "tool_input": {"command": "xmem-only-cmd"}}, env)
        check("pre: repo_scope=xmem stays silent in another repo", out.strip() == "")

        # --- post lane -----------------------------------------------------
        rc, out = run("post", dict(base, session_id="s6",
                                   tool_input={"command": "uv run pytest tests/"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: result rule fires on failing output", "[post-rule]" in ctx(out))

        rc, out = run("post", dict(base, session_id="s7",
                                   tool_input={"command": "ls"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: cmd_rx gates the result rule", out.strip() == "")

        # --- ledger --------------------------------------------------------
        ledger = os.path.join(td, "ledger", "fires.jsonl")
        check("ledger written beside the relocated rulebook", os.path.isfile(ledger))
        if os.path.isfile(ledger):
            with open(ledger, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
            ids = [r["rule_id"] for r in rows]
            check("ledger v2: one row per rule, rule_id column",
                  "bash-rule" in ids and "post-rule" in ids and all("rules" not in r for r in rows))
            check("ledger v2: client-minted fire_id, unique",
                  len({r["fire_id"] for r in rows}) == len(rows) and all(len(r["fire_id"]) == 36 for r in rows))
            check("ledger v2: hook_phase and mode are separate columns",
                  {r["hook_phase"] for r in rows} >= {"pre", "post", "session"} and
                  all(r["mode"] == "advise" for r in rows))
            check("ledger v2: full session_id, rule_version, tz-aware fired_at",
                  all(r["session_id"] in ("s1", "s2", "s6") for r in rows) and
                  all(r["rule_version"] == 1 for r in rows) and
                  all(re.search(r"([+-]\d\d:\d\d|Z)$", r["fired_at"]) for r in rows))
            check("ledger v2: schema_version file stamped",
                  open(os.path.join(td, "ledger", "schema_version"), encoding="utf-8").read().strip() == "2")

        # --- session lane: spec cap (15 / ~2k tokens), deterministic, logged ---
        seed_book(td, "capsrepo", [
            {"id": f"post-{i:02d}", "on": "session", "repo_scope": "any", "text": f"POSTURE {i:02d}", "why": "w", "title": f"Posture {i:02d}"}
            for i in range(17)] + [
            {"id": "post-big", "on": "session", "repo_scope": "any", "text": "BIG " * 3000, "why": "w", "title": "Posture 00 big"}])
        caps = os.path.join(td, "capsrepo"); os.makedirs(os.path.join(caps, ".git"))
        rc, out = run("session", {"cwd": caps, "session_id": "cap1"}, env)
        shown = [i for i in range(17) if f"POSTURE {i:02d}" in ctx(out)]
        check("session: at most 15 posture rules, chosen by title", shown == list(range(15)), str(shown))
        check("session: a rule that would blow the ~2k-token budget is not served", "BIG BIG" not in ctx(out))
        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            srows = [json.loads(l) for l in f if '"cap1"' in l]
        sup = sorted(r["rule_id"] for r in srows if r["mode"] == "suppressed")
        check("session: every rule past the cap or budget is logged suppressed with a session dedup key",
              sup == ["post-15", "post-16", "post-big"] and all(r["dedup_key"] == r["rule_id"] + "@session" for r in srows if r["mode"] == "suppressed"), str(sup))

        # --- cap → suppressed rows; converted_rx → conversions sidecar --------
        seed_book(td, "xmem", rules["rules"] + [
                {"id": f"cap-{i}", "on": "bash", "rx": r"capcmd", "fire_scope": "session",
                 "repo_scope": "any", "text": f"cap {i}", "why": "w",
                 **({"converted_rx": r"do-the-thing"} if i == 0 else {})}
                for i in range(3)])
        cenv = env
        cb = {"cwd": repo, "session_id": "c1", "tool_name": "Bash"}
        rc, out = run("pre", dict(cb, tool_input={"command": "capcmd"}), cenv)
        check("cap: at most MAX_ADVISE rules shown", ctx(out).count("[cap-") == 2)
        run("pre", dict(cb, tool_input={"command": "capcmd again"}), cenv)   # deduped → raw count
        run("post", dict(cb, tool_input={"command": "now do-the-thing"},
                         tool_response={"stdout": "ok"}), cenv)
        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip() and '"cap-' in l]
        modes = {r["rule_id"]: r["mode"] for r in rows}
        check("cap: the cut rule is logged mode=suppressed, never silently dropped",
              modes == {"cap-0": "advise", "cap-1": "advise", "cap-2": "suppressed"}, str(modes))
        check("cap: raw_matches_before_fire recorded", all(r["raw_matches_before_fire"] == 1 for r in rows))
        conv = os.path.join(td, "ledger", "conversions.jsonl")
        with open(conv, encoding="utf-8") as f:
            convs = [json.loads(l) for l in f if l.strip()]
        fid0 = next(r["fire_id"] for r in rows if r["rule_id"] == "cap-0")
        check("converted_rx: follow-up command writes a conversion for that fire_id",
              [c["fire_id"] for c in convs] == [fid0] and convs[0]["how"] == "converted_rx", str(convs))

        # --- shell_only + evaluate(): the pure engine ---
        sys.path.insert(0, os.path.dirname(HOOK))
        import rulebook_hook as H  # noqa: E402
        so = H.shell_only
        check("shell_only: plain command unchanged", so("git push origin main") == "git push origin main")
        check("shell_only: heredoc body dropped",
              "forbidden" not in so("cat > f <<'EOF'\nforbidden\nEOF"))
        chained = "git commit -F - <<'MSG'\nfix: forbidden\nMSG\ngit push -u origin fm"
        check("shell_only: shell AFTER a heredoc terminator is kept (the 44% FN class)",
              "git push -u origin fm" in so(chained) and "fix: forbidden" not in so(chained))
        check("shell_only: unquoted and <<- delimiters", "secret" not in so("cat <<-EOF\nsecret\nEOF\nls"))
        check("shell_only: a numeric bit-shift is not a heredoc", so("x=$((1 << 2))\ngit push") == "x=$((1 << 2))\ngit push")
        push = {"on": "bash", "rx": r"git\s+push"}
        check("evaluate: push after heredoc fires",
              H.evaluate(push, hook_phase="pre", tool="Bash", cmd=chained))
        check("evaluate: push only inside a body does not fire",
              not H.evaluate(push, hook_phase="pre", tool="Bash",
                             cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        check("evaluate: match_heredoc_body opts in",
              H.evaluate(dict(push, match_heredoc_body=True), hook_phase="pre",
                         tool="Bash", cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        hd = {"on": "bash", "rx": r"python3?\s+-?\s*<<", "match_heredoc_body": True, "body_rx": r"results\.json"}
        check("evaluate: body_rx rule — rx on the shell line, body_rx on the payload",
              H.evaluate(hd, hook_phase="pre", tool="Bash", cmd="python3 - <<'PY'\nload('results.json')\nPY")
              and not H.evaluate(hd, hook_phase="pre", tool="Bash", cmd="python3 - <<'PY'\nprint(1)\nPY")
              and not H.evaluate(hd, hook_phase="pre", tool="Bash",
                                 cmd="cat > spec.md <<'MD'\nuse python3 - << for results.json\nMD"))
        ws = {"on": "write_stdlib", "min_chars": 10, "path_not_rx": r"/tests?/"}
        check("evaluate: write_stdlib honours path_not_rx",
              H.evaluate(ws, hook_phase="pre", tool="Write", file_path="/r/pkg/m.py", body="import os\nimport re\nx=1")
              and not H.evaluate(ws, hook_phase="pre", tool="Write", file_path="/r/tests/t.py", body="import os\nimport re\nx=1"))
        check("bash_ok: exit_code wins; None is never ok; 'error:' in green output is ok",
              H.bash_ok({"exit_code": 0, "stdout": "3 failed earlier but fixed"}) and not H.bash_ok({"exit_code": 1})
              and not H.bash_ok(None) and H.bash_ok({"stdout": "warning: error: handled gracefully\n5 passed"})
              and not H.bash_ok({"stdout": "== 2 failed, 3 passed =="})
              and not H.bash_ok({"stdout": "collected 0 items / 1 error"})
              and not H.bash_ok({"stdout": "npm ERR! code ELIFECYCLE"})
              and not H.bash_ok({"stdout": "error[E0308]: mismatched types"}))
        check("bash_ok strict: a gate-mode receipt needs an explicit exit_code (text cannot forge green)",
              not H.bash_ok({"stdout": "5 passed"}, strict=True) and H.bash_ok({"stdout": "5 passed", "exit_code": 0}, strict=True))
        check("last_segment: receipt only counts as the final unpiped segment",
              H.last_segment("cd x && uv run pytest tests/architecture -q") == "uv run pytest tests/architecture -q"
              and H.last_segment("pytest tests/architecture; git push") == "git push")
        check("evaluate: broken regex is False, never raises",
              H.evaluate({"on": "bash", "rx": "("}, hook_phase="pre", tool="Bash", cmd="x") is False)

        # --- ordering engine: arm / receipt / gate, keyed by worktree ---------
        seed_book(td, "wtrepo", [
                {"id": "audit-before-push", "on": "ordering", "repo_scope": "any",
                 "ordering": {"required_command_rx": r"pytest\s+\S*tests/architecture",
                              "gated_command_rx": r"git\s+push",
                              "armed_by_events": ["edit", "write"],
                              "min_edits": 1, "display_name": "the architecture suite"},
                 "text": "Run the architecture suite before pushing", "why": "w"}])
        oenv = env
        wt = os.path.join(td, "wtrepo")
        os.makedirs(os.path.join(wt, ".git"))
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/feat\n")
        edit_ev = {"cwd": wt, "session_id": "o1", "tool_name": "Edit",
                   "tool_input": {"file_path": os.path.join(wt, "pkg", "gate.py")}}
        suite = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                 "tool_input": {"command": "uv run pytest tests/architecture -q"}}
        pushev = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                  "tool_input": {"command": "git push -u origin feat"}}

        rc, out = run("pre", pushev, oenv)
        check("ordering: push with nothing armed → silent", out.strip() == "")
        run("post", edit_ev, oenv)                                   # arm
        rc, out = run("pre", pushev, oenv)
        check("ordering: push while armed → fires and names the file",
              "[audit-before-push]" in ctx(out) and "gate.py" in ctx(out), ctx(out))
        run("post", dict(suite, tool_response={"stdout": "1 failed", "exit_code": 1}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: RED suite run is not a receipt", "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q; echo done"},
                         tool_response={"stdout": "3 passed\ndone", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a receipt that is NOT the last segment does not discharge (exit status isn't its own)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q | tail -3"},
                         tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a PIPED receipt does not discharge (pipe masks the status)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: green run discharges → push allowed", out.strip() == "")
        run("post", edit_ev, oenv)                                   # re-arm
        rc, out = run("pre", dict(pushev, session_id="o2-sibling"), oenv)
        check("ordering: state is per worktree, not per session (sibling sees the arm)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, session_id="subagent-9",
                         tool_response={"stdout": "ok", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a SUBAGENT's green receipt discharges the parent's obligation",
              out.strip() == "")
        run("post", edit_ev, oenv)
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/other\n")
        rc, out = run("pre", pushev, oenv)
        check("ordering: obligation survives `git checkout -b` (keyed by worktree, not branch)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q &"},
                         tool_response={"stdout": "", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a BACKGROUNDED receipt does not discharge", "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        oledger = os.path.join(td, "ledger", "fires.jsonl")
        with open(os.path.join(td, "ledger", "conversions.jsonl"), encoding="utf-8") as f:
            hows = [json.loads(l)["how"] for l in f if l.strip()]
        check("ordering: a discharge after a fire converts that fire (the conversion signal)",
              hows.count("discharged") == 3, str(hows))
        statefiles = [n for n in os.listdir(os.path.join(td, "state")) if n.startswith("wt-") and n.endswith(".json")]
        check("ordering: one state file per worktree, atomic (no temp leftovers)",
              len(statefiles) == 1 and not any(n.startswith(".wt-") for n in os.listdir(os.path.join(td, "state"))))

    # --- message_id_of: the fire's link back to the transcript record ---
    sys.path.insert(0, os.path.dirname(HOOK))
    import rulebook_hook as rb  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        tp = os.path.join(td, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"}) + "\n")
        check("message_id_of: the LAST message record's uuid",
              rb.message_id_of({"transcript_path": tp}) == "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
        check("message_id_of: no transcript_path -> None",
              rb.message_id_of({}) is None)
        check("message_id_of: missing file -> None (never raises)",
              rb.message_id_of({"transcript_path": os.path.join(td, "nope.jsonl")}) is None)
        with open(tp, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        check("message_id_of: skips an unparseable tail line",
              rb.message_id_of({"transcript_path": tp}) == "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
        big = os.path.join(td, "big.jsonl")
        with open(big, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "cccccccc-3333-4333-8333-cccccccccccc", "pad": "x" * 200000}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "dddddddd-4444-4444-8444-dddddddddddd"}) + "\n")
        check("message_id_of: reads only the tail of a large transcript",
              rb.message_id_of({"transcript_path": big}) == "dddddddd-4444-4444-8444-dddddddddddd")

    # A transcript interleaves non-message records that carry their own uuid —
    # `attachment` outnumbers real messages in a long session. Picking one of
    # those links the fire to something that is not a message.
    with tempfile.TemporaryDirectory() as td:
        tp = os.path.join(td, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"}) + "\n")
            f.write(json.dumps({"type": "attachment", "uuid": "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"}) + "\n")
            f.write(json.dumps({"type": "file-history-snapshot", "uuid": "ffffffff-6666-4666-8666-ffffffffffff"}) + "\n")
        check("message_id_of: skips attachment / meta records that carry a uuid",
              rb.message_id_of({"transcript_path": tp}) == "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")

        # A single record can exceed the initial tail window; the read grows
        # rather than returning nothing.
        huge = os.path.join(td, "huge.jsonl")
        with open(huge, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "99999999-7777-4777-8777-999999999999"}) + "\n")
            f.write(json.dumps({"type": "attachment", "uuid": "88888888-8888-4888-8888-888888888888",
                                "pad": "x" * 300000}) + "\n")
        check("message_id_of: grows the window past a >64 KiB record",
              rb.message_id_of({"transcript_path": huge}) == "99999999-7777-4777-8777-999999999999")

        none_f = os.path.join(td, "none.jsonl")
        with open(none_f, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "attachment", "uuid": "77777777-9999-4999-8999-777777777777"}) + "\n")
        check("message_id_of: no message record anywhere -> None",
              rb.message_id_of({"transcript_path": none_f}) is None)

        # The growing window is bounded: `window >= _TAIL_MAX` is checked
        # BEFORE the multiply, so the read stops at 1 MiB (64K -> 256K -> 1M)
        # for a file of any size. A hook on a 5 s budget must never walk a
        # multi-megabyte transcript.
        def _windows(end):
            w, out = rb._TAIL_START, []
            while True:
                out.append(w)
                if max(0, end - w) == 0 or w >= rb._TAIL_MAX:
                    return out
                w *= 4
        check("message_id_of: at most 3 windows, capped at _TAIL_MAX, for any file size",
              all(len(_windows(n)) <= 3 and max(_windows(n)) <= rb._TAIL_MAX
                  for n in (300_000, 5_400_000, 50_000_000, 5_000_000_000)))

        early = os.path.join(td, "early.jsonl")
        with open(early, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "msg-at-the-very-start"}) + "\n")
            for i in range(40):
                f.write(json.dumps({"type": "attachment", "uuid": "a%d" % i, "pad": "x" * 90000}) + "\n")
        t0 = time.time()
        got = rb.message_id_of({"transcript_path": early})
        check("message_id_of: a >1 MiB file whose only message is at the start "
              "gives up quickly rather than reading it all",
              got is None and (time.time() - t0) < 1.0)

    # --- what the recall lane sends ---------------------------------------
    # /recall is the one lane that carries content: the relevance judge needs
    # the call itself. A command line is also where credentials live, and they
    # are worth nothing to the judge.
    for label, cmd in [
        ("Authorization header", 'curl -H "Authorization: Bearer sk-live-abcdefghij1234567890" https://x'),
        ("credentials in a URL", 'psql "postgres://admin:hunter2@db.internal/prod"'),
        ("--flag=value", "gh auth login --with-token=ghp_AAAABBBBCCCCDDDDEEEEFFFF1111"),
        ("KEY value (space form)", "aws configure set aws_secret_access_key AKIAIOSFODNN7EXAMPLE"),
        ("KEY=value", "export API_KEY=super-secret-value && ./deploy.sh"),
        ("curl -u user:pass", "curl -u user:p4ssw0rd https://x.com"),
        ("a JWT", 'echo "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijk"'),
        ("our own access key", 'curl -H "x: mhk_AbCdEf123456789" https://x'),
    ]:
        out = rb.redact_secrets(cmd)
        check("recall redacts: %s" % label,
              "<redacted>" in out and "hunter2" not in out and "p4ssw0rd" not in out
              and "super-secret-value" not in out and "ghp_AAAABBBBCCCCDDDDEEEEFFFF1111" not in out
              and "AKIAIOSFODNN7EXAMPLE" not in out, out)

    # Over-redaction is not free: it costs the judge the verb of the command.
    for cmd in ["git push --force origin main", "gh auth login", "kubectl get secrets",
                "npm run build -- --token-budget 500", "pytest tests/ -k rulebook",
                "ls -la ~/.ssh"]:
        check("recall leaves an innocent command intact: %s" % cmd[:34],
              rb.redact_secrets(cmd) == cmd, rb.redact_secrets(cmd))

    check("redaction never raises on empty or None",
          rb.redact_secrets("") == "" and rb.redact_secrets(None) is None)

    check("WIRE_KEYS carries source_message_id (server links the fire to its message)",
          "source_message_id" in rb.WIRE_KEYS)
    check("WIRE_KEYS carries override_reason (a gate override is a fact about the fire)",
          "override_reason" in rb.WIRE_KEYS)

    # --- delivery: the user sees a fire; a gate blocks (§5.3) ------------------
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "gaterepo")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        seed_book(td, "gaterepo", [
            {"id": "adv", "on": "bash", "rx": r"advisory-cmd", "fire_scope": "session",
             "repo_scope": "any", "text": "Advisory text", "why": "w", "version": 1},
            {"id": "no-force-push", "on": "bash", "rx": r"git\s+push\s+--force", "mode": "gate",
             "fire_scope": "session", "repo_scope": "any", "text": "Never force-push", "why": "w",
             "version": 1},
            {"id": "result-gate", "on": "result", "rx": r"BOOM", "mode": "gate",
             "fire_scope": "session", "repo_scope": "any", "text": "Result rule", "why": "w",
             "version": 1},
        ])
        genv = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        base = {"cwd": repo, "session_id": "g1", "tool_name": "Bash"}

        def outj(out):
            return json.loads(out) if out.strip() else {}

        rc, out = run("pre", dict(base, tool_input={"command": "advisory-cmd"}), genv)
        j = outj(out)
        check("advisory: user sees an XTrace line naming the rule (systemMessage)",
              j.get("systemMessage", "").startswith("XTrace") and "[adv]" in j.get("systemMessage", "")
              and "Advisory text" in j["systemMessage"], out)
        check("advisory: agent context header is branded, no ruler",
              "XTrace Rulebook" in ctx(out) and "📏" not in ctx(out), ctx(out))
        check("advisory: never blocks", "permissionDecision" not in j["hookSpecificOutput"])

        push = dict(base, tool_input={"command": "git push --force origin main"})
        rc, out = run("pre", push, genv)
        j = outj(out)
        hso = j.get("hookSpecificOutput", {})
        check("gate: pre Bash call is DENIED", rc == 0 and hso.get("permissionDecision") == "deny", out)
        check("gate: deny reason carries the statement and the override line",
              "Never force-push" in hso.get("permissionDecisionReason", "")
              and "RULEBOOK_OVERRIDE=" in hso.get("permissionDecisionReason", ""), out)
        check("gate: user line says blocked, branded",
              j.get("systemMessage", "").startswith("XTrace") and "blocked" in j["systemMessage"]
              and "[no-force-push]" in j["systemMessage"], out)
        rc, out = run("pre", push, genv)
        check("gate: the SAME call is gated again — gates are never deduped",
              outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)

        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='hotfix, approved by lead' git push --force origin main"}), genv)
        j = outj(out)
        check("override: the prefixed call is ALLOWED",
              "permissionDecision" not in j.get("hookSpecificOutput", {}), out)
        check("override: user line records the override reason",
              "overridden" in j.get("systemMessage", "") and "approved by lead" in j["systemMessage"], out)

        for empty in ("RULEBOOK_OVERRIDE= git push --force origin main",
                      "RULEBOOK_OVERRIDE='' git push --force origin main",
                      "RULEBOOK_OVERRIDE='   ' git push --force origin main"):
            rc, out = run("pre", dict(base, tool_input={"command": empty}), genv)
            check("override: an EMPTY reason is not an override — still denied: %s" % empty[:24],
                  outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab expired' git push --force origin main"}), genv)
        j = outj(out)
        check("override: the reason is redacted before it is recorded or shown",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "ghp_ABCDEFGHIJ" not in json.dumps(j), out)
        for seg in ("cd /tmp && RULEBOOK_OVERRIDE='cd first' git push --force origin main",
                    "git fetch origin; RULEBOOK_OVERRIDE='after semicolon' git push --force origin main",
                    "echo a\nRULEBOOK_OVERRIDE='on line two' git push --force origin main"):
            rc, out = run("pre", dict(base, tool_input={"command": seg}), genv)
            j = outj(out)
            check("override: recognised at the start of the blocked SEGMENT, not only the command: %s" % seg[:22],
                  "permissionDecision" not in j.get("hookSpecificOutput", {})
                  and "overridden" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={"command": "echo \"RULEBOOK_OVERRIDE='x' git push --force origin main\""}), genv)
        j = outj(out)
        check("override: the variable inside a quoted ARGUMENT is not an override (the gate still denies)",
              j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
              and "overridden" not in j.get("systemMessage", ""), out)
        for quoted in ("echo 'a|RULEBOOK_OVERRIDE=x git push --force origin main'",
                       "echo \"(RULEBOOK_OVERRIDE='x' git push --force origin main)\"",
                       "printf '%s' \"x;RULEBOOK_OVERRIDE=\\\"why\\\" git push --force origin main\""):
            rc, out = run("pre", dict(base, tool_input={"command": quoted}), genv)
            j = outj(out)
            check("override: a segment boundary INSIDE quotes is data, not an override — still denied: %s" % quoted[:20],
                  j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                  and "overridden" not in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "true|RULEBOOK_OVERRIDE= x; RULEBOOK_OVERRIDE='the real one' git push --force origin main"}), genv)
        j = outj(out)
        check("override: an earlier EMPTY override does not shadow the real one",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "the real one" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "cat > note.txt <<'EOF'\nit's got an apostrophe\nEOF\nRULEBOOK_OVERRIDE='after heredoc' git push --force origin main"}), genv)
        j = outj(out)
        check("override: an apostrophe inside a HEREDOC body cannot hide a later real override",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "after heredoc" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={"command": "echo it's | RULEBOOK_OVERRIDE='x' git push --force origin main"}), genv)
        check("override: an unbalanced quote is a parse error → no override, gate stands (fail closed)",
              outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='why' git commit -m 'note about RULEBOOK_OVERRIDE=secret handling' && git push --force origin main"}), genv)
        j = outj(out)
        check("override: only the validated token is stripped — a look-alike inside a quoted argument stays",
              "permissionDecision" not in j.get("hookSpecificOutput", {}) and "[why]" not in j.get("systemMessage", "")
              and "overridden" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "git fetch origin \\\n  && RULEBOOK_OVERRIDE='cont' git push --force origin main"}), genv)
        j = outj(out)
        check("override: on a backslash-continued line it is honoured AND the token is still stripped",
              "permissionDecision" not in j.get("hookSpecificOutput", {}) and "overridden" in j.get("systemMessage", ""), out)
        heredoc_pr = ("cd /tmp && RULEBOOK_OVERRIDE='blocked its own fix' gh pr create "
                      "--base main --title \"t\" --body \"$(cat <<'EOF'\n"
                      "body\nEOF\n)\" && git push --force origin main")
        rc, out = run("pre", dict(base, tool_input={"command": heredoc_pr}), genv)
        j = outj(out)
        check("override: honoured on a `--body \"$(cat <<EOF\"` line — the quote closes on a LATER "
              "physical line, and skipping it made the gate unbypassable on the commonest gated command",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "overridden" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "gh pr create --body \"$(cat <<'EOF'\nbody\nEOF\n)\" && git push --force origin main"}), genv)
        check("override: the same command WITHOUT an override is still denied — rejoining lines "
              "reads the shell honestly, it does not open a hole",
              outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)
        rc, out = run("pre", dict(base, tool_input={"command": "grep RULEBOOK_OVERRIDE= hook.py"}), genv)
        check("override: the variable name inside an argument is not an override, and matches no gate",
              out.strip() == "", out)

        rc, out = run("post", dict(base, tool_input={"command": "git push --force origin main"},
                                   tool_response={"stdout": "BOOM", "exit_code": 1}), genv)
        j = outj(out)
        check("gate: a result (post) rule marked gate can only advise — nothing to block after the fact",
              "[result-gate]" in ctx(out) and "permissionDecision" not in j["hookSpecificOutput"], out)

        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        gate_rows = [r for r in rows if r["rule_id"] == "no-force-push"]
        check("ledger: blocked, overridden and empty-override calls are all mode=gate fires",
              len(gate_rows) == 21 and all(r["mode"] == "gate" for r in gate_rows), str(len(gate_rows)))
        reasons = [r.get("override_reason") for r in gate_rows]
        check("ledger: override_reason only on the overridden fires, secrets redacted",
              reasons[:3] == [None, None, "hotfix, approved by lead"] and reasons[3:6] == [None] * 3
              and reasons[6] and "ghp_ABCDEFGHIJ" not in reasons[6]
              and reasons[7:] == ["cd first", "after semicolon", "on line two", None,
                                  None, None, None, "the real one", "after heredoc", None, "why", "cont",
                                  "blocked its own fix", None], str(reasons))
        cont = [r for r in gate_rows if r.get("override_reason") == "cont"][0]
        check("ledger: the continued-line token was stripped before rules matched (excerpt has no assignment)",
              "RULEBOOK_OVERRIDE=" not in cont["excerpt"], cont["excerpt"])
        look_alike = [r for r in gate_rows if r.get("override_reason") == "why"][0]
        check("ledger: the excerpt rules matched kept the quoted look-alike intact and lost only the real token",
              "RULEBOOK_OVERRIDE=secret handling" in look_alike["excerpt"]
              and "RULEBOOK_OVERRIDE='why'" not in look_alike["excerpt"], look_alike["excerpt"])
        check("ledger: advisory fire stays mode=advise",
              [r["mode"] for r in rows if r["rule_id"] == "adv"] == ["advise"])
        check("ledger: override_reason crosses the wire",
              rb.wire_row(gate_rows[2]).get("override_reason") == "hotfix, approved by lead")

        # there is no freshness timer: the cached book is the book, however old.
        # A rule the server retired disappears at the next successful fetch (a
        # session refreshes its own book once it is an hour old); a stale gate
        # costs one RULEBOOK_OVERRIDE, a gate that stopped enforcing because the
        # server was unreachable for a day is the failure a gate exists to prevent.
        import datetime as _dt
        bdir = os.path.join(td, "book")
        bp = os.path.join(bdir, [n for n in os.listdir(bdir) if n.startswith("gaterepo-")][0])
        with open(bp, encoding="utf-8") as f:
            book = json.load(f)
        book["fetched_at"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat()
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(book, f)
        rc, out = run("pre", dict(push, session_id="g-stale"), dict(genv, MEMHUB_RULEBOOK_FETCH="0"))
        j = outj(out)
        check("gate: a month-old cached book still denies — no freshness timer, no degrade note",
              j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
              and "24 h" not in ctx(out), out)

    # --- an anchored rule is not bypassed by a leading env assignment -------
    #
    # `FOO=1 git push` execs `git push`: bash strips the assignment before it
    # looks up the command. A matcher that disagrees lets an ANCHORED gate
    # through with no deny AND no fire — silently, which is worse than either.
    for src, want in (("FOO=1 git push", "git push"),
                      ("FOO=1 BAR=2 git push", "git push"),       # a run goes whole
                      ("cd /tmp && FOO=1 git push", "cd /tmp && git push"),
                      ("RULEBOOK_OVERRIDE= git push", "git push"),
                      ("echo 'A=1 git push'", "echo 'A=1 git push'"),   # quoted: data
                      ("git commit -m 'A=1'", "git commit -m 'A=1'"),   # not at a start
                      ("echo 'unbalanced", "echo 'unbalanced")):        # shlex refuses
        check("normalise: %s" % src, rb.strip_leading_assignments(src) == want,
              rb.strip_leading_assignments(src))

    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "anchrepo")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        seed_book(td, "anchrepo", [
            {"id": "anchored-gate", "on": "bash", "rx": r"^git\s+push\s+--force", "mode": "gate",
             "not_rx": "SKIP_GATE=",
             "fire_scope": "session", "repo_scope": "any", "text": "Never force-push", "why": "w",
             "version": 1},
            {"id": "inline-secret", "on": "bash", "rx": r"AWS_SECRET_ACCESS_KEY=",
             "fire_scope": "session", "repo_scope": "any", "text": "No inline secret", "why": "w",
             "version": 1},
        ])
        genv = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        base = {"cwd": repo, "session_id": "a1", "tool_name": "Bash"}

        def denied(cmd):
            rc, out = run("pre", dict(base, tool_input={"command": cmd}), genv)
            return outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out

        for cmd in ("git push --force origin main",
                    "FOO=1 git push --force origin main",
                    "FOO=1 BAR=2 git push --force origin main",
                    "RULEBOOK_OVERRIDE= git push --force origin main",
                    "RULEBOOK_OVERRIDE='' git push --force origin main"):
            ok, out = denied(cmd)
            check("anchored gate: still DENIES behind a prefix — %s" % cmd[:38], ok, out)

        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='approved by lead' git push --force origin main"}), genv)
        j = outj(out)
        check("anchored gate: a real override still passes exactly that call",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "approved by lead" in j.get("systemMessage", ""), out)

        ok, out = denied("echo 'FOO=1 git push --force origin main'")
        check("anchored gate: an assignment inside a quoted argument is data — no fire", not ok, out)

        # monotonic: the RAW text is tried first, so a rule written to catch the
        # assignment ITSELF keeps firing. Stripping is an extra form, never a
        # replacement.
        rc, out = run("pre", dict(base, session_id="a2", tool_input={
            "command": "AWS_SECRET_ACCESS_KEY=abc123 aws s3 ls"}), genv)
        check("anchored gate: a rule ABOUT an assignment still fires on the raw text",
              "[inline-secret]" in ctx(out), out)

        # not_rx is a VETO over BOTH forms. Stripping must never become a way to
        # delete the token an author's exemption keys on — that would let
        # `FOO=1 cmd` defeat an exemption `cmd` itself honours.
        ok, out = denied("SKIP_GATE=1 git push --force origin main")
        check("anchored gate: an exemption keyed on the assignment still exempts", not ok, out)

        # KNOWN GAP, tracked separately: `^` is start-of-LINE, not
        # start-of-segment, so a command after `cd x &&` is still unmatched.
        # Stripping the assignment does not change that, and this locks it.
        ok, out = denied("cd /tmp && FOO=1 git push --force origin main")
        check("anchored gate: `^` is still line-anchored after `&&` (known gap)", not ok, out)

    branch_name_checks()
    repo_identity_checks()
    given_and_scope_checks()
    bash_edit_checks()
    diff_base_checks()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook hook checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
