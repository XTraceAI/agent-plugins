"""Asking for a work type is conditional, and the condition is the link itself.

v0.62.0 (#245) taught the hook to ask the agent for a `pr_type`, but asked
unconditionally. That is not merely a wasted call on a pull request that
already has a type: `record_classification` raises `pr_classification_conflict`
BEFORE `_insert_links` and before the commit, so the server refuses the WHOLE
write — the session does not get linked at all. A second session touching an
already-classified pull request therefore lost its link, which is the thing
linking exists to do.

So the ask is gated on the server reporting no type yet, and the injected text
carries a recovery for the race that gate cannot close. Verified live against
staging on agent-plugins#247: a third call with a different type returned
"This PR already has a different classification; linking cannot overwrite it."
and wrote nothing.

Run: python3 tests/pr_work_type_test.py   (stdlib only)
"""
from __future__ import annotations

import hashlib
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import pr_link  # noqa: E402

PR = "https://github.com/o/r/pull/7"
SID = "sess-1"

# The CREATED / IN_PLAY templates as they stood BEFORE #245 added the
# classification sentence. A pull request that already carries a type must
# render exactly this again — the ask disappears rather than degrading into
# something new. Change these deliberately, never to make the suite green.
GOLDEN_PRE_CLASSIFICATION = {"CREATED": "0feb75e89d6c385f",
                             "IN_PLAY": "c79dc68b09546397"}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def reply(pr_type=None, **over):
    body = {
        "enabled": True, "github_connected": True, "repo_in_install": True,
        "connect_url": "https://app.example.test/i",
        "pr": {"known": True, "repo_full_name": "o/r", "pr_number": 7,
               "state": "open", "pr_type": pr_type,
               "pr_type_source": "agent_session" if pr_type else None},
        "linked_sessions": [],
    }
    body.update(over)
    return body


def _ctx(pr_type, *, created):
    return pr_link.context_for(reply(pr_type), PR, SID, created=created)


def test_pr_types_is_exactly_the_backends_set():
    check("the seven tokens, in the backend's order",
          pr_link.PR_TYPES
          == ("feat", "fix", "chore", "docs", "perf", "refactor", "other"),
          repr(pr_link.PR_TYPES))
    check("mixed and none are never offered as inputs",
          "mixed" not in pr_link.PR_TYPES and "none" not in pr_link.PR_TYPES)


def test_classification_wanted_only_on_an_untyped_pr():
    check("null pr_type wants a classification",
          pr_link.classification_wanted(reply()) is True)
    check("an existing type does not",
          pr_link.classification_wanted(reply("fix")) is False)
    check("an explicit `other` is a decision, not an absence",
          pr_link.classification_wanted(reply("other")) is False)
    for junk in ({}, {"pr": None}, {"pr": "o/r#7"}, {"pr": 5}, None, [], "nope"):
        check(f"a reply shaped {junk!r} is not a classification prompt",
              pr_link.classification_wanted(junk) is False)


def test_an_untyped_pr_is_asked_for_a_type():
    for created in (True, False):
        lane = "B1" if created else "B2"
        ctx = _ctx(None, created=created)
        check(f"{lane}: names the pr_type argument", "pr_type = " in ctx)
        check(f"{lane}: names the classification session",
              'classification_session_id="sess-1"' in ctx)
        check(f"{lane}: offers every accepted token",
              all(t in ctx for t in pr_link.PR_TYPES))
        check(f"{lane}: still carries the link instruction",
              'link_source="session_self"' in ctx)


def test_an_already_typed_pr_renders_the_pre_classification_text():
    """The regression this change exists to fix.

    Not merely "does not ask" — it must render what the hook rendered before
    #245 existed, because that text is the one whose link actually lands.
    """
    for created in (True, False):
        lane = "B1" if created else "B2"
        ctx = _ctx("fix", created=created)
        for marker in ("pr_type", "classification_session_id",
                       "THE LINK DID NOT HAPPEN"):
            check(f"{lane}: no {marker}", marker not in ctx)
        want = (pr_link.CREATED if created else pr_link.IN_PLAY).format(
            pr_url=PR, pr_ref=" (o/r#7, open)", linked="", session_id=SID,
            classify="", recovery="")
        check(f"{lane}: identical to the un-asked rendering", ctx == want)


