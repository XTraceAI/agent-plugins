"""Self-test for the repo NAME a worktree reports.

The bug this pins: the hook keyed its book cache, its `?repo=` fetch and every
fire on `os.path.basename(worktree_root)`. In a linked worktree that is the
*worktree directory's* name, not the repository's. The server filters
`scope_repos` by exact string membership, so a rule scoped `["MemHub-Backend"]`
asked about `"MemHub-Backend-msg-buckets"` is dropped before the response is
built — and the worktree caches a valid, fresh, EMPTY-of-scoped-rules book.

Measured against staging while writing this fix:

    ?repo=MemHub-Backend              -> 5 rules
    ?repo=MemHub-Backend-msg-buckets  -> 2 rules  (only the scope_repos:[] ones)

i.e. 60% of that repo's rulebook was silently unenforced in every worktree, and
the rules that survived were exactly the ones nobody scoped.

The asymmetry that made it a bug rather than a choice: `scope_ok()` was already
worktree-aware — it parsed the gitdir to recover the main checkout's name — but
it can only judge rules that reached the book, and the fetch had already
discarded them under the wrong name. Worktrees were handled at the MATCHING
layer and not at the NAMING layer.

Covers:

* `main_checkout()` recovers the repo name from a linked worktree's gitdir, and
  is anchored on the `worktrees` segment rather than the first `.git` in the
  path (a repo living under a literal `.git` directory answered several levels
  too high);
* `repo_info()` reports the CANONICAL name from every worktree, while `root`
  stays the worktree's own path — git runs there and ordering state is keyed by
  it, so both must stay per-worktree;
* one book, one cache key, one `?repo=` for a repo and all its worktrees;
* a repo-scoped rule fires from a worktree with only the canonical book on
  disk — the end-to-end behaviour the naming bug prevented;
* a main checkout is completely unaffected.

Run: python3 rulebook_canonical_repo_test.py  (stdlib only).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "plugins", "memhub", "scripts")
HOOK = os.path.join(SCRIPTS, "rulebook_hook.py")
sys.path.insert(0, SCRIPTS)

import rulebook_hook as rb  # noqa: E402  (path set above so the engine is importable)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def seed_book(base: str, repo_name: str, rules: list) -> None:
    d = os.path.join(base, "book")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)[:60]
    h = hashlib.sha1(repo_name.encode("utf-8")).hexdigest()[:8]
    with open(os.path.join(d, f"{safe}-{h}.json"), "w", encoding="utf-8") as f:
        json.dump({"etag": "seed",
                   "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                   "rules": rules}, f)


def ctx(out: str) -> str:
    if not out.strip():
        return ""
    try:
        return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")
    except Exception:
        return ""


def mkmain(parent: str, name: str, branch: str = "main") -> str:
    repo = os.path.join(parent, name)
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
    with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")
    return repo


def mkworktree(parent: str, main_repo: str, name: str, branch: str) -> str:
    """A linked worktree exactly as `git worktree add` writes it: a `.git`
    FILE holding `gitdir: <main>/.git/worktrees/<name>`."""
    gitdir = os.path.join(main_repo, ".git", "worktrees", name)
    os.makedirs(gitdir, exist_ok=True)
    with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")
    wt = os.path.join(parent, name)
    os.makedirs(wt, exist_ok=True)
    with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
        f.write(f"gitdir: {gitdir}\n")
    return wt


# The incident's rule, reduced to its shape: scoped to the REPO by name.
RULE = {"id": "metered-llm", "on": "edit", "version": 1,
        "path_rx": r"^(?:.*/)?app/.*\.py$",
        "content_rx": r"AsyncAnthropic\(\s*api_key",
        "_scope_repos": ["MainRepo"], "fire_scope": "session",
        "text": "LLM calls go through the metered path", "why": "cost attribution"}
BODY = "_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)\n"


def unit_checks() -> None:
    check("main_checkout recovers the repo name from a worktree gitdir",
          rb.main_checkout("/repos/MemHub-Backend/.git/worktrees/msg-buckets")
          == "MemHub-Backend")
    # Anchored on `worktrees`, not on the FIRST `.git`: a repo kept under a
    # literal `.git` directory used to answer with a directory far too high.
    check("main_checkout is not fooled by an earlier literal .git segment",
          rb.main_checkout("/home/u/.git/backup/MemHub-Backend/.git/worktrees/wt")
          == "MemHub-Backend", rb.main_checkout("/home/u/.git/backup/MemHub-Backend/.git/worktrees/wt"))
    check("main_checkout handles Windows separators",
          rb.main_checkout(r"C:\repos\MemHub-Backend\.git\worktrees\wt") == "MemHub-Backend")
    # git APPENDS `/.git/worktrees/<x>`, so with more than one such segment the
    # LAST is authoritative — a first-match scan answers with another repo's
    # name, which is the cache key AND the `?repo=` the server filters on.
    for gitdir, want in (("/a/.git/worktrees/b/.git/worktrees/c", "b"),
                         ("/home/u/.git/worktrees/.git/worktrees/wt", "worktrees")):
        check(f"main_checkout takes the LAST .git/worktrees, not the first ({want})",
              rb.main_checkout(gitdir) == want, rb.main_checkout(gitdir))
    for bad in ("/repos/MemHub-Backend/.git", "", None, "worktrees", "/a/worktrees/b"):
        check(f"main_checkout says nothing for a non-worktree gitdir ({bad!r})",
              rb.main_checkout(bad) == "", repr(rb.main_checkout(bad)))

    with tempfile.TemporaryDirectory() as td:
        main = mkmain(td, "MainRepo")
        wt1 = mkworktree(td, main, "MainRepo-feature", "feat/x")
        wt2 = mkworktree(td, main, "MainRepo-hotfix", "hotfix/y")

        check("a main checkout is unchanged — its directory name IS the repo",
              rb.repo_info(main)[0] == "MainRepo", rb.repo_info(main)[0])

        for wt, branch in ((wt1, "x"), (wt2, "y")):
            repo, root, gitdir, br = rb.repo_info(wt)
            check(f"a worktree reports the canonical repo name ({os.path.basename(wt)})",
                  repo == "MainRepo", repo)
            check(f"…while root stays the worktree's own path ({os.path.basename(wt)})",
                  root == wt, root)
            check(f"…and branch still comes from the worktree's own HEAD ({branch})",
                  br == branch, br)

        # One repo, one book — the whole point. Before the fix these were three
        # different cache keys and three different `?repo=` values.
        keys = {os.path.basename(rb.book_path(rb.repo_info(p)[0]))
                for p in (main, wt1, wt2)}
        check("the main checkout and all its worktrees share ONE book cache key",
              len(keys) == 1, str(sorted(keys)))

        # scope_ok keeps working; the gitdir parse is now redundant, not wrong.
        repo, _, gitdir, _ = rb.repo_info(wt1)
        check("scope_ok still matches a repo-scoped rule from a worktree",
              rb.scope_ok(RULE, repo, gitdir))
        check("scope_ok still refuses a rule scoped to another repo",
              not rb.scope_ok(dict(RULE, _scope_repos=["OtherRepo"]), repo, gitdir))
        # scope_ok shares main_checkout, so it inherits the same hardening:
        # the old inline parse took the first `.git` and answered "u" here.
        check("scope_ok is not fooled by a repo under a literal .git directory",
              rb.scope_ok(RULE, "MainRepo",
                          "/home/u/.git/backup/MainRepo/.git/worktrees/wt")
              and not rb.scope_ok(dict(RULE, _scope_repos=["u"]), "MainRepo",
                                  "/home/u/.git/backup/MainRepo/.git/worktrees/wt"))

        # A RELATIVE gitdir — what git writes under `worktree.useRelativePaths`
        # and `git worktree add --relative-paths`. Unresolved, the name parse
        # answers ".." for every such worktree on the machine, so unrelated
        # repos would share one book, one fetch and one `repo` on every fire —
        # and HEAD would be read against the hook process's cwd, losing branch.
        rel = {}
        for name in ("RelAlpha", "RelBeta"):
            m = mkmain(td, name)
            os.makedirs(os.path.join(m, ".git", "worktrees", "wt"), exist_ok=True)
            with open(os.path.join(m, ".git", "worktrees", "wt", "HEAD"), "w",
                      encoding="utf-8") as f:
                f.write("ref: refs/heads/feat\n")
            w = os.path.join(m, "wt"); os.makedirs(w, exist_ok=True)
            with open(os.path.join(w, ".git"), "w", encoding="utf-8") as f:
                f.write("gitdir: ../.git/worktrees/wt\n")
            rel[name] = rb.repo_info(w)
        check("a relative gitdir still names its own repo, not '..'",
              rel["RelAlpha"][0] == "RelAlpha" and rel["RelBeta"][0] == "RelBeta",
              f'{rel["RelAlpha"][0]!r} {rel["RelBeta"][0]!r}')
        check("…so two unrelated relative-path worktrees do NOT share a book",
              rb.book_path(rel["RelAlpha"][0]) != rb.book_path(rel["RelBeta"][0]))
        check("…and branch survives, instead of resolving against our own cwd",
              rel["RelAlpha"][3] == "feat", rel["RelAlpha"][3])

        # A submodule's `.git` file points at `.git/modules/...`, which names no
        # worktree — the directory-basename fallback is the ONLY thing keeping
        # the rulebook alive there, so pin it.
        sub = os.path.join(td, "MainRepo", "vendor", "libsub")
        os.makedirs(os.path.join(main, ".git", "modules", "libsub"), exist_ok=True)
        with open(os.path.join(main, ".git", "modules", "libsub", "HEAD"), "w",
                  encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {os.path.join(main, '.git', 'modules', 'libsub')}\n")
        check("a submodule falls back to its directory name (never empty)",
              rb.repo_info(sub)[0] == "libsub", rb.repo_info(sub)[0])

        # v0.42.1's contract must survive: root is the worktree, so ordering
        # state stays per-worktree even though the NAME is now shared.
        r1 = rb.repo_of_call({"cwd": td, "tool_input": {"file_path": os.path.join(wt1, "app", "a.py")}})
        r2 = rb.repo_of_call({"cwd": td, "tool_input": {"file_path": os.path.join(wt2, "app", "a.py")}})
        check("two worktrees share a repo name but NOT a root (ordering stays per-worktree)",
              r1[0] == r2[0] == "MainRepo" and r1[1] != r2[1], f"{r1[:2]} {r2[:2]}")


def hook_checks() -> None:
    """The end-to-end behaviour the naming bug prevented: only the CANONICAL
    book is on disk, and a repo-scoped rule fires from inside a worktree."""
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "state")
        main = mkmain(td, "MainRepo")
        wt = mkworktree(td, main, "MainRepo-feature", "feat/x")
        seed_book(base, "MainRepo", [RULE])      # the ONLY book — no worktree-named one
        env = {"MEMHUB_RULEBOOK_BASE": base, "MEMHUB_RULEBOOK_FETCH": "0",
               "MEMHUB_RULEBOOK_RECALL": "0"}
        fp = os.path.join(wt, "app", "svc.py")

        for label, cwd in (("worktree cwd", wt), ("container cwd", td)):
            p = subprocess.run([sys.executable, HOOK, "pre"],
                               input=json.dumps({"session_id": f"s-{label}", "cwd": cwd,
                                                 "tool_name": "Write",
                                                 "tool_input": {"file_path": fp, "content": BODY}}),
                               capture_output=True, text=True,
                               env=dict(os.environ, **env), timeout=30)
            check(f"a repo-scoped rule reaches a worktree from the canonical book ({label})",
                  p.returncode == 0 and "[metered-llm]" in ctx(p.stdout), p.stdout)

        # The fire is attributed to the REPO, not to the worktree directory —
        # otherwise one repo's fires scatter across every worktree name.
        rows = []
        led = os.path.join(base, "ledger", "fires.jsonl")
        if os.path.exists(led):
            with open(led, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        check("fires are attributed to the repo, not the worktree directory",
              bool(rows) and all(r.get("repo") == "MainRepo" for r in rows),
              str({r.get("repo") for r in rows}))

        # A main checkout keeps behaving exactly as before.
        p = subprocess.run([sys.executable, HOOK, "pre"],
                           input=json.dumps({"session_id": "s-main", "cwd": main,
                                             "tool_name": "Write",
                                             "tool_input": {"file_path": os.path.join(main, "app", "svc.py"),
                                                            "content": BODY}}),
                           capture_output=True, text=True,
                           env=dict(os.environ, **env), timeout=30)
        check("the main checkout still fires from the same book",
              p.returncode == 0 and "[metered-llm]" in ctx(p.stdout), p.stdout)


def main() -> int:
    unit_checks()
    hook_checks()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all canonical-repo-name checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
