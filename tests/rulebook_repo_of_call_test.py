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
* and when a Bash command DOES say which repo it is about — `gh … -R
  owner/repo`, or a `cd` it runs first — the `given` probes measure that
  checkout rather than wherever the shell happened to be; a named repo we
  hold NO checkout of, or several, measures nothing at all;
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
    # `commondir` points back at the main checkout's `.git`, and git writes
    # it for every linked worktree. It is what `repo_identity` reads to name
    # the repo, so a fixture without one is not the shape git produces and
    # would test a resolution path no real worktree takes.
    with open(os.path.join(gitdir, "commondir"), "w", encoding="utf-8") as f:
        f.write("../..\n")
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
        # repo is the CANONICAL name (the main checkout's) while root stays the
        # worktree's own path — the split the naming fix introduced.
        check("a file in a worktree decides the checkout from a container cwd",
              repo == "MainRepo" and root == wt, f"{repo} {root}")
        check("the worktree's gitdir points into the MAIN checkout",
              gitdir == os.path.join(main, ".git", "worktrees", "MainRepo-feature"), gitdir)
        # `feat/x` whole, not `x`: this assertion predated the fix that stopped
        # `_branch` splitting a ref on its last slash, so asserting "x" here
        # would re-assert that truncation.
        check("branch is read from the worktree's own HEAD, not the main one",
              branch == "feat/x", branch)
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
        # Assert the PAIR: main and the worktree now share a repo NAME, so a
        # name-only assertion can no longer tell "resolved into the worktree"
        # from "resolved into the main checkout".
        check("a cwd inside a checkout is unaffected when no file is named",
              rb.repo_of_call({"cwd": wt})[:2] == ("MainRepo", wt))

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
              == "MainRepo")

        # --- root stays in the session's path space (OrderingEngine key) -----
        alias = os.path.join(td, "alias")
        try:
            os.symlink(container, alias)
            r_alias = call(alias, file_path=os.path.join(alias, "MainRepo-feature", "app", "s.py"))
            check("root is returned unresolved, so one worktree keys ordering state once",
                  r_alias[1] == os.path.join(alias, "MainRepo-feature"), r_alias[1])
            check("…and it still resolves to the same repo through the alias",
                  r_alias[0] == "MainRepo", r_alias[0])
        except (OSError, NotImplementedError):
            print("SKIP  unresolved-root check (symlinks unavailable)")

        # --- untrusted payload strings degrade, never raise -----------------
        newdir = os.path.join(wt, "app", "does", "not", "exist", "yet.py")
        check("a Write naming a directory that does not exist yet walks up to it",
              call(container, file_path=newdir)[:2] == ("MainRepo", wt))

        # Judged from INSIDE a checkout, so "degrades to the cwd answer" is a
        # real claim: were the junk to be honoured, or to abort the call, the
        # answer would not be the worktree. From the container cwd these would
        # pass vacuously — every answer there is empty.
        for bad in ("", "\x00/etc/passwd", "   ", "../" * 40 + "etc/passwd"):
            check(f"a junk file_path degrades to the cwd answer ({bad[:18]!r})",
                  rb.repo_of_call({"cwd": wt, "tool_input": {"file_path": bad}})[0]
                  == "MainRepo")
        for bad in (None, 123, ["/a/b"], {"p": 1}):
            check(f"a non-string file_path degrades to the cwd answer ({type(bad).__name__})",
                  rb.repo_of_call({"cwd": wt, "tool_input": {"file_path": bad}})[0]
                  == "MainRepo")
        for bad in ("a string", ["a", "list"], 7, 0, False):
            check(f"a tool_input that is not a dict cannot raise ({type(bad).__name__})",
                  rb.repo_of_call({"cwd": wt, "tool_input": bad})[0] == "MainRepo")

        check("notebook_path is honoured like file_path",
              call(container, notebook_path=os.path.join(wt, "app", "n.ipynb"))[:2]
              == ("MainRepo", wt))
        check("a junk file_path does not shadow a good notebook_path",
              rb._acted_on_dir(container, {"file_path": 123,
                                           "notebook_path": os.path.join(wt, "app", "n.ipynb")})
              == os.path.join(wt, "app"))
        check("a file that resolves nowhere falls back to the cwd's checkout",
              rb.repo_of_call({"cwd": wt, "tool_input":
                               {"file_path": os.path.join(outside, "a.py")}})[0]
              == "MainRepo")


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
        seed_book(base, "MainRepo", [RULE])   # canonical: one book for the repo
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
        seed_book(base2, "MainRepo", [dict(RULE, _scope_repos=["SomeOtherRepo"])])
        rc, out = run("pre", {"session_id": "s9", "cwd": container, "tool_name": "Write",
                              "tool_input": {"file_path": fp, "content": BODY}},
                      dict(env, MEMHUB_RULEBOOK_BASE=base2))
        check("a rule scoped to a different repo still does not fire",
              rc == 0 and out.strip() == "", out)


