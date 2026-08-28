"""Pin the two Rulebook authoring skills to the server's authoring contract.

The skills are prose, so nothing compiles them and nothing catches them
drifting from the tool they call. That has cost twice already: the skills
taught `event: "result"` / `result_rx` for months while the server's allowlist
had only `output` / `content_rx` (every "warn me when this command fails" rule
was refused at authoring time), and they kept teaching `nominate_rule` and
`status: "draft"` after the server removed both.

So the contract is restated here as constants and asserted against the files.
Every constant below is read off MemHub-Backend `app/services/rulebook/`
(`validation.py`, `mcp_tools.py`, `enums/rulebook.py`) and the plugin's own
`rulebook_hook.py`; see `docs/specs/rulebook-authoring-skills.md` §3. When the
server's vocabulary genuinely changes, change it HERE and in the skills in the
same commit — that is the whole point.

Run: python3 rulebook_skills_test.py
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "..", "plugins", "memhub", "skills")
CREATE = os.path.join(SKILLS, "create-rule", "SKILL.md")
IMPORT = os.path.join(SKILLS, "import-claude-md", "SKILL.md")

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    """The `key: value` block between the first two `---` lines."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return m.group(1) if m else ""


# ── the contract ────────────────────────────────────────────────────────────

# validation.py MATCHER_EVENTS. "write" is a legacy alias the server folds into
# "edit", so it is accepted on the wire but must not be taught as an event.
EVENTS = ("bash", "edit", "output")
# validation.py MATCHER_KEYS, minus `converted_rx` — that one marks a fire as
# converted (outcome instrumentation), it is not a trigger anyone authors.
# `min_chars` is NOT here on purpose. validation.py accepts it (and accepts an
# `edit` matcher carrying only it), but rulebook_hook.evaluate reads
# rule["path_rx"] unconditionally on the edit lane and only consults min_chars
# in the "write_stdlib" lane, which no server row can reach — so such a rule
# loads, reports itself active, and never fires. The skills must not teach it.
MATCHER_KEYS = (
    "event", "command_rx", "command_not_rx", "content_rx", "content_not_rx",
    "path_rx", "path_not_rx", "match_heredoc_body", "body_rx",
    "warn_once_per",
)
# validation.py ORDERING_KEYS
ORDERING_KEYS = (
    "required_command_rx", "gated_command_rx", "armed_by_events", "min_edits",
    "display_name", "warn_once_per",
)
# validation._WARN_ONCE_PER, minus "file": rulebook_hook._SCOPE_MAP maps both
# "file" and "session" to once-per-session, so "file" is not a real choice.
WARN_ONCE_PER = ("session", "turn", "call", "branch", "counter:N")
# mcp_tools.create_rule's reply (§3.3). `rulebook` / `org` name the destination
# — with no org_id it comes from the caller's default org, so a misroute is
# invisible unless the skill reports them.
REPLY_KEYS = ("rulebook", "org", "status", "unchanged",
              "supersedes_rule_id", "message")
# Only create-rule can reach `active`, and only an active rule retires its
# predecessor on the spot — an imported row has nothing to report here.
REPLY_KEYS_CREATE_ONLY = ("activated", "superseded_rule_ids")
# BadRequestError data.reason codes both skills must know how to recover from.
# Both mean nothing was written, so a retry is safe.
REFUSALS = ("supersedes_unknown", "target_already_replaced")
# validation.MAX_STATEMENT, and rulebook_hook._TEXT_MAX / _RX_MAX. Matched as a
# phrase, not as digits: "400" is a substring of "4000", which is what this cap
# used to be — so the naive check passed on the very regression it pins.
TEXT_BUDGET = "400 characters"
STALE_BUDGETS = ("4000", "2000 characters")

# The MCP tools each skill may call, prod and staging names. `nominate_rule` is
# gone (MemHub-Backend #1117): a nomination is create_rule(source="nomination").
EXPECTED_TOOLS = {
    "mcp__plugin_memhub_memhub__list_rules",
    "mcp__plugin_memhub_memhub__create_rule",
    "mcp__plugin_memhub-staging_memhub__list_rules",
    "mcp__plugin_memhub-staging_memhub__create_rule",
}

