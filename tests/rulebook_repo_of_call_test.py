"""Self-test for which checkout the rulebook hook decides a CALL works in.

The bug this pins: the repo gate resolved the session's cwd and hard-returned
when that directory was not itself inside a checkout. Standing in a folder
that CONTAINS many worktrees and editing files inside them — the ordinary
worktree-parent workflow — made the entire rulebook silently inert for the
whole session, while every edited file sat in a real worktree the entire time.
No output, no error, no ledger row: "no rules exist", "nothing matched" and
"the book never loaded" were indistinguishable from inside the session.

Covers:

* the decisive 2x2 — {Write, Edit} x {container cwd, worktree cwd}: all four
  fire once the acted-on file decides the checkout, where three were silent;
* worktrees resolve through the `.git` FILE and `scope_ok` maps the gitdir
  back to the MAIN checkout, so a rule scoped to the repo matches from any of
  its worktrees — the machinery was always correct, only the starting point
  was wrong;
* a Bash call carries no path, so a non-git cwd stays silent exactly as
  before (the fail-open property the hook is built on);
* the session cwd is the trust boundary: a path outside it is ignored, a
  symlink under it cannot smuggle the lookup out, and a relative path binds
  to the SESSION's cwd rather than this process's;
* `root` comes back in the session's own path space, because it keys
  OrderingEngine state (`{rid}@{root}:{branch}`) and one worktree reached two
  ways would split into two keys and silently re-arm its rules;
* untrusted payload strings (NUL bytes, wrong types, a directory that does
  not exist yet) resolve or degrade, never raise.

Run: python3 rulebook_repo_of_call_test.py  (stdlib only).
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


def run(mode: str, payload: dict, env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ, **env_extra)
    p = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


def seed_book(base: str, repo_name: str, rules: list) -> None:
    """Write the cached server book the fetch lane would have left on disk."""
    d = os.path.join(base, "book")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)[:60]
    h = hashlib.sha1(repo_name.encode("utf-8")).hexdigest()[:8]
    with open(os.path.join(d, f"{safe}-{h}.json"), "w", encoding="utf-8") as f:
        json.dump({"etag": "seed",
                   "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                   "rules": rules}, f)


def ctx(out: str) -> str:
    """The additionalContext the hook emitted, or "" when it stayed silent."""
    if not out.strip():
        return ""
    try:
        return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")
    except Exception:
        return ""


def mkmain(parent: str, name: str, branch: str = "main") -> str:
    """A checkout with a real `.git` DIRECTORY."""
    repo = os.path.join(parent, name)
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
    with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")
    return repo


def mkworktree(parent: str, main_repo: str, name: str, branch: str) -> str:
    """A linked worktree: a `.git` FILE pointing into the main checkout's
    gitdir, exactly as `git worktree add` writes it."""
    gitdir = os.path.join(main_repo, ".git", "worktrees", name)
    os.makedirs(gitdir, exist_ok=True)
    with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as f:
        f.write(f"ref: refs/heads/{branch}\n")
    wt = os.path.join(parent, name)
    os.makedirs(wt, exist_ok=True)
    with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
        f.write(f"gitdir: {gitdir}\n")
    return wt


# The rule from the incident, reduced to its shape: an edit-family rule scoped
# to the MAIN checkout by name, matching a client construction with no meter.
RULE = {"id": "metered-llm", "on": "edit", "version": 1,
        "path_rx": r"^(?:.*/)?app/.*\.py$",
        "content_rx": r"AsyncAnthropic\(\s*api_key",
        "_scope_repos": ["MainRepo"], "fire_scope": "session",
        "text": "LLM calls go through the metered path", "why": "cost attribution"}
BODY = "_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)\n"


def unit_checks() -> None:
    """`repo_of_call` and `_acted_on_dir` are pure and importable — replayed
    here, so what is tested is what runs."""
    with tempfile.TemporaryDirectory() as td:
        container = os.path.join(td, "container")   # holds checkouts, is not one
        os.makedirs(container)
        main = mkmain(container, "MainRepo")
        wt = mkworktree(container, main, "MainRepo-feature", "feat/x")
        outside = mkmain(td, "OutsideRepo")          # a checkout NOT under cwd

        def call(cwd, **inp):
            return rb.repo_of_call({"cwd": cwd, "tool_input": inp})

        # --- the gate itself ------------------------------------------------
        check("container cwd alone resolves nothing (the bug's precondition)",
              rb.repo_info(container) == ("", "", "", ""))

        repo, root, gitdir, branch = call(container,
                                          file_path=os.path.join(wt, "app", "svc.py"))
        check("a file in a worktree decides the checkout from a container cwd",
              repo == "MainRepo-feature" and root == wt, f"{repo} {root}")
        check("the worktree's gitdir points into the MAIN checkout",
              gitdir == os.path.join(main, ".git", "worktrees", "MainRepo-feature"), gitdir)
        check("branch is read from the worktree's own HEAD, not the main one",
              branch == "x", branch)
        check("scope_ok maps the worktree back to the repo the rule names",
              rb.scope_ok(RULE, repo, gitdir))

        repo2, root2, _, _ = call(container, file_path=os.path.join(main, "app", "svc.py"))
        check("a file in the main checkout resolves it too",
              repo2 == "MainRepo" and root2 == main, f"{repo2} {root2}")

        # --- no path to act on: unchanged, still silent ----------------------
        check("a Bash call from a container cwd still resolves nothing",
              call(container, command="git push") == ("", "", "", ""))
        check("no tool_input at all (SessionStart) still resolves nothing",
              rb.repo_of_call({"cwd": container}) == ("", "", "", ""))
        check("a cwd inside a checkout is unaffected when no file is named",
              rb.repo_of_call({"cwd": wt})[0] == "MainRepo-feature")

        # --- the trust boundary ---------------------------------------------
        check("an absolute path OUTSIDE the session cwd is ignored",
              call(container, file_path=os.path.join(outside, "app", "svc.py"))
              == ("", "", "", ""))

        link = os.path.join(container, "escape")
        try:
            os.symlink(outside, link)
            check("a symlink under cwd cannot smuggle the lookup outside it",
                  call(container, file_path=os.path.join(link, "app", "svc.py"))
                  == ("", "", "", ""))
        except (OSError, NotImplementedError):      # unprivileged Windows
            print("SKIP  symlink containment (symlinks unavailable)")

        # The DANGEROUS direction, and the one a resolved-only check misses: a
        # link OUTSIDE cwd whose target is inside it. Containment on the real
        # path passes, while the lexical parents still lead to another
        # checkout — which is the one `repo_info` would walk up into.
        inward = os.path.join(td, "outside-link")
        try:
            os.symlink(os.path.join(container, "MainRepo-feature"), inward)
            check("a symlink outside cwd pointing INTO it does not escape the boundary",
                  call(container, file_path=os.path.join(inward, "app", "svc.py"))
                  == ("", "", "", ""),
                  str(call(container, file_path=os.path.join(inward, "app", "svc.py"))))
        except (OSError, NotImplementedError):
            print("SKIP  inward-symlink containment (symlinks unavailable)")

        # --- one path space per session (the OrderingEngine key) ------------
        # An absolute path spelled in a different-but-equivalent space
        # (/var vs /private/var, an automounted home) must not produce a
        # second `root` for the same worktree: `{rid}@{root}:{branch}` and the
        # ordering state file are keyed by it, and two keys silently re-arm.
        real_wt = os.path.realpath(wt)
        if real_wt != wt:
            by_cwd = rb.repo_of_call({"cwd": wt, "tool_input": {"command": "git push"}})[1]
            by_file = rb.repo_of_call({"cwd": wt, "tool_input":
                                       {"file_path": os.path.join(real_wt, "app", "x.py")}})[1]
            check("the same worktree yields ONE root via the file and via the cwd",
                  by_cwd == by_file, f"{by_cwd!r} != {by_file!r}")
        else:
            print("SKIP  path-space check (tmpdir is already canonical)")

        # --- _under: the prefix test at every kind of root ------------------
        check("_under keeps a name-prefix sibling out (/a/bc is not under /a/b)",
              not rb._under("/a/bc", "/a/b"))
        check("_under admits a real child, and the base itself",
              rb._under("/a/b/c", "/a/b") and rb._under("/a/b", "/a/b"))
        check("_under at a POSIX root admits everything below it",
              rb._under("/etc", "/"))

        # A relative path belongs to the SESSION's cwd. Were it bound to this
        # process's cwd instead, the answer would depend on where the test ran.
        check("a relative path resolves against the session cwd, not ours",
              call(container, file_path=os.path.join("MainRepo-feature", "app", "svc.py"))[0]
              == "MainRepo-feature")

        # --- root stays in the session's path space (OrderingEngine key) -----
        alias = os.path.join(td, "alias")
        try:
            os.symlink(container, alias)
            r_alias = call(alias, file_path=os.path.join(alias, "MainRepo-feature", "app", "s.py"))
            check("root is returned unresolved, so one worktree keys ordering state once",
                  r_alias[1] == os.path.join(alias, "MainRepo-feature"), r_alias[1])
            check("…and it still resolves to the same repo through the alias",
                  r_alias[0] == "MainRepo-feature", r_alias[0])
        except (OSError, NotImplementedError):
            print("SKIP  unresolved-root check (symlinks unavailable)")

        # --- untrusted payload strings degrade, never raise -----------------
        newdir = os.path.join(wt, "app", "does", "not", "exist", "yet.py")
        check("a Write naming a directory that does not exist yet walks up to it",
              call(container, file_path=newdir)[0] == "MainRepo-feature")

        # Judged from INSIDE a checkout, so "degrades to the cwd answer" is a
        # real claim: were the junk to be honoured, or to abort the call, the
        # answer would not be the worktree. From the container cwd these would
        # pass vacuously — every answer there is empty.
        for bad in ("", "\x00/etc/passwd", "   ", "../" * 40 + "etc/passwd"):
            check(f"a junk file_path degrades to the cwd answer ({bad[:18]!r})",
                  rb.repo_of_call({"cwd": wt, "tool_input": {"file_path": bad}})[0]
                  == "MainRepo-feature")
        for bad in (None, 123, ["/a/b"], {"p": 1}):
            check(f"a non-string file_path degrades to the cwd answer ({type(bad).__name__})",
                  rb.repo_of_call({"cwd": wt, "tool_input": {"file_path": bad}})[0]
                  == "MainRepo-feature")
        for bad in ("a string", ["a", "list"], 7, 0, False):
            check(f"a tool_input that is not a dict cannot raise ({type(bad).__name__})",
                  rb.repo_of_call({"cwd": wt, "tool_input": bad})[0] == "MainRepo-feature")

        check("notebook_path is honoured like file_path",
              call(container, notebook_path=os.path.join(wt, "app", "n.ipynb"))[0]
              == "MainRepo-feature")
        check("a junk file_path does not shadow a good notebook_path",
              rb._acted_on_dir(container, {"file_path": 123,
                                           "notebook_path": os.path.join(wt, "app", "n.ipynb")})
              == os.path.join(wt, "app"))
        check("a file that resolves nowhere falls back to the cwd's checkout",
              rb.repo_of_call({"cwd": wt, "tool_input":
                               {"file_path": os.path.join(outside, "a.py")}})[0]
              == "MainRepo-feature")


def hook_checks() -> None:
    """§5.2's 2x2, driven through the real hook as a subprocess."""
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "state")
        container = os.path.join(td, "container")
        os.makedirs(container)
        main = mkmain(container, "MainRepo")
        wt = mkworktree(container, main, "MainRepo-feature", "feat/x")

        # The book is cached under the checkout the call resolves to — the
        # worktree's own name. Seed both so the 2x2 varies only cwd and tool.
        #
        # NOTE, so this suite is not read as more than it proves: seeding by
        # hand is what lets the worktree rows pass. In production the book is
        # FETCHED under that same worktree name, and the server filters
        # `scope_repos` by exact string membership — so a rule scoped to the
        # repo is dropped before it ever reaches the book. Measured against
        # staging: `MemHub-Backend` returns 5 rules, `MemHub-Backend-msg-buckets`
        # returns 2 (only the unscoped ones). That is a separate defect in the
        # NAMING layer, not the seeding layer this file covers; these rows
        # prove the call resolves the right checkout, not that the right rules
        # were fetched for it.
        for name in ("MainRepo", "MainRepo-feature"):
            seed_book(base, name, [RULE])
        env = {"MEMHUB_RULEBOOK_BASE": base, "MEMHUB_RULEBOOK_FETCH": "0",
               "MEMHUB_RULEBOOK_RECALL": "0"}
        fp = os.path.join(wt, "app", "message_intent.py")

        n = 0
        for tool in ("Write", "Edit"):
            for label, cwd in (("container cwd", container), ("worktree cwd", wt)):
                n += 1
                key = "content" if tool == "Write" else "new_string"
                rc, out = run("pre", {"session_id": f"s{n}", "cwd": cwd,
                                      "tool_name": tool,
                                      "tool_input": {"file_path": fp, key: BODY}},
                              env)
                check(f"2x2: {tool} from {label} fires the rule",
                      rc == 0 and "[metered-llm]" in ctx(out), out)

        # The regression the fix must NOT introduce: a Bash call from a
        # non-git cwd stays silent, because it names no file to locate.
        rc, out = run("pre", {"session_id": "b1", "cwd": container, "tool_name": "Bash",
                              "tool_input": {"command": "python app/message_intent.py"}}, env)
        check("Bash from a container cwd is still silent, exit 0",
              rc == 0 and out.strip() == "", out)

        # A file outside the session cwd must not reach across into another
        # checkout's book, even when one is cached for it.
        other = mkmain(td, "MainRepo2")
        seed_book(base, "MainRepo2", [dict(RULE, _scope_repos=["MainRepo2"])])
        rc, out = run("pre", {"session_id": "o1", "cwd": container, "tool_name": "Write",
                              "tool_input": {"file_path": os.path.join(other, "app", "x.py"),
                                             "content": BODY}}, env)
        check("a file outside the session cwd loads no book and stays silent",
              rc == 0 and out.strip() == "", out)

        # An edit that does not match still says nothing — the fix widens where
        # rules are LOOKED for, never what counts as a match.
        rc, out = run("pre", {"session_id": "q1", "cwd": container, "tool_name": "Write",
                              "tool_input": {"file_path": fp, "content": "x = 1\n"}}, env)
        check("a non-matching edit in the resolved worktree stays silent",
              rc == 0 and out.strip() == "", out)

        # Scope still bites: a rule naming another repo must not fire here.
        base2 = os.path.join(td, "state2")
        seed_book(base2, "MainRepo-feature", [dict(RULE, _scope_repos=["SomeOtherRepo"])])
        rc, out = run("pre", {"session_id": "s9", "cwd": container, "tool_name": "Write",
                              "tool_input": {"file_path": fp, "content": BODY}},
                      dict(env, MEMHUB_RULEBOOK_BASE=base2))
        check("a rule scoped to a different repo still does not fire",
              rc == 0 and out.strip() == "", out)


def main() -> int:
    unit_checks()
    hook_checks()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all repo-of-call checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
