"""The work-type block `pr_link` appends when a pull request has no type yet.

Pure text assertions against `pr_link.context_for` — no network, no `$HOME`
state, stdlib only. The wire behaviour this text describes is the backend's and
is pinned by MemHub-Backend's own `tests/test_pr_classifications.py`; what can
go wrong HERE is the plugin telling the agent something the server will refuse,
so these cases are about the instruction, not the transport.

The two rules with teeth:

* a classification is PERMANENT (the backend ships no UPDATE, no DELETE and no
  correction endpoint), so a pull request that already carries a type must
  never be re-asked — the call would 409 and the type could never land; and
* the seven tokens are matched literally with no normalisation, so the tuple
  here has to stay exactly the backend's.

Run: python3 tests/pr_work_type_test.py   (stdlib only)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import pr_link  # noqa: E402

PR = "https://github.com/o/r/pull/7"
SID = "sess-1"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def reply(pr_type=None, **over):
    """A connected `check` reply whose PR carries `pr_type`."""
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


def test_pr_types_is_exactly_the_backends_set():
    # Order and spelling both matter: `validate_classification` compares
    # against this set with no lowering, stripping or synonym map, and a DB
    # CHECK constraint backs it. A "feature" added here is a 400 at runtime.
    check("the seven tokens, in the backend's order",
          pr_link.PR_TYPES
          == ("feat", "fix", "chore", "docs", "perf", "refactor", "other"),
          repr(pr_link.PR_TYPES))
    check("mixed and none are not offered as inputs",
          "mixed" not in pr_link.PR_TYPES and "none" not in pr_link.PR_TYPES)


def test_classification_wanted_only_on_an_untyped_pr():
    check("null pr_type wants a classification",
          pr_link.classification_wanted(reply()) is True)
    check("an existing type does not",
          pr_link.classification_wanted(reply("fix")) is False)
    check("an explicit `other` is a decision, not an absence",
          pr_link.classification_wanted(reply("other")) is False)
    for junk in ({}, {"pr": None}, {"pr": "o/r#7"}, None, [], "nope"):
        check(f"a reply shaped {junk!r} is not a classification prompt",
              pr_link.classification_wanted(junk) is False)


def _ctx(pr_type, *, created):
    return pr_link.context_for(reply(pr_type), PR, SID, created=created)


def test_b1_asks_for_a_type_when_the_pr_has_none():
    ctx = _ctx(None, created=True)
    check("names the pr_type argument", 'pr_type="' in ctx)
    check("names the classification session argument",
          'classification_session_id="sess-1"' in ctx, ctx[-200:])
    check("offers every accepted token",
          all(t in ctx for t in pr_link.PR_TYPES))
    check("says the decision is final", "CANNOT be corrected" in ctx)
    check("still carries the link instruction",
          'link_source="session_self"' in ctx)


def test_the_session_id_is_identical_in_both_arguments():
    ctx = pr_link.context_for(reply(), PR, "sess-1", created=True)
    check("both arguments name the same id",
          'session_ids=["sess-1"]' in ctx
          and 'classification_session_id="sess-1"' in ctx)


def test_a_padded_session_id_can_never_reach_the_instruction():
    # The server STRIPS each entry of session_ids but compares
    # classification_session_id RAW, so emitting "  abc  " in BOTH places is a
    # guaranteed `classification_session_invalid`. What keeps the hook safe is
    # upstream: `conversation_id_for` strips before the id is ever formatted.
    for host in pr_link.HOSTS:
        got = pr_link.conversation_id_for(host, "  abc  ")
        check(f"{host}: the namespaced id carries no padding",
              got is not None and got == got.strip() and "abc" in got, repr(got))


# Digests of the two templates as they shipped in v0.59.0, BEFORE this feature.
# Their whole job is that an already-classified pull request still gets exactly
# the text it got before classification existed. Rebuilding the expectation from
# `pr_link.CREATED` would only prove `classify` resolved to "" — it would pass
# just as happily if the wording had been rewritten. Change one of these
# deliberately, never to make the suite green.
GOLDEN = {"CREATED": "d1ff99bbfe3e421c", "IN_PLAY": "c11f5c756d2608a1"}


def test_the_templates_still_say_what_they_said_before_this_feature():
    import hashlib
    for name, want in GOLDEN.items():
        got = hashlib.sha256(
            getattr(pr_link, name).encode("utf-8")).hexdigest()[:16]
        check(f"{name} wording is unchanged", got == want,
              f"{got} != {want} — intended? then update GOLDEN")


def test_an_already_typed_pr_is_never_re_asked():
    for created in (True, False):
        lane = "B1" if created else "B2"
        ctx = _ctx("fix", created=created)
        check(f"{lane}: no pr_type argument", "pr_type=" not in ctx)
        check(f"{lane}: no classification session argument",
              "classification_session_id" not in ctx)
        check(f"{lane}: nothing of the block leaks in",
              not any(t in ctx for t in ("CANNOT be corrected", "primary type",
                                         "record what kind of work")))


def test_b2_ties_the_type_to_the_decision_to_link():
    ctx = _ctx(None, created=False)
    check("asks only if the agent links", ctx.count("IF you link it") == 1)
    check("says a non-author records nothing",
          "does not get to label it" in ctx)
    check("keeps B2's authorship conditional",
          "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" in ctx)


def test_the_two_failures_get_opposite_advice():
    """The 404 and the 409 both refuse the WHOLE write, but for opposite
    reasons, so the recovery differs and the difference is the whole point.

    404 `classification_session_not_found`: the session has not been captured
    yet, so retrying without the type links nothing either — waiting is the
    only thing that helps.

    409 `pr_classification_conflict`: the session is fine and the link would
    have succeeded; only the type is contested. Retrying WITHOUT the type is
    what rescues the link, and telling the agent to just stop would lose it.
    """
    ctx = _ctx(None, created=True)
    check("the capture race gets one bounded wait",
          "wait 10 seconds" in ctx and "retry the identical call once" in ctx)
    check("…and is never 'fixed' by dropping the type",
          "Do NOT drop the type" in ctx)
    check("the conflict says the link did not happen",
          "THE LINK DID NOT HAPPEN" in ctx)
    check("…and is recovered by retrying without the fields",
          "classification_session_id REMOVED" in ctx)
    check("neither branch ever re-guesses a type",
          "Never try a different type" in ctx)


def test_hostile_values_never_break_the_instruction():
    """Server-controlled strings reach `.format()` — as ARGUMENTS, never as the
    format string. A `{` in a repo name, a state, or a session id must not
    raise: this runs in a PostToolUse hook, where an exception is a traceback
    on a user's screen after a command that otherwise succeeded.

    Also re-asserts the two properties most likely to rot: B2's last line, and
    the two places the session id is written.
    """
    import itertools
    repos = ["o/r", "o/{r}", "{}", None, 123]
    states = ["open", "{state}", None]
    sids = ["s1", "s{1}", "{0}{1}", "  spaced  ", 'quo"te', "back\\slash",
            "nl\nline", "unicodé-ид", "{!r}", "%s"]
    rows = [[], [{"is_mine": True}], "not-a-list", [None, {"is_mine": True}]]

    raised = tail = mismatch = 0
    for repo, state, sid, ls in itertools.product(repos, states, sids, rows):
        body = reply()
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
            if (f'session_ids=["{sid}"]' in out) != (
                    f'classification_session_id="{sid}"' in out):
                mismatch += 1

    check("no input shape raises out of context_for", raised == 0, f"{raised} raised")
    check("B2 always ends on the line that keeps a babysit loop quiet",
          tail == 0, f"{tail} lost the tail")
    check("the session id is written identically in both arguments",
          mismatch == 0, f"{mismatch} mismatched")


def test_an_org_that_cannot_link_is_never_asked_to_classify():
    # These replies return before the link text is built, so a classification
    # block would be advice about a call the org cannot make at all.
    off = pr_link.context_for(reply(github_connected=False), PR, SID, created=True)
    check("disconnected GitHub gets the advisory only",
          "pr_type=" not in off and "GitHub integration connected" in off)
    out = pr_link.context_for(reply(repo_in_install=False), PR, SID, created=True)
    check("a repo outside the install gets the advisory only",
          "pr_type=" not in out and "isn't part of the install" in out)
    check("a disabled org gets silence",
          pr_link.context_for(reply(enabled=False), PR, SID, created=True) is None)


def test_no_session_id_means_no_instruction_at_all():
    check("an empty session id yields nothing to link or classify",
          pr_link.context_for(reply(), PR, "", created=True) is None)


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