# Vocabulary the server refuses or no longer has, and that no skill may name as
# something to write. `draft` is matched as a status token, not as the English
# word — "draft the rule" is fine.
BANNED = (
    ("nominate_rule", "removed in MemHub-Backend #1117"),
    ("`draft`", "removed from RuleStatus; a write lands active or proposed"),
    ('status: "draft"', "removed from RuleStatus"),
    ("backtest", "column, argument and gate all removed in MemHub-Backend #1103"),
    ("at most 15", "MAX_POSTURE is removed; do not quote a cap"),
)
# These three are the shapes an author reaches for by reflex, so naming them as
# wrong is worth more than never writing them down. Allowed ONLY inside a
# paragraph that marks them as such — which is what NEGATION detects.
BANNED_UNLESS_NEGATED = (
    ("result_rx", "not a matcher key; the failing-command shape is content_rx"),
    ('event: "result"', "not an event; the server's tool-output event is `output`"),
    ('warn_once_per: "file"', "the hook maps `file` to `session`"),
)
# A paragraph-wide waiver was too coarse: one "there is no …" sentence licensed
# every banned token beside it, so a wrong instruction could be appended to the
# very paragraph that disowns it and still pass. Scope the waiver to the
# SENTENCE, and require the negation to be near the token.
_NEGATED = r"(?:\bno\b|\bnot\b|\bnever\b|\bnot an?\b)[^.]{0,60}$"


def sentences(text):
    """Rough sentence split — enough to keep a waiver local to its own claim."""
    return [s for s in re.split(r"(?<=[.:])\s+|\n\s*\n", text) if s]


def mentions(text, key):
    """Is `key` named as a code token — `key` or `key: value` — not as prose?"""
    return bool(re.search(r"`" + re.escape(key) + r"[`:]", text))


def is_negated(sentence, token):
    """Does the text before EVERY occurrence of `token` disown it?

    Per occurrence, not just the first. `sentence.index(token)` looked only at
    the text before the earliest one, so a sentence that disowns the token and
    then teaches it — "there is no `result_rx` key, so use `result_rx`" — was
    waived wholesale on the strength of the disavowal. That is the same hole as
    the paragraph-wide waiver this replaced, one scope down.
    """
    prev_end = 0
    for m in re.finditer(re.escape(token), sentence):
        # Each occurrence is judged only on the text since the PREVIOUS one.
        # Without that bound the 60-character window simply reaches back to the
        # earlier disavowal and waives the teaching occurrence anyway — which is
        # the paragraph-wide bug all over again, just at a shorter range.
        if not re.search(_NEGATED, sentence[prev_end:m.start()], re.S):
            return False
        prev_end = m.end()
    return True


def _check_waiver_semantics() -> None:
    """Guard the guard: the negation waiver is the one place this suite can be
    talked out of a finding, so its own edge cases are asserted here.

    Not named ``test_*`` on purpose — this suite runs as one ``main()`` (the
    house's other single-entry suite, ``rulebook_conflicts_test.py``, does the
    same), and ``registration_test.py`` reads the ``if __name__`` block to find
    which ``test_*`` functions are actually wired. A ``test_*`` helper called
    from inside ``main()`` reads to that checker as an orphan, and it is right
    to say so rather than guess.
    """
    check(is_negated("There is no `result_rx` key", "result_rx"),
          "a disowned token must be waived")
    check(not is_negated("Use `result_rx` for failing commands", "result_rx"),
          "a taught token must be flagged")
    check(not is_negated(
              "There is no `result_rx` key, so always use `result_rx` here",
              "result_rx"),
          "a token taught AFTER being disowned in the same sentence must still "
          "be flagged — the waiver is per occurrence, not per sentence")
    check(not is_negated(
              "Use `result_rx`, though there is no `result_rx` key", "result_rx"),
          "a token taught BEFORE the disavowal must be flagged")


