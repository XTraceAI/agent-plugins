"""Self-test for the forward-test redirect: whose book a call reads.

/memhub:create-rule's §4b test has to arm its candidate in a book the hook
really reads, and until now that meant editing the SHARED book — one file per
repo under `$MEMHUB_RULEBOOK_BASE`, read by every session on the machine
working in that repo, on every PreToolUse. Two tests that interleave leave an
unfiled candidate armed with no backup left to find it by, and a sibling
session's fire lands inside the test's own ledger evidence window.

So a test may claim a private base for ITS OWN calls. The claim is keyed on
the session cwd, because §4b already confines the test sub-agent to a scratch
worktree.

Covers:

* the decisive pair — a cwd inside the claimed prefix reads the private book,
  a cwd outside it reads the real one, from the same redirect file;
* the default: no redirect file at all leaves every call on the real base,
  which is the overwhelming case and must cost nothing;
* the trust rules, each of which reads as "no redirect" rather than as an
  error — someone else's file, a file others can write, a forgotten one past
  REDIRECT_MAX_AGE_S, malformed JSON, a base that is not a directory;
* symlinks cannot smuggle a session in or out of a claim, checked on both the
  lexical and the resolved spelling, as `_acted_on_dir` does;
* a redirect never RAISES — a broken one must not take a session's rules away.

Run: python3 rulebook_pretest_redirect_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "plugins", "memhub", "scripts")

BASE = tempfile.mkdtemp(prefix="rb-base-")
os.environ["MEMHUB_RULEBOOK_BASE"] = BASE     # BEFORE the import: BASE is read at module load
sys.path.insert(0, SCRIPTS)

import rulebook_hook as rb  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def write_redirect(base: str, prefix: str, *, mode: int = 0o600, age_s: float = 0.0,
                   raw: str | None = None) -> str:
    p = os.path.join(BASE, rb.REDIRECT_NAME)
    body = raw if raw is not None else json.dumps({"base": base, "cwd_prefix": prefix,
                                                   "pid": os.getpid()})
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(p, mode)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def clear_redirect() -> None:
    try:
        os.unlink(os.path.join(BASE, rb.REDIRECT_NAME))
    except FileNotFoundError:
        pass


def book_under(cwd: str) -> str:
    """The directory `book_path` resolves to for a call made in `cwd`."""
    rb.set_active_base(cwd)
    return os.path.dirname(rb.book_path("MemHub-Backend"))


def main() -> int:
    check("hook is relocated to the temp base (never the real one)", rb.BASE == BASE, rb.BASE)
    real_book = os.path.join(BASE, "book")

    work = tempfile.mkdtemp(prefix="rb-work-")
    scratch_base = os.path.join(work, "scratch-base")
    scratch_wt = os.path.join(work, "scratch-worktree")
    other_wt = os.path.join(work, "other-worktree")
    for d in (os.path.join(scratch_base, "book"), scratch_wt, other_wt):
        os.makedirs(d, exist_ok=True)
    scratch_book = os.path.join(scratch_base, "book")

    # ── the default ─────────────────────────────────────────────────────
    clear_redirect()
    check("no redirect file: every call reads the real book",
          book_under(scratch_wt) == real_book and book_under(other_wt) == real_book)
    check("no cwd at all: the real book, never a crash", book_under("") == real_book)

    # ── the decisive pair ───────────────────────────────────────────────
    write_redirect(scratch_base, scratch_wt)
    check("a call inside the claimed worktree reads the PRIVATE book",
          book_under(scratch_wt) == scratch_book, book_under(scratch_wt))
    check("a call deeper inside the claimed worktree reads the private book too",
          book_under(os.path.join(scratch_wt, "a", "b")) == scratch_book)
    check("a sibling session elsewhere still reads the REAL book",
          book_under(other_wt) == real_book, book_under(other_wt))
    check("a path that merely shares the prefix's name is not inside it",
          book_under(scratch_wt + "-sibling") == real_book)

    # ── the claim covers the whole base, not just the book ──────────────
    #
    # The book alone is not enough: an unfiled candidate that fires writes a
    # row to ledger/fires.jsonl, which SYNCS. Isolating the rules while leaving
    # the evidence trail shared lets a candidate leak into the team's real fire
    # history — and leaves §4b.4's byte-offset window just as untrustworthy.
    rb.set_active_base(scratch_wt)
    check("the claimed worktree's LEDGER is private too",
          rb._ledger_dir() == os.path.join(scratch_base, "ledger"), rb._ledger_dir())
    check("the claimed worktree's STATE is private too",
          os.path.dirname(rb.state_path("s1")) == os.path.join(scratch_base, "state"))
    rb.set_active_base(other_wt)
    check("a sibling's ledger is still the real one",
          rb._ledger_dir() == os.path.join(BASE, "ledger"), rb._ledger_dir())
    check("a sibling's state is still the real one",
          os.path.dirname(rb.state_path("s1")) == os.path.join(BASE, "state"))

    # `_base()` is LAZY, never a snapshot: the other suites rebind BASE after
    # import, and a snapshot would send their ledger writes to the REAL base.
    rb.set_active_base("")
    real_base, rb.BASE = rb.BASE, os.path.join(work, "rebound-base")
    try:
        check("rebinding BASE after import still moves the ledger (lazy, not a snapshot)",
              rb._ledger_dir() == os.path.join(rb.BASE, "ledger"), rb._ledger_dir())
    finally:
        rb.BASE = real_base

    # ── the trust rules ─────────────────────────────────────────────────
    write_redirect(scratch_base, scratch_wt, mode=0o666)
    check("a redirect others can write is ignored", book_under(scratch_wt) == real_book)

    write_redirect(scratch_base, scratch_wt, age_s=rb.REDIRECT_MAX_AGE_S + 60)
    check("a redirect older than REDIRECT_MAX_AGE_S stops steering anything",
          book_under(scratch_wt) == real_book)

    write_redirect(scratch_base, scratch_wt, raw="{not json")
    check("a malformed redirect is ignored, not raised", book_under(scratch_wt) == real_book)

    write_redirect(os.path.join(work, "does-not-exist"), scratch_wt)
    check("a redirect whose base is not a directory is ignored",
          book_under(scratch_wt) == real_book)

    write_redirect(scratch_base, os.path.join(work, "no-such-prefix"))
    check("a claim on a prefix this call is not under is ignored",
          book_under(scratch_wt) == real_book)

    # ── symlinks, both spellings ────────────────────────────────────────
    write_redirect(scratch_base, scratch_wt)
    link_in = os.path.join(work, "link-to-scratch")
    os.symlink(scratch_wt, link_in)
    check("a symlink OUTSIDE the claim pointing into it does not join the claim "
          "(lexical check)", book_under(link_in) == real_book, book_under(link_in))
    link_out = os.path.join(scratch_wt, "link-out")
    os.symlink(other_wt, link_out)
    check("a symlink UNDER the claim pointing out of it does not leave the claim "
          "unresolved (resolved check)", book_under(link_out) == real_book)

    # ── never raises ────────────────────────────────────────────────────
    clear_redirect()
    for bad in ("\x00", "relative/path", "/nonexistent/" + "x" * 300):
        try:
            book_under(bad)
        except Exception as e:                      # noqa: BLE001
            check(f"a hostile cwd never raises ({bad[:16]!r})", False, repr(e))
    check("hostile cwds resolved or degraded, none raised", True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("all pretest-redirect checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