def test_the_templates_minus_the_slots_are_the_v0_59_text():
    """A digest of what an already-typed PR gets, pinned to pre-#245 bytes.

    Rebuilding the expectation from `pr_link.CREATED` (as the test above does)
    proves the slots resolved empty; it would pass just as happily if the
    surrounding wording had been rewritten. This is what catches that.
    """
    for name, want in GOLDEN_PRE_CLASSIFICATION.items():
        rendered = getattr(pr_link, name).format(
            pr_url="{pr_url}", pr_ref="{pr_ref}", linked="{linked}",
            session_id="{session_id}", classify="", recovery="")
        got = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
        check(f"{name} minus the slots is byte-for-byte v0.59.0", got == want,
              f"{got} != {want} — intended? then update GOLDEN_PRE_CLASSIFICATION")


def test_b2_keeps_the_line_that_quiets_a_babysit_loop_last():
    """`/memhub:pr-babysit` runs `gh pr view` on every pass, so B2 fires every
    pass. The final line is what stops the agent narrating it each time, and it
    only works while it is the last thing read — so the recovery text is
    slotted above it, never appended after it.
    """
    for pr_type in (None, "fix"):
        ctx = _ctx(pr_type, created=False)
        check(f"pr_type={pr_type!r}: ends on the quieting line",
              ctx.endswith("say nothing at all."), ctx[-90:])


def test_the_two_failures_get_opposite_advice():
    """Both refuse the whole write, for opposite reasons, so recovery differs.

    404 `classification_session_not_found`: the session is not captured yet, so
    retrying without the type links nothing either — only waiting helps.
    409 `pr_classification_conflict`: the session is fine and the link would
    have succeeded; only the type is contested, so dropping it rescues the LINK.
    """
    ctx = _ctx(None, created=True)
    check("the capture race gets one bounded wait",
          "wait 10 seconds" in ctx and "retry the identical call once" in ctx)
    check("…and is never 'fixed' by dropping the type",
          "Do NOT drop the type" in ctx)
    check("the conflict says the link was refused too",
          "THE LINK DID NOT HAPPEN EITHER" in ctx)
    check("…and is recovered by retrying without the fields",
          "classification_session_id REMOVED" in ctx)
    check("neither branch ever re-guesses a type",
          "Never try a different type" in ctx)


def test_an_org_that_cannot_link_is_never_asked_to_classify():
    off = pr_link.context_for(reply(github_connected=False), PR, SID, created=True)
    check("disconnected GitHub gets the advisory only",
          "pr_type" not in off and "GitHub integration connected" in off)
    out = pr_link.context_for(reply(repo_in_install=False), PR, SID, created=True)
    check("a repo outside the install gets the advisory only",
          "pr_type" not in out and "isn't part of the install" in out)
    check("a disabled org gets silence",
          pr_link.context_for(reply(enabled=False), PR, SID, created=True) is None)
    check("no session id means nothing to link or classify",
          pr_link.context_for(reply(), PR, "", created=True) is None)


def test_hostile_values_never_break_the_instruction():
    """Server-controlled strings reach `.format()` as ARGUMENTS, never as the
    format string — a `{` in a repo name or session id must not raise, because
    this runs in a PostToolUse hook where an exception is a traceback on the
    user's screen after a command that already succeeded.
    """
    repos = ["o/r", "o/{r}", "{}", None, 123]
    states = ["open", "{state}", None]
    sids = ["s1", "s{1}", "{0}{1}", 'quo"te', "back\\slash", "unicodé-ид",
            "{!r}", "%s"]
    rows = [[], [{"is_mine": True}], "not-a-list", [None, {"is_mine": True}]]

    raised = tail = mismatch = 0
    for repo, state, sid, ls, ptype in itertools.product(
            repos, states, sids, rows, (None, "fix")):
        body = reply(ptype)
        body["pr"].update(repo_full_name=repo, state=state)
        body["linked_sessions"] = ls
        for created in (True, False):
            try:
                out = pr_link.context_for(body, PR, sid, created=created)
            except Exception:
                raised += 1
                continue
            if not out:
                continue
            if not created and not out.endswith("say nothing at all."):
                tail += 1
            # Both markers, asserted independently: an `and` guard here would
            # score a break that stopped emitting session_ids as a pass.
            if (f'session_ids=["{sid}"]' in out) != (
                    f'classification_session_id="{sid}"' in out or ptype == "fix"):
                mismatch += 1

    check("no input shape raises out of context_for", raised == 0, f"{raised} raised")
    check("B2 always ends on the line that quiets a babysit loop",
          tail == 0, f"{tail} lost the tail")
    check("the session id is written identically in both arguments",
          mismatch == 0, f"{mismatch} mismatched")


if __name__ == "__main__":
    print("pr_work_type")
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all pr_work_type checks passed")
    sys.exit(1 if failures else 0)