def main() -> int:
    create, imp = read(CREATE), read(IMPORT)
    both = (("create-rule", create), ("import-claude-md", imp))

    _check_waiver_semantics()

    # 1. Nothing the server refuses, or no longer has, is taught anywhere.
    for name, text in both:
        for bad, why in BANNED:
            check(bad not in text, f"{name} still teaches {bad!r} — {why}")
        for bad, why in BANNED_UNLESS_NEGATED:
            taught = [s for s in sentences(text)
                      if bad in s and not is_negated(s, bad)]
            check(not taught,
                  f"{name} names {bad!r} in a sentence that does not disown it "
                  f"— {why}. Offending: {taught[:1]}")

    # 2. allowed-tools names exactly the tools that exist, on both servers.
    for name, text in both:
        tools = set(re.findall(r"mcp__[\w-]+__\w+", frontmatter(text)))
        check(tools == EXPECTED_TOOLS,
              f"{name} allowed-tools mismatch: extra={sorted(tools - EXPECTED_TOOLS)} "
              f"missing={sorted(EXPECTED_TOOLS - tools)}")

    # 3. create-rule carries the authoritative matcher vocabulary in full.
    for ev in EVENTS:
        check(f"`{ev}`" in create, f"create-rule omits the {ev!r} event")
    for key in MATCHER_KEYS:
        check(f"`{key}`" in create, f"create-rule omits the matcher key {key!r}")
    for key in ORDERING_KEYS:
        check(key in create, f"create-rule omits the ordering key {key!r}")
    for scope in WARN_ONCE_PER:
        check(f"`{scope}`" in create, f"create-rule omits warn_once_per {scope!r}")
    # …and says why `file` is missing, so nobody restores it as a kindness.
    check("Do **not** offer `file`" in create,
          "create-rule must say `file` is not offered (the hook collapses it)")

    # 4. import-claude-md defers to that one table rather than restating it —
    #    two copies of a closed vocabulary is how the last drift started.
    check("/memhub:create-rule" in imp,
          "import-claude-md must point at create-rule's matcher vocabulary")
    for ev in EVENTS:
        check(ev in imp, f"import-claude-md omits the {ev!r} event")
    check("min_chars" in imp, "import-claude-md omits min_chars")

    # 5. Both state the 400-character statement budget — and neither restates a
    #    cap this one replaced.
    for name, text in both:
        check(TEXT_BUDGET in text, f"{name} omits the {TEXT_BUDGET!r} statement budget")
        for stale in STALE_BUDGETS:
            check(stale not in text, f"{name} quotes the stale budget {stale!r}")

    # 6. Both read the reply rather than assuming an outcome, and both name the
    #    destination (rulebook + org) so a wrong-org write is visible.
    for name, text in both:
        for key in REPLY_KEYS:
            check(mentions(text, key), f"{name} never reads the reply key `{key}`")
        for reason in REFUSALS:
            check(reason in text, f"{name} does not handle the {reason!r} refusal")
        # …and by their sentence, since the reason code never crosses the wire
        # (mcp_server.py collapses AppException to exc.msg).
        check("isn't a live rule in this rulebook" in text,
              f"{name} must recognise the supersedes_unknown refusal by its text")
        check("already been replaced by someone else" in text,
              f"{name} must recognise the target_already_replaced refusal by its text")
    for key in REPLY_KEYS_CREATE_ONLY:
        check(mentions(create, key), f"create-rule never reads the reply key `{key}`")

    # 7. `activate` is create-rule's alone: an imported rule is always reviewed,
    #    whatever it passes (mcp_tools._landing_status).
    check("`activate: true`" in create,
          "create-rule must name the activate argument, not just the word 'activates'")
    check('`status: "active"`' in create,
          "create-rule must describe the active landing status")
    check("Never pass `activate`" in imp,
          "import-claude-md must forbid activate outright")

    # 8. Neither skill files without an explicit confirmation turn.
    for name, text in both:
        check("in the background" in text,
              f"{name} must forbid filing in the background")

    # 9. An `edit` rule with no `path_rx` is accepted by the server and never
    #    fires in the hook, so neither skill may present one as an option.
    check("`edit` needs `path_rx`" in create,
          "create-rule must require path_rx on an edit matcher")
    for name, text in both:
        if "min_chars" in text:
            check("never fire" in text,
                  f"{name} names min_chars without saying such a rule never fires")

    if FAILS:
        print(f"{FAILS} rulebook_skills checks failed")
        return 1
    print("all rulebook_skills checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