def bash_target_checks() -> None:
    """Which checkout a BASH call's `given` predicates measure.

    `repo_of_call`'s own docstring named the hole: "A Bash call carries no
    path and keeps the cwd answer." So the diff and branch probes ran wherever
    the shell happened to be. Measured consequence, and the reason this
    exists: a `diff_lines_gt: 500` gate read 32,910 lines — the untracked
    files of sibling agents' worktrees — because the session's cwd was the
    directory that CONTAINS the checkouts rather than the one the command was
    actually about.

    A command says which repo it is about in two ways, and both now outrank
    cwd: a repo it NAMES (`gh … -R owner/repo`) and a `cd` it runs first.
    """
    with tempfile.TemporaryDirectory() as td:
        container = os.path.join(td, "container")       # holds checkouts, is not one
        os.makedirs(container)
        here = mkmain(container, "Here")
        other = mkmain(container, "Other")
        plain = os.path.join(container, "notarepo")
        os.makedirs(plain)

        def set_origin(root, url):
            with open(os.path.join(root, ".git", "config"), "w", encoding="utf-8") as f:
                f.write('[core]\n\trepositoryformatversion = 0\n'
                        '[remote "origin"]\n\turl = %s\n\tfetch = +refs/heads/*\n' % url)

        set_origin(here, "git@github.com:acme/here.git")
        set_origin(other, "https://github.com/acme/other")

        def addressed(command, cwd=here):
            return rb.addressed_root(cwd, rb.repo_info(cwd)[1], command)

        # --- 3. the default: the worktree containing cwd, exactly as before --
        check("bash: a command that says nothing keeps the session's checkout",
              addressed("git diff --stat") == here)
        check("bash: and from a container cwd it still resolves nothing",
              addressed("git diff --stat", cwd=container) == "")

        # --- 2. the `cd` the command runs before it --------------------------
        check("bash: `cd ../other && git diff` measures the repo it cd'd into",
              addressed("cd ../Other && git diff --stat") == other,
              addressed("cd ../Other && git diff --stat"))
        check("bash: an absolute `cd` too", addressed(f"cd {other} && git diff") == other)
        # A `cd` on its own LINE is the ordinary way an agent writes this, and
        # the old prefix regex required a `&&` or `;` right after the path.
        check("bash: a `cd` on its own line counts as much as one before `&&`",
              addressed(f"cd {other}\ngit diff --stat") == other,
              addressed(f"cd {other}\ngit diff --stat"))
        check("bash: chained `cd`s resolve against each other",
              addressed("cd ../Other && cd ../Here && git diff") == here,
              addressed("cd ../Other && cd ../Here && git diff"))
        # A `cd` AFTER a command has already run means the call worked in two
        # trees, and there is one probe root. `command_root` still declines to
        # follow that `cd` — it may never be reached — but declining now means
        # the call measures NOTHING rather than the session's tree, because
        # `git diff` here most likely runs in `../Other` and answering with
        # `Here` would be the confidently wrong answer.
        check("bash: a `cd` after a command means the call measures nothing",
              addressed("ls && cd ../Other && git diff") == "",
              addressed("ls && cd ../Other && git diff"))
        check("bash: a LEADING `cd` run is still followed",
              addressed(f"cd {other} && git diff") == other)
        check("bash: a `cd` to somewhere that is not a checkout keeps cwd's",
              addressed(f"cd {plain} && git diff") == here)
        # `cd A || cd B` runs the second only when the FIRST failed, and the
        # command text cannot say which happened. Following it would answer
        # about a tree the shell may never have entered.
        # A refusal is not "no redirect". `cd Other || cd Here; git diff` most
        # likely diffs in Other (a successful `cd` returns zero, so the second
        # is skipped), and answering with the session's tree was the
        # confidently wrong answer — `command_root` returns None to refuse and
        # "" only when the shell really did not move.
        check("bash: a `cd` reached by `||` measures nothing",
              addressed(f"cd {other} || cd {here} ; git diff") == "",
              addressed(f"cd {other} || cd {here} ; git diff"))
        check("bash: `command_root` refuses with None, not \"\"",
              rb.command_root(here, f"cd {other} || cd {here} ; git diff") is None)
        # The shapes where the shell really does NOT move still answer "" and
        # keep the session's tree: a failed `cd`, a piped one, a backgrounded
        # one — in each the command runs where the shell already was.
        for stays in (f"cd /nope/nowhere && git diff", f"cd {other} | git diff",
                      f"cd {other} & git diff"):
            check(f"bash: {stays.split('&&')[0].strip()!r} leaves the shell put",
                  rb.command_root(here, stays) == "", repr(rb.command_root(here, stays)))

        # `help builtin` / `help command`: both run the named builtin with its
        # arguments, and both really do move the shell.
        for wrapped in (f"builtin cd {other} && git diff",
                        f"command cd {other} && git diff"):
            check(f"bash: {wrapped.split(' cd')[0]!r} cd is still a cd",
                  addressed(wrapped) == other, f"{wrapped!r} -> {addressed(wrapped)}")
        check("bash: `cd A ; cd B` (both run) is still followed",
              addressed(f"cd {plain} ; cd {other} ; git diff") == other,
              addressed(f"cd {plain} ; cd {other} ; git diff"))
        # A `cd` to somewhere that is not there leaves the shell where it was,
        # so the answer is the deepest directory the chain provably REACHED.
        # `cd A && cd missing || git diff` runs its recovery command from A.
        recover = f"cd {other} && cd nowhere-at-all || git diff"
        check("bash: a failed later `cd` keeps the last directory reached, "
              "not the session's checkout",
              addressed(recover) == other, f"{recover!r} -> {addressed(recover)}")
        check("bash: and with no directory ever reached it still keeps cwd's",
              addressed("cd /nope/nowhere && git diff") == here)
        # A `cd` on the LEFT of a pipeline runs in a subshell, so the
        # directory never reaches the right-hand side. `||` is refused because
        # we cannot see which branch ran; this is refused because we can.
        check("bash: a `cd` piped into something is refused — it runs in a "
              "subshell and the directory never carries",
              addressed(f"cd {other} | git diff") == here,
              addressed(f"cd {other} | git diff"))

        # --- 1. the repo the command NAMES ----------------------------------
        check("bash: `gh -R acme/other` measures the repo it addresses, not cwd",
              addressed("gh pr create -R acme/other --fill") == other,
              addressed("gh pr create -R acme/other --fill"))
        check("bash: `--repo` reads the same",
              addressed("gh pr view 7 --repo acme/other") == other)
        check("bash: the case of the slug does not decide it",
              addressed("gh pr view 7 -R ACME/Other") == other)
        check("bash: naming cwd's OWN repo keeps cwd's checkout",
              addressed("gh pr create -R acme/here --fill") == here)
        check("bash: a named repo outranks a `cd`",
              addressed(f"cd {here} && gh pr view 7 -R acme/other") == other)
        # The whole point: a command about a repo we do not hold must be
        # measured in NO repo rather than in this one. Every probe then
        # answers None, no predicate is satisfied, and the rule stays silent —
        # the fail-open property the hook rests on.
        check("bash: a repo this machine does not hold resolves to no checkout",
              addressed("gh pr create -R someone/elsewhere --fill") == "")
        # Refusing now means SILENCE, not a fall-back to the local checkout.
        # A command that names a repo this cannot read must not be answered
        # with the tree the shell happens to be in — that is a guess, and a
        # guess is how the wrong-repo measurement comes back.
        check("bash: two different repos named in one command measure nothing, "
              "exactly as two `--base`es are refused",
              addressed("gh pr view -R acme/other || gh pr view -R acme/here") == "",
              addressed("gh pr view -R acme/other || gh pr view -R acme/here"))
        # `-R` is `--recursive` to grep, cp and rsync. Only a `gh` segment
        # gets to name a repo with it.
        check("bash: `grep -R foo/bar .` names no repo",
              addressed("grep -R foo/bar .") == here)
        check("bash: a `-R` this cannot parse measures nothing rather than "
              "falling back to the local tree",
              addressed("gh pr create -R https://github.com/acme/other") == "",
              addressed("gh pr create -R https://github.com/acme/other"))
        # `gh pr view --help`: `-R, --repo [HOST/]OWNER/REPO`. Missing a valid
        # spelling is not harmless — it falls back to the CWD checkout, which
        # is the bug this whole function exists to fix.
        for spelling in ("gh pr view 7 -Racme/other",               # attached short form
                         "gh pr view 7 --repo=acme/other",          # equals form
                         "gh pr view 7 --repo acme/other"):
            check(f"bash: {spelling.split(' ', 3)[3]!r} names the repo",
                  addressed(spelling) == other, f"{spelling!r} -> {addressed(spelling)}")

        # The HOST is part of the identity. `acme/other` here is on
        # github.com; a command naming the same owner and name on another
        # server is naming different code, and matching it would answer
        # branch and diff predicates about the wrong repository entirely.
        check("bash: a host-qualified `-R` does not match a checkout on "
              "ANOTHER host", addressed("gh pr view 7 -R ghe.corp/acme/other") == "",
              addressed("gh pr view 7 -R ghe.corp/acme/other"))
        check("bash: and it does match when the hosts agree",
              addressed("gh pr view 7 -R github.com/acme/other") == other,
              addressed("gh pr view 7 -R github.com/acme/other"))
        check("bash: an unqualified `-R` still matches on owner/repo, the way "
              "`gh` resolves its default host",
              addressed("gh pr view 7 -R acme/other") == other)
        check("bash: two spellings that are not provably one repo measure nothing",
              addressed("gh pr view -R ghe.corp/acme/other || "
                        "gh pr view -R acme/other") == "")

        # A `|` inside a quoted argument is data, not a separator. `--jq` with
        # a pipe is the everyday case, and splitting there left the `-R` in a
        # fragment that no longer began with `gh`.
        jq = "gh pr view --json title --jq '.title | ascii_downcase' -R acme/other"
        check("bash: a jq pipe inside quotes does not hide the named repo",
              addressed(jq) == other, f"{jq!r} -> {addressed(jq)}")
        check("bash: a `;` inside quotes is data too",
              addressed('gh pr comment -b "one; two" -R acme/other') == other)
        # A FLAG inside a quoted value is data as well. The flag is looked for
        # in the blanked copy; the value is read from the original at the same
        # offsets, which is why a legitimately quoted value still parses.
        body = 'gh pr comment 1 -b "try --repo acme/other please"'
        check("bash: a `--repo` inside a comment body names no repo",
              addressed(body) == here, f"{body!r} -> {addressed(body)}")
        check("bash: but a quoted VALUE still parses",
              addressed('gh pr view 7 -R "acme/other"') == other,
              addressed('gh pr view 7 -R "acme/other"'))

        # `gh help environment`: GH_REPO names the repo in the same
        # `[HOST/]OWNER/REPO` form, and GH_HOST supplies the host an
        # unqualified `-R` leaves out. `strip_leading_assignments` threw both
        # away before `named_repo` could look, so the call fell back to the
        # session's checkout — the bug this function exists to remove.
        check("bash: `GH_REPO=` names the repo just as `-R` does",
              addressed("GH_REPO=acme/other gh pr view 7") == other,
              addressed("GH_REPO=acme/other gh pr view 7"))
        check("bash: a host-qualified GH_REPO on another host matches nothing",
              addressed("GH_REPO=ghe.corp/acme/other gh pr view 7") == "")
        check("bash: `GH_HOST=` qualifies an unqualified `-R`",
              addressed("GH_HOST=ghe.corp gh pr view -R acme/other") == "",
              addressed("GH_HOST=ghe.corp gh pr view -R acme/other"))
        check("bash: and the matching host still resolves",
              addressed("GH_HOST=github.com gh pr view -R acme/other") == other)
        check("bash: an assignment that is not leading is not env",
              addressed("echo GH_REPO=acme/other") == here)
        # `gh` also reads GH_REPO/GH_HOST from the environment it inherits,
        # and Claude Code hands the hook the same environment it hands the
        # tool shell. An exported GH_REPO silently retargets every plain `gh`
        # call in the session.
        import os as _os
        for var, val in (("GH_REPO", "acme/other"),):
            saved = _os.environ.get(var)
            _os.environ[var] = val
            try:
                check("bash: an INHERITED GH_REPO names the repo",
                      rb.named_repo("gh pr view 7") == "acme/other",
                      rb.named_repo("gh pr view 7"))
                check("bash: an explicit `-R` still outranks an inherited one",
                      rb.named_repo("gh pr view 7 -R acme/here") == "acme/here")
            finally:
                if saved is None:
                    _os.environ.pop(var, None)
                else:
                    _os.environ[var] = saved
        check("bash: and with nothing inherited a plain `gh` names nothing",
              rb.named_repo("gh pr view 7") == "")
        # An assignment written as `GH_REPO=` is an EXPLICIT empty value: the
        # shell passes it and `gh` falls back to the local repo. Falling back
        # on falsiness restored the inherited value and probed the wrong one.
        saved = _os.environ.get("GH_REPO")
        _os.environ["GH_REPO"] = "acme/other"
        try:
            check("bash: `GH_REPO= gh …` clears an inherited value",
                  rb.named_repo("GH_REPO= gh pr view") == "",
                  rb.named_repo("GH_REPO= gh pr view"))
        finally:
            if saved is None:
                _os.environ.pop("GH_REPO", None)
            else:
                _os.environ["GH_REPO"] = saved
        # `env --help`: `env [NAME=VALUE]... [COMMAND]`. Stripping the
        # assignments left a segment starting with `env`, which no longer
        # looked like a `gh` call.
        check("bash: `env GH_REPO=… gh …` is a gh call",
              addressed("env GH_REPO=acme/other gh pr view") == other,
              addressed("env GH_REPO=acme/other gh pr view"))

        # ONE call has ONE probe root, so a call whose segments address
        # DIFFERENT repos has no answer that is right for both. `-R` selects
        # another repository for the `gh` call only; the push still runs here.
        mixed = "gh pr view -R acme/other && git push"
        check("bash: a call that addresses two repos measures neither",
              addressed(mixed) == "", f"{mixed!r} -> {addressed(mixed)}")
        check("bash: all-`gh` segments naming the same repo still resolve",
              addressed("gh pr view -R acme/other && gh pr merge -R acme/other") == other)
        check("bash: a bare `cd` alongside a `gh` call is not a disagreement",
              addressed(f"cd {plain} && gh pr view -R acme/other") == other,
              addressed(f"cd {plain} && gh pr view -R acme/other"))
        # `gh help environment`: GH_REPO applies to commands that would
        # OTHERWISE use the local repo; `-R` selects one explicitly. The flag
        # wins. Treating them as two candidates made this look ambiguous and
        # fall back to the session's checkout.
        check("bash: an explicit `-R` outranks GH_REPO",
              addressed("GH_REPO=acme/here gh pr view -R acme/other") == other,
              addressed("GH_REPO=acme/here gh pr view -R acme/other"))

        # A double-quoted span honours backslash escapes, so ending it at the
        # first `\"` put the rest of the argument back into the shell grammar
        # and a `|` inside it became an operator.
        esc = ('gh pr view --json title --jq "if .title == '
               '\\"a|b\\" then .title else empty end" -R acme/other')
        check("bash: an escaped quote inside a quoted jq expression does not "
              "end the span", addressed(esc) == other, f"{esc!r} -> {addressed(esc)}")
        # THE INVARIANT: `named_repo` answers only when the WHOLE call names
        # one repo. `git status` runs in the checkout the shell is in, so this
        # call addresses two, and the answer is nothing. Agreement is
        # `named_repo`'s own job now rather than a separate check downstream —
        # the same rule reached five separate times as five bug fixes, stated
        # once instead.
        check("bash: a `gh -R` beside a command that runs HERE names nothing",
              rb.named_repo("git status && gh pr view 7 -R acme/other") == "",
              rb.named_repo("git status && gh pr view 7 -R acme/other"))
        check("bash: two `gh` segments naming the SAME repo agree",
              rb.named_repo("gh pr view -R acme/other && gh pr merge -R acme/other")
              == "acme/other")
        # An unflagged `gh` still targets the current checkout, so it disagrees
        # with a flagged one just as `git push` does.
        check("bash: a flagged `gh` beside an unflagged one names nothing",
              rb.named_repo("gh pr view -R acme/other && gh pr create --fill") == "",
              rb.named_repo("gh pr view -R acme/other && gh pr create --fill"))
        # `env [OPTION]... [NAME=VALUE]... [COMMAND]`: `env -u CI gh …` is a
        # gh call and `env -i sh -c …` is not. Not knowing is a legitimate
        # answer — the next spelling nobody has thought of lands here and is
        # harmless, instead of being guessed at.
        check("bash: an `env` wrapper carrying options refuses rather than guesses",
              rb.named_repo("env -u CI GH_REPO=acme/other gh pr view") == "",
              rb.named_repo("env -u CI GH_REPO=acme/other gh pr view"))
        check("bash: a plain `env` wrapper is still read",
              rb.named_repo("env GH_REPO=acme/other gh pr view") == "acme/other")
        # `env NAME=VALUE cmd` sets the environment FOR that command, so an
        # inner assignment beats the shell's outer one.
        check("bash: an inner `env` assignment beats the outer shell one",
              rb.named_repo("GH_REPO=acme/here env GH_REPO=acme/other gh pr view")
              == "acme/other",
              rb.named_repo("GH_REPO=acme/here env GH_REPO=acme/other gh pr view"))
        check("bash: an inner `GH_REPO=` clears an outer value",
              rb.named_repo("GH_REPO=acme/other env GH_REPO= gh pr view") == "")
        # The `env --help` contract holds however the executable was located.
        check("bash: `/usr/bin/env` is the same wrapper",
              rb.named_repo("/usr/bin/env GH_REPO=acme/other gh pr view") == "acme/other",
              rb.named_repo("/usr/bin/env GH_REPO=acme/other gh pr view"))
        check("bash: and by path WITH options it still refuses",
              rb.named_repo("/usr/bin/env -u CI GH_REPO=acme/other gh pr view") == "")
        # `<wrapper> [options] <the real command>`: `command gh …` is a gh
        # call, and calling it `local` was a confidently wrong answer rather
        # than a refusal. Options put it back in `unknown`.
        for cmd, want in (("command gh pr view -R acme/other", "acme/other"),
                          ("sudo gh pr view -R acme/other", "acme/other"),
                          ("sudo env GH_REPO=acme/other gh pr view", "acme/other"),
                          ("nohup gh pr view -R acme/other", "acme/other"),
                          ("sudo -u bob gh pr view -R acme/other", "unknown"),
                          ("sudo git push", "local")):
            check(f"segment target: {cmd!r} -> {want}",
                  rb._segment_target(cmd) == want, rb._segment_target(cmd))

        # Quoting was only ever half of "this character is data": a
        # backslash-escaped operator outside quotes is not an operator.
        esc_op = r"gh pr view --jq .title\|ascii_downcase -R acme/other"
        check("bash: a backslash-escaped pipe is not a separator",
              addressed(esc_op) == other, f"{esc_op!r} -> {addressed(esc_op)}")
        check("bash: `gh` invoked by path is still `gh`",
              addressed("/usr/bin/gh pr view -R acme/other") == other,
              addressed("/usr/bin/gh pr view -R acme/other"))
        # `git [-C <path>] …` selects a DIRECTORY. The call knows more than a
        # slug could resolve, so refusing keeps it out of the wrong tree —
        # measuring the session's would be the confidently wrong answer.
        check("bash: `git -C <path>` refuses rather than measuring the session's",
              addressed("git -C ../Other diff") == "",
              addressed("git -C ../Other diff"))
        check("bash: a plain git command is unaffected",
              addressed("git diff --stat") == here)
        # A grouping token is not part of a command's name. Stripped rather
        # than refused, because `(gh …)` is unambiguous — what it groups is
        # right there. Command substitution is deliberately untouched: it is
        # everywhere, and sweeping it into `unknown` would stop measuring
        # ordinary commands.
        for grouped in ("(gh pr view -R acme/other)", "{ gh pr view -R acme/other; }"):
            check(f"bash: {grouped!r} is still a gh call",
                  addressed(grouped) == other, f"{grouped!r} -> {addressed(grouped)}")
        check("bash: command substitution is not grouping",
              rb._segment_target("git checkout $(git branch --show-current)") == "local")
        # A subshell's `cd` DOES move the commands inside that subshell, so a
        # group wrapping the WHOLE command is unwrapped and read normally.
        # The two shapes where it does not move the last command are left
        # alone: `(cd a) && x` closes the subshell first, and `(cd a && x) &&
        # y` moves only `x`.
        check("bash: `(cd ../Other && git push)` measures Other",
              rb.command_root(here, f"(cd {other} && git push)") == other,
              rb.command_root(here, f"(cd {other} && git push)"))
        check("bash: `{ cd ../Other && git push; }` reads the same",
              rb.command_root(here, f"{{ cd {other} && git push; }}") == other)
        check("bash: `(cd ../Other) && git push` does NOT — the subshell exited",
              rb.command_root(here, f"(cd {other}) && git push") == "")
        check("bash: `(cd ../Other && x) && y` does NOT — only `x` moved",
              rb.command_root(here, f"(cd {other} && x) && git push") == "")

        # `git -h` lists three global selectors, and GIT_DIR / GIT_WORK_TREE
        # do the same from the environment. Each points git at a tree this
        # cannot name as a repo, so each refuses rather than answering with
        # the session's.
        for cmd in ("git -C ../Other diff",
                    "git --git-dir=../Other/.git --work-tree=../Other diff",
                    "GIT_DIR=../Other/.git git diff"):
            check(f"segment target: {cmd.split(' ')[1]!r} refuses",
                  rb._segment_target(cmd) == "unknown", rb._segment_target(cmd))
        check("segment target: a plain git command still answers local",
              rb._segment_target("git diff --stat") == "local")
        # Inherited counts too — `git rev-parse --local-env-vars` lists these
        # as repository-local, so a session launched with GIT_DIR exported
        # points every plain `git` at another checkout.
        import os as _os2
        saved = _os2.environ.get("GIT_DIR")
        _os2.environ["GIT_DIR"] = "/somewhere/else/.git"
        try:
            check("segment target: an INHERITED GIT_DIR refuses",
                  rb._segment_target("git diff --stat") == "unknown",
                  rb._segment_target("git diff --stat"))
        finally:
            if saved is None:
                _os2.environ.pop("GIT_DIR", None)
            else:
                _os2.environ["GIT_DIR"] = saved
        check("segment target: and without it, local again",
              rb._segment_target("git diff --stat") == "local")

        # A substitution RUNS its body. Read rather than refused wholesale, so
        # the everyday `$(git branch --show-current)` keeps its precision.
        check("bash: a `gh -R` inside `$( )` is seen and disagrees with the "
              "assignment around it",
              sorted(rb.segment_targets("x=$(gh pr view -R acme/other)"))
              == ["acme/other", "local"],
              str(sorted(rb.segment_targets("x=$(gh pr view -R acme/other)"))))
        check("bash: ordinary command substitution stays local",
              sorted(rb.segment_targets("git checkout $(git branch --show-current)"))
              == ["local"])
        check("bash: a substitution inside quotes is data",
              sorted(rb.segment_targets("echo '$(gh pr view -R acme/other)'")) == ["local"])
        check("bash: a NESTED substitution refuses rather than guessing",
              "unknown" in rb.segment_targets("x=$(echo $(gh pr view -R acme/other))"))
        # The shell's own distinction: `'$(cmd)'` is a literal string and
        # `"$(cmd)"` RUNS cmd. Blanking both hid every substitution written
        # inside double quotes, which is most of them.
        check("bash: a substitution inside DOUBLE quotes runs and is seen",
              sorted(rb.segment_targets('echo "$(gh pr view -R acme/other)"'))
              == ["acme/other", "local"],
              str(sorted(rb.segment_targets('echo "$(gh pr view -R acme/other)"'))))
        check("bash: and inside SINGLE quotes it is still a string",
              sorted(rb.segment_targets("echo '$(gh pr view -R acme/other)'")) == ["local"])

        # `help export`: exported values apply to commands run afterwards, so
        # this steers a segment resolved independently. Refused rather than
        # carried forward — the precise answer is available, but this review
        # has been unkind to extra precision on this surface.
        check("bash: `export GH_REPO=… && gh …` refuses",
              "unknown" in rb.segment_targets("export GH_REPO=acme/other && gh pr view"))
        check("bash: `set -a` refuses too — every later assignment is exported",
              "unknown" in rb.segment_targets("set -a && GH_REPO=acme/other gh pr view"))
        check("bash: an export of something UNrelated does not refuse",
              sorted(rb.segment_targets("export PATH=/x && gh pr view -R acme/other"))
              == ["acme/other", "local"])

        # --- found by auditing the paired functions, not by the reviewer ---
        # A shell runs whatever it is handed; `sh -c "gh …"` reached the gh
        # test as `sh` and answered `local`. It meets the `-c` rule now.
        for shell_wrapped in ('sh -c "gh pr view -R acme/other"',
                              'bash -c "gh pr view -R acme/other"'):
            check(f"bash: {shell_wrapped[:6]!r} refuses rather than answering local",
                  rb.segment_targets(shell_wrapped) == {"unknown"},
                  str(rb.segment_targets(shell_wrapped)))

        # The shell worked in two trees, and there is one probe root.
        check("bash: a `cd` partway through the call measures nothing",
              addressed("cd /tmp && git push && cd /var && git diff") == "",
              addressed("cd /tmp && git push && cd /var && git diff"))

        # A standalone `&` backgrounds the command to its left and the next
        # one runs anyway, so this is TWO commands addressing two repos.
        bg = "gh pr view -R acme/other & git push"
        check("bash: a standalone `&` separates segments",
              rb.named_repo(bg) == "", f"{bg!r} -> {rb.named_repo(bg)}")
        # ...but a redirection `&` is not a separator.
        for redir in ("gh pr view -R acme/other 2>&1", "gh pr view -R acme/other >&2",
                      "gh pr view -R acme/other &> /tmp/log"):
            check(f"bash: {redir.split(' ', 4)[-1]!r} is redirection, not a separator",
                  rb.named_repo(redir) == "acme/other", rb.named_repo(redir))

        # `origin_slug` reads .git/config, never git — this decides a probe
        # root on every Bash call's hot path. It carries the HOST, because
        # owner/repo alone cannot tell two servers apart.
        check("origin slug: scp-style remote keeps the host",
              rb.origin_slug(here) == "github.com/acme/here", rb.origin_slug(here))
        check("origin slug: https remote, `.git` or not",
              rb.origin_slug(other) == "github.com/acme/other", rb.origin_slug(other))
        check("origin slug: a checkout with no remote answers nothing",
              rb.origin_slug(mkmain(container, "Bare")) == "")
        for url, want in (("ssh://git@ghe.corp:22/acme/x", "ghe.corp/acme/x"),
                          ("git@github.com:acme/x.git", "github.com/acme/x"),
                          ("https://ghe.corp/acme/x/", "ghe.corp/acme/x"),
                          ("/srv/git/x", "git/x")):          # a local remote has no host
            check(f"origin slug: {url!r}", rb._slug_of_url(url) == want, rb._slug_of_url(url))
        check("slug match: a local checkout with no known host still matches "
              "a host-qualified name — one unknown is not a mismatch",
              rb.slug_matches("acme/x", "ghe.corp/acme/x"))

        # A LINKED worktree has no config of its own; its `commondir` names
        # the main checkout's, which is where the remote actually lives.
        wt = mkworktree(container, other, "Other-feature", "feat/y")
        check("origin slug: a linked worktree answers with its repo's remote",
              rb.origin_slug(wt) == "github.com/acme/other", rb.origin_slug(wt))

        # AMBIGUITY. `acme/other` is now TWO checkouts — the main one and its
        # worktree. `-R` names a repository, and the probes read
        # worktree-specific state (branch, dirty, the diff itself), so
        # answering with either one answers a question nobody asked: a clean
        # sibling silently passes a diff gate the real work would have
        # tripped, a dirty one blocks a call over somebody else's branch.
        # Silence is the honest answer, and it is the direction the whole
        # hook fails in.
        check("bash: a named repo with SEVERAL local checkouts resolves to none",
              rb.addressed_root(container, "", "gh pr view 7 -R acme/other") == "",
              rb.addressed_root(container, "", "gh pr view 7 -R acme/other"))
        # Two details do pin one, and both outrank the scan.
        check("bash: standing in one of them disambiguates it",
              rb.addressed_root(wt, wt, "gh pr view 7 -R acme/other") == wt)
        check("bash: `cd`ing into one of them disambiguates it",
              rb.addressed_root(container, "", f"cd {wt} && gh pr view 7 -R acme/other") == wt,
              rb.addressed_root(container, "", f"cd {wt} && gh pr view 7 -R acme/other"))
        # The command's own `cd` outranks the session's cwd: a `cd` is what
        # the command SAYS, cwd is only where it happens to start. Checking
        # the session first made this disambiguation work only when the
        # session was somewhere else entirely.
        check("bash: a `cd` into a SIBLING worktree beats the session's own "
              "checkout of the same repo",
              rb.addressed_root(other, other, f"cd {wt} && gh pr view 7 -R acme/other") == wt,
              rb.addressed_root(other, other, f"cd {wt} && gh pr view 7 -R acme/other"))
        check("bash: a repo with exactly ONE checkout still resolves",
              addressed("gh pr view 7 -R acme/here") == here)

        # A scan that stopped on its cap or its deadline has not ruled out a
        # second worktree further down the listing, so its single hit is not
        # a proven-unique hit.
        saved = rb._SIBLINGS_MAX
        try:
            rb._SIBLINGS_MAX = 1
            check("bash: a TRUNCATED scan answers nothing, even with one hit",
                  rb.addressed_root(container, "", "gh pr view 7 -R acme/here") == "",
                  rb.addressed_root(container, "", "gh pr view 7 -R acme/here"))
        finally:
            rb._SIBLINGS_MAX = saved
        check("bash: and resolves again once the scan can finish",
              rb.addressed_root(container, "", "gh pr view 7 -R acme/here") == here)

        # No root is not a crash: every probe answers None, which satisfies
        # no predicate.
        p = rb.Probes("", "")
        check("probes: with no checkout every git-backed probe answers None",
              p.diff_lines() is None and p.diff_paths() is None and p.dirty() is None)


def main() -> int:
    unit_checks()
    bash_target_checks()
    hook_checks()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all repo-of-call checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
