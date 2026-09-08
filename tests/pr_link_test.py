"""The PR-link detectors, URL extraction, session-id namespacing and check().

The two questions this module answers are independent and both matter:
``touches_github`` is the gate (without it, a `cat CHANGELOG.md` whose text
mentions a PR would inject linking context about a pull request nobody is
working on), and ``creates_pr`` chooses between an unconditional self-link and
a judgment left to the model. The implication test below is the lock on them:
a `creates_pr` that does not imply `touches_github` is a `gh pr create` that
never reaches the hook at all.

Nothing here reaches a network. ``check()`` is exercised against a stubbed
``mcp_http.rest``, and ``$HOME`` is redirected before the import so the
negative-answer cache writes into a tmpdir.

Run: python3 tests/pr_link_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"

_HOME = tempfile.mkdtemp(prefix="memhub-prlink-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
# A stray credential in the environment would let check() try a real request.
os.environ.pop("MEMHUB_TOKEN", None)

sys.path.insert(0, str(SCRIPTS))
import mcp_http  # noqa: E402
import pr_babysit_trigger  # noqa: E402
import pr_link  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# A shared corpus, used by the individual predicates AND by the two implication
# tests. Adding a command here strengthens every one of them at once.
GH_PR_COMMANDS = [
    "gh pr create --fill",
    "cd .. && gh pr create",
    "cd /repo; gh pr create",
    "(cd sub && gh pr create)",
    "env X=1 gh pr create",
    "gh pr view 12",
    "gh pr checkout 12",
    "gh --repo o/r pr merge",
    "env FOO=1 gh pr ready",
    "sudo gh pr edit",
    "x && gh pr view",
    "gh pr comment 12 --body hi",
]
NOT_GH_PR_COMMANDS = [
    'grep "gh pr view" f',
    "echo gh pr create",
    "ghost pr view",
    "gh repo view && foo pr create",
    "echo 'gh pr create'",
    "git push",
    "",
]


def test_is_gh_pr_command_matches_any_subcommand_at_command_position():
    for command in GH_PR_COMMANDS:
        check(f"gh-pr: {command!r}", pr_link.is_gh_pr_command(command))
    for command in NOT_GH_PR_COMMANDS:
        check(f"not gh-pr: {command!r}", not pr_link.is_gh_pr_command(command))


def test_is_gh_pr_create_is_narrower_and_survives_chaining():
    # The chained cases are not decoration: a `^gh pr create` anchor would miss
    # them, and missing them turns an unconditional self-link into a coin flip.
    for command in ("gh pr create --fill", "cd .. && gh pr create",
                    "cd /repo; gh pr create", "(cd sub && gh pr create)",
                    "env X=1 gh pr create"):
        check(f"create: {command!r}", pr_link.is_gh_pr_create(command))
    for command in ("gh pr view", "gh pr merge", 'grep "gh pr create" f',
                    "echo gh pr create"):
        check(f"not create: {command!r}", not pr_link.is_gh_pr_create(command))


def test_creates_pr_implies_touches_github():
    corpus = GH_PR_COMMANDS + NOT_GH_PR_COMMANDS + [
        "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
        "gh api --method POST repos/o/r/pulls -f title=x",
        "curl https://api.github.com/repos/o/r/pulls/12",
    ]
    for command in corpus:
        if pr_link.is_gh_pr_create(command):
            check(f"create implies gh-pr: {command!r}",
                  pr_link.is_gh_pr_command(command))
        payload = ("Bash", {"command": command})
        if pr_link.creates_pr(*payload):
            check(f"creates_pr implies touches_github: {command!r}",
                  pr_link.touches_github(*payload))
    for name in ("mcp__github__create_pull_request",
                 "mcp__github-mcp__open_pull_request",
                 "mcp__GitHub__createPullRequest"):
        if pr_link.creates_pr(name, {}):
            check(f"creates_pr implies touches_github: {name}",
                  pr_link.touches_github(name, {}))


def test_the_widened_regex_is_never_narrower_than_the_babysit_one():
    # pr_link copies pr_babysit_trigger's command-position regex and widens it.
    # A command the narrow one accepts and the wide one rejects would be a
    # `gh pr create` the link hook never sees.
    for command in GH_PR_COMMANDS + NOT_GH_PR_COMMANDS:
        if pr_babysit_trigger.is_pr_create(command):
            check(f"agreement: {command!r}", pr_link.is_gh_pr_command(command))
            check(f"agreement (create): {command!r}", pr_link.is_gh_pr_create(command))


def test_github_api_call_reads_the_rest_shapes_people_actually_paste():
    cases = [
        # (command, target, is_write)
        ("curl -L -X POST -H 'Accept: application/vnd.github+json' "
         "https://api.github.com/repos/O/R/pulls -d '{\"title\":\"x\"}'",
         "pulls_collection", True),
        # `-d` with no explicit method IS a POST — the shape in the wild.
        ("curl https://api.github.com/repos/O/R/pulls -d '{\"title\":\"x\"}'",
         "pulls_collection", True),
        ("curl -X GET https://api.github.com/repos/O/R/pulls -d '{}'",
         "pulls_collection", False),
        ("curl https://api.github.com/repos/O/R/pulls/12", "pull_item", False),
        ("curl -X POST https://api.github.com/repos/O/R/pulls/12", "pull_item", True),
        ("gh api --method POST repos/o/r/pulls -f title=x", "pulls_collection", True),
        # `gh api --help`: "To send the parameters as a GET query string
        # instead, use --method GET" — a documented LISTING that carries `-f`.
        # Reading it as a write told a session that had only listed PRs to
        # record a confirmed self-link (Codex review, PR #182).
        ("gh api --method GET repos/o/r/pulls -f state=open --jq '.[0].html_url'",
         "pulls_collection", False),
        ("gh api --method PATCH repos/o/r/pulls/12 -f title=x", "pull_item", False),
        # A QUOTED method value. The blanked text lost it, so an explicit GET
        # read as "no method" and `-f` inferred a POST (Codex review, PR #182).
        ("gh api repos/o/r/pulls --method 'GET' -f state=open", "pulls_collection", False),
        ('gh api repos/o/r/pulls --method "GET" -f state=open', "pulls_collection", False),
        ("curl https://api.github.com/repos/o/r/pulls -X 'GET' -d x",
         "pulls_collection", False),
        ("curl -XPOST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        ("curl --request=POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # curl's `-f` is --fail, NOT a field: it must not imply a write.
        ("curl -f https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        # A `-X POST` inside a quoted body is data, not a method.
        ("""curl https://api.github.com/repos/o/r/pulls -X GET -d '{"t":"-X POST"}'""",
         "pulls_collection", False),
        # An unbalanced quote cannot be read, so it must not manufacture one —
        # but the call still addressed GitHub, so it must not vanish either:
        # it reaches B2, where the model judges, and never B1.
        ("curl -X POST https://api.github.com/repos/o/r/pulls -d '{", "pulls_collection", False),
        # Flags belong to the invocation that carries the target. Reading them
        # from the whole string bound a chained POST to a later listing, and
        # the reverse order missed a real create (Codex review, PR #182).
        ("gh api --method POST repos/o/r/issues -f x=1 && "
         "gh api --method GET repos/o/r/pulls -f state=open", "pulls_collection", False),
        ("gh api --method GET repos/o/r/issues && "
         "gh api --method POST repos/o/r/pulls -f title=x", "pulls_collection", True),
        ("grep 'https://api.github.com/repos/o/r/pulls' f && "
         "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # `gh api --hostname` is an enterprise call with no URL in it at all.
        ("gh api --hostname ghe.corp --method POST repos/o/r/pulls -f title=x",
         "pulls_collection", True),
        # A path-prefixed client is still that client.
        ("/usr/local/bin/curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        ("env X=1 sudo timeout 5 curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # The other clients we accept: wget documents `--post-data` as POST,
        # and HTTPie/xh take the method as a bare word (Codex review, PR #182).
        ("wget --post-data='{\"title\":\"x\"}' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        ("wget --post-file=b.json https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        ("wget https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        # `curl --help all`: "--json <data>  HTTP POST JSON" (Codex, PR #182).
        ("curl --json '{\"title\":\"x\"}' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        ("curl --json @body.json https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        # Short options can carry their value attached, and can be bundled.
        # `-f` is curl's --fail and `-D` its --dump-header, so neither counts
        # (Codex review, PR #182).
        ("curl -d'{\"title\":\"x\"}' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        ("curl -Ftitle=x https://api.github.com/repos/o/r/pulls", "pulls_collection", True),
        ("curl -sSfd @b.json https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        ("curl -fsSL https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("curl -D headers.txt https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("curl -o out.json https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        # An ATTACHED value must not be scanned for more options: `-Dheaders`
        # is `--dump-header headers`, and finding the `d` in "headers" called a
        # GET a creation (Codex review, PR #182 — a regression I introduced).
        ("curl -Dheaders https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("curl -oFood https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("curl -Afriend https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("curl -HFood:x https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        # `-G/--get` puts the data in the URL and sends GET, so a listing that
        # carries `-d` is still a read (Codex review, PR #182).
        ("curl -G -d state=open https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("curl --get --data state=open https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("curl -sG -d state=open https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("curl -Gd state=open https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        # gh's field flags can carry their value attached, and a field
        # switches the method to POST.
        ("gh api repos/o/r/pulls -ftitle=x", "pulls_collection", True),
        ("gh api repos/o/r/pulls -Ftitle=x", "pulls_collection", True),
        ("gh api --method GET repos/o/r/pulls -ftitle=x", "pulls_collection", False),
        # Long field flags carry their value with `=` too.
        ("gh api repos/o/r/pulls --field=title=x", "pulls_collection", True),
        ("gh api repos/o/r/pulls --raw-field=title=x", "pulls_collection", True),
        ("gh api repos/o/r/pulls --input=body.json", "pulls_collection", True),
        ("gh api --method GET repos/o/r/pulls --field=title=x", "pulls_collection", False),
        # A read-only option's OPERAND must not be read as an option: `-H` takes
        # a header, and `-XPOST` inside one is data (Codex review, PR #182).
        ("curl -H '-XPOST' 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("curl -H 'X-Y: z' -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        ("curl -XPOST https://api.github.com/repos/o/r/pulls -d @b",
         "pulls_collection", True),
        # curl documents that a repeated -X uses the LAST value; returning the
        # first read `-X POST -X GET` as a creation (Codex review, PR #182).
        ("curl -X POST -X GET 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("curl -X GET -X POST https://api.github.com/repos/o/r/pulls -d @b",
         "pulls_collection", True),
        # `-d` is curl's data but wget's --debug. Sharing curl's predicate with
        # every HTTP client turned a wget LISTING into a claimed creation.
        ("wget -d -O - 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("wget --post-data='{}' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        # A value-taking long option missing from the list lets its argument be
        # re-read as an option: `--url-query '-XPOST'` became a method.
        ("curl --url-query '-XPOST' 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("curl --aws-sigv4 '-XPOST' 'https://api.github.com/repos/o/r/pulls'",
         "pulls_collection", False),
        ("wget --output-file '-XPOST' -O - 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        # The point of the inverted default: an option this code has never
        # heard of consumes its operand, so it can cost a link but never
        # manufacture a claim (Codex review, PR #182).
        ("curl --some-future-option '-XPOST' 'https://api.github.com/repos/o/r/pulls'",
         "pulls_collection", False),
        # …while known boolean flags do NOT swallow the method after them.
        ("curl -s -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        ("curl -fsSL -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        ("curl --silent --location -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # SHORT options needed the same inverted default: the value table is
        # curl's and shared, so `curl -A` and `wget -P` were not in it and
        # their operands were re-read as a method (Codex review, PR #182).
        ("curl -A '-XPOST' 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("wget -P '-XPOST' -O - 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("curl -Z '-XPOST' 'https://api.github.com/repos/o/r/pulls'",
         "pulls_collection", False),
        ("curl -k -v -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # The DATA inference needed the same operand-aware walk as the method
        # scan: a header whose value looks like a flag is data, not a flag.
        ("curl -H '-d' 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("curl -A '--data' 'https://api.github.com/repos/o/r/pulls'",
         "pulls_collection", False),
        ("gh api --jq '-f' repos/o/r/pulls", "pulls_collection", False),
        ("wget --output-file '--post-data' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("curl -H 'X-Y: z' -d '{}' https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        # Operand rules are PER CLIENT: `-O` is curl's boolean --remote-name
        # but wget's value-taking --output-document, and `-p` is curl's
        # --proxytunnel but gh's --preview <strings> (Codex review, PR #182).
        ("wget -O '-XPOST' 'https://api.github.com/repos/o/r/pulls?per_page=1'",
         "pulls_collection", False),
        ("gh api -p '-XPOST' repos/o/r/pulls", "pulls_collection", False),
        # …and for curl, `-O` really does leave `-XPOST` as a method flag,
        # which is the same distinction seen from the other side.
        ("curl -O -XPOST https://api.github.com/repos/o/r/pulls",
         "pulls_collection", True),
        # `gh api --help` documents {owner}/{repo} placeholders and a query
        # string; rejecting both meant no context at all for those forms.
        ("gh api repos/{owner}/{repo}/pulls -f title=x", "pulls_collection", True),
        ("gh api repos/o/r/pulls?state=open", "pulls_collection", False),
        # The endpoint is an OPERAND. A URL handed to a read-only option is not
        # the destination: this posts to example.test (Codex review, PR #182).
        ("curl --referer https://api.github.com/repos/o/r/pulls -d url=x "
         "https://example.test/echo", None, False),
        ("curl -H 'Ref: https://api.github.com/repos/o/r/pulls' "
         "https://api.github.com/repos/o/r/pulls -d '{}'", "pulls_collection", True),
        # curl's documented `--url <url>`: the destination arrives as an
        # option's VALUE, which the operand walk correctly skips — so it has to
        # be read back deliberately (Codex review, PR #182).
        ("curl --url https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("curl --url https://api.github.com/repos/o/r/pulls -X POST -d '{}'",
         "pulls_collection", True),
        ("curl --url=https://api.github.com/repos/o/r/pulls -X POST -d '{}'",
         "pulls_collection", True),
        # One curl operation performs one transfer PER URL, so with two of them
        # nothing says which produced the response.
        ("curl -X POST -d @body -o /dev/null https://api.github.com/repos/o/a/pulls "
         "https://example.test/echo", "pulls_collection", False),
        ("curl -X POST -d @body https://api.github.com/repos/o/a/pulls "
         "https://api.github.com/repos/o/b/pulls", "pulls_collection", False),
        # Wrapper flags that take a separate operand.
        ("env -u DEBUG curl -X POST https://api.github.com/repos/o/r/pulls -d x",
         "pulls_collection", True),
        ("sudo -u ci curl -X POST https://api.github.com/repos/o/r/pulls -d x",
         "pulls_collection", True),
        ("timeout -s KILL 5 curl -X POST https://api.github.com/repos/o/r/pulls -d x",
         "pulls_collection", True),
        ("http POST https://api.github.com/repos/o/r/pulls title=x",
         "pulls_collection", True),
        ("xh POST https://api.github.com/repos/o/r/pulls title=x",
         "pulls_collection", True),
        ("http GET https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        # HTTPie's method and body items are OPERANDS: `--session POST` names a
        # session, and `--session ./foo=bar` a path (Codex review, PR #182).
        ("http --session POST https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("http --session ./foo=bar https://api.github.com/repos/o/r/pulls",
         "pulls_collection", False),
        ("http --session mine POST https://api.github.com/repos/o/r/pulls t=x",
         "pulls_collection", True),
        # HTTPie/xh default to GET with no data and POST with some. Only BODY
        # items count — `k==v` is a query param and `Header:value` a header,
        # and reading either as a body would turn a listing into a claimed
        # creation (Codex review, PR #182).
        ("http https://api.github.com/repos/o/r/pulls title=x head=f base=main",
         "pulls_collection", True),
        ("xh https://api.github.com/repos/o/r/pulls body:=@b.json", "pulls_collection", True),
        ("http https://api.github.com/repos/o/r/pulls", "pulls_collection", False),
        ("http https://api.github.com/repos/o/r/pulls state==open", "pulls_collection", False),
        ("http https://api.github.com/repos/o/r/pulls X-Api-Key:abc", "pulls_collection", False),
        ("http 'https://api.github.com/repos/o/r/pulls?state=open'", "pulls_collection", False),
        # An explicit verb still beats the inference.
        ("http GET https://api.github.com/repos/o/r/pulls title=x", "pulls_collection", False),
        # Two pulls calls in ONE shell call: the tool result is their combined
        # output and nothing says which produced the URL, so this can reach B2
        # but never the unconditional B1 (Codex review, PR #182).
        ("gh api --method POST repos/o/a/pulls >/dev/null && "
         "gh api --method GET repos/o/b/pulls/7 --jq .html_url",
         "pulls_collection", False),
        ("gh api repos/o/r/pulls", "pulls_collection", False),
        ("gh api repos/o/r/pulls/12", "pull_item", False),
        ("curl -X POST https://gh.corp/api/v3/repos/o/r/pulls -d '{}'",
         "pulls_collection", True),
        # Quoting the target is the normal way to write these, and blanking
        # quoted segments before looking for it used to delete it — a silent
        # miss on a real PR creation (Codex review, PR #182).
        ('curl -X POST "https://api.github.com/repos/O/R/pulls" -d \'{}\'',
         "pulls_collection", True),
        ("curl -X POST 'https://api.github.com/repos/O/R/pulls' -d '{}'",
         "pulls_collection", True),
        ("gh api --method POST 'repos/o/r/pulls' -f title=x", "pulls_collection", True),
        ('gh api --method POST "repos/o/r/pulls" -f title=x', "pulls_collection", True),
        ('curl "https://api.github.com/repos/O/R/pulls/12"', "pull_item", False),
        ("curl https://api.github.com/repos/o/r/issues", None, False),
        ('echo "https://api.github.com/repos/o/r/pulls"', None, False),
        ('grep "https://api.github.com/repos/o/r/pulls" f', None, False),
    ]
    for command, target, is_write in cases:
        got = pr_link.github_api_call(command)
        check(f"api: {command[:52]!r}", got == (target, is_write), repr(got))

    # A POST to the COLLECTION opens a PR; a POST to pulls/<n> is an edit.
    check("only a write to the collection creates a PR",
          pr_link.creates_pr("Bash", {"command":
              "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'"})
          and not pr_link.creates_pr("Bash", {"command":
              "curl -X POST https://api.github.com/repos/o/r/pulls/12 -d '{}'"})
          and not pr_link.creates_pr("Bash", {"command":
              "gh api repos/o/r/pulls"}))


def test_github_mcp_tools_are_recognised_by_their_server_segment():
    cases = [
        ("mcp__github__create_pull_request", True, True),
        ("mcp__github-mcp__open_pull_request", True, True),
        ("mcp__GitHub__createPullRequest", True, True),
        ("mcp__github__get_pull_request", True, False),
        ("mcp__github__list_pull_requests", True, False),
        # Reviewing someone else's PR is not opening it. Without an anchored
        # object these all read as "you created this pull request", and B1 tells
        # the reviewing session to record itself as the author of code it was
        # only reading (Codex review, PR #182).
        ("mcp__github__create_pull_request_review", True, False),
        ("mcp__github__create_pull_request_comment", True, False),
        ("mcp__github__submit_pull_request_review", True, False),
        ("mcp__github__create_pull_request_review_comment", True, False),
        ("mcp__github__create_and_submit_pull_request_review", True, False),
        # The tail check alone accepted these: they END in the right words
        # while creating something attached to the PR (Codex review, PR #182).
        ("mcp__github__create_review_for_pull_request", True, False),
        ("mcp__github__create_comment_on_pull_request", True, False),
        ("mcp__github__submit_review_for_pull_request", True, False),
        ("mcp__github__add_labels_to_pull_request", True, False),
        ("mcp__github__merge_pull_request", True, False),
        ("mcp__github__update_pull_request", True, False),
        # …and the real creation spellings still land on B1. `create_pr` is
        # here because `\bpr\b` never matched it — `_` is a word character, so
        # there is no boundary between `create_` and `pr`.
        ("mcp__github__create_draft_pull_request", True, True),
        ("mcp__github__create_pr", True, True),
        ("mcp__github__create_prs", True, True),
        ("mcp__github__newPullRequest", True, True),
        # The server segment still gates everything: a non-GitHub server that
        # happens to expose a create_pull_request tool is not GitHub.
        ("mcp__notes__create_pull_request", False, False),
        # …but a server whose NAME contains underscores is still GitHub.
        # `[^_]*` stopped at the first one and rejected every tool from
        # `github_enterprise`, or from any plugin-provided server — this repo's
        # own tools arrive as `mcp__plugin_memhub-staging_memhub__…`
        # (Codex review, PR #182).
        ("mcp__github_enterprise__create_pull_request", True, True),
        ("mcp__plugin_github_github__create_pull_request", True, True),
        ("mcp__github_enterprise__get_pull_request", True, False),
        # The SERVER segment is not GitHub — this is a note-taking tool.
        ("mcp__notes__github_summary", False, False),
        ("Bash", False, False),
    ]
    for name, touches, creates in cases:
        check(f"mcp touches: {name}", pr_link.touches_github(name, {}) is touches)
        check(f"mcp creates: {name}", pr_link.creates_pr(name, {}) is creates)


def test_an_enterprise_create_resolves_its_own_host():
    """`github_api_call` accepts an enterprise REST target, but
    `urls_from_output_text` only knows `github.com` — so the enterprise create
    was detected and then produced no URL, and the hook went silent on exactly
    the calls it had decided to care about (Codex review, PR #182)."""
    command = "curl -X POST https://ghe.corp/api/v3/repos/o/r/pulls -d '{\"title\":\"x\"}'"
    body = {"stdout": '{"html_url": "https://ghe.corp/o/r/pull/7", '
                      '"issue_url": "https://ghe.corp/api/v3/repos/o/r/issues/7"}',
            "stderr": ""}
    check("the host comes from the command",
          pr_link.github_api_host(command) == "ghe.corp")
    for flag in ("gh api --hostname ghe.corp --method POST repos/o/r/pulls -f t=x",
                 "gh api --hostname=ghe.corp --method POST repos/o/r/pulls -f t=x"):
        check(f"…including from --hostname: {flag[:38]!r}",
              pr_link.github_api_host(flag) == "ghe.corp")
    check("a github.com command names no extra host",
          pr_link.github_api_host(
              "curl https://api.github.com/repos/o/r/pulls") is None)
    got = pr_link.context_for_call(
        "Bash", {"command": command}, body, "s1",
        checker=lambda _u: {"enabled": True, "github_connected": True,
                            "repo_in_install": True})
    check("an enterprise create reaches B1",
          bool(got) and "without asking" in got, str(got)[:120])
    # The host is taken from the COMMAND, never wildcarded over the response —
    # `https://<any host>/<o>/<r>/pull/<n>` would match unrelated sites.
    check("an unrelated host in the output of a github.com call is ignored",
          pr_link.pr_url_from_response(
              {"stdout": "https://evil.example/o/r/pull/7"},
              host=pr_link.github_api_host("gh pr view 7")) is None)
    check("github.com extraction is unchanged",
          pr_link.pr_url_from_response({"stdout": "https://github.com/o/r/pull/7"})
          == "https://github.com/o/r/pull/7")


def test_an_enterprise_mcp_create_reads_its_host_from_the_result():
    """The MCP lane has no shell command, so `github_api_host` has nothing to
    read and a GHES server's create was recognised and then dropped. The host
    comes from the reply's own `html_url` FIELD — an MCP result is the
    structured answer of a server whose name had to say "github" to get here,
    unlike a shell stdout, which can contain anything (Codex, PR #182)."""
    connected = {"enabled": True, "github_connected": True, "repo_in_install": True}
    ghes = {"content": [{"type": "text", "text": json.dumps({
        "html_url": "https://ghe.corp/o/r/pull/7",
        "issue_url": "https://ghe.corp/api/v3/repos/o/r/issues/7"})}]}
    got = pr_link.context_for_call("mcp__github__create_pull_request", {"title": "x"},
                                   ghes, "s1", checker=lambda _u: connected)
    check("a GHES MCP create reaches B1",
          bool(got) and "you just opened" in got, str(got)[:120])
    check("…naming the enterprise PR", bool(got) and "ghe.corp/o/r/pull/7" in got)
    check("the host is read from html_url, not from free text",
          pr_link._mcp_result_host(
              {"content": [{"type": "text",
                            "text": "see https://evil.example/o/r/pull/9"}]}) is None)
    check("a github.com MCP result names no extra host",
          pr_link._mcp_result_host({"content": [{"type": "text", "text": json.dumps(
              {"html_url": "https://github.com/o/r/pull/7"})}]}) is None)


def test_a_gh_pr_command_may_report_an_enterprise_url_itself():
    """`gh` infers its host from the repository's remote, so plain
    `gh pr create --fill` on GHES names the host NOWHERE in the command. Its
    own output is the only place it appears (Codex review, PR #182)."""
    connected = {"enabled": True, "github_connected": True, "repo_in_install": True}

    def ctx(command, response):
        return pr_link.context_for_call("Bash", {"command": command}, response,
                                        "s1", checker=lambda _u: connected) or ""

    ghes = {"stdout": "https://ghe.corp/o/r/pull/7", "stderr": "", "exit_code": 0}
    check("a plain gh pr create on GHES reaches B1",
          "you just opened" in ctx("gh pr create --fill", ghes))
    check("…and a gh pr view on GHES reaches B2",
          "IF IT WAS NOT" in ctx("gh pr view 7", ghes))
    # Accepting any host is scoped to the `gh pr` lane, because there the text
    # is gh's own output. Everything else still takes its host from the command.
    check("a non-gh command gets no host-agnostic parsing",
          ctx("cat notes.md", ghes) == "")
    check("…and two URLs still silence it",
          ctx("gh pr create --fill",
              {"stdout": "https://ghe.corp/o/r/pull/7 https://ghe.corp/o/r/pull/8",
               "exit_code": 0}) == "")
    check("github.com behaviour is unchanged",
          "you just opened" in ctx("gh pr create --fill",
                                   {"stdout": "https://github.com/o/r/pull/7",
                                    "exit_code": 0}))


def test_a_quoted_api_target_is_still_found_but_a_quoted_mention_is_not():
    """The two halves of the quoting rule, which pull against each other.

    Quotes must not hide a real target (`curl -X POST "…/pulls"`), and must
    still stop a mention from looking like a call (`grep "…/pulls" f`). What
    separates them is WHO is at command position, which is asked of the
    blanked text; the target is then looked for in the dequoted text.
    """
    for command in (
        'curl -X POST "https://api.github.com/repos/o/r/pulls" -d \'{"title":"x"}\'',
        "gh api --method POST 'repos/o/r/pulls' -f title=x",
    ):
        check(f"a quoted target still creates: {command[:44]!r}",
              pr_link.creates_pr("Bash", {"command": command})
              and pr_link.touches_github("Bash", {"command": command}))
    for command in (
        'grep "https://api.github.com/repos/o/r/pulls" f',
        'echo "https://api.github.com/repos/o/r/pulls"',
        "echo 'gh api repos/o/r/pulls'",
        'rg "api.github.com/repos/o/r/pulls" .',
    ):
        check(f"a quoted mention is still not a call: {command[:44]!r}",
              not pr_link.touches_github("Bash", {"command": command}))


def test_the_cache_scope_covers_enterprise_repos_too():
    """A github.com-only scope parser gave every enterprise URL an empty scope,
    so they shared one cache file and one disconnected enterprise repo silenced
    linking for all the others (Codex review, PR #182)."""
    scopes = [pr_link._repo_of(u) for u in (
        "https://github.com/o/r/pull/1",
        "https://ghe.corp/o/r/pull/1",
        "https://ghe.corp/other/repo/pull/9",
        "https://ghe2.corp/o/r/pull/1")]
    check("every PR URL gets a scope", all(scopes), str(scopes))
    check("…and they are all distinct across host, owner and repo",
          len(set(scopes)) == 4, str(scopes))
    check("a non-PR URL still has no scope", pr_link._repo_of("nonsense") == "")


def test_the_negative_cache_is_scoped_to_the_repo_not_the_deployment():
    """`enabled` and `github_connected` are per-ORG, and one person can be in
    several on one backend. Keying on the api_base alone let one disconnected
    org silence linking for every other org's PRs for 24h, with no request
    (Codex review, PR #182)."""
    calls: list[str] = []
    disconnected = {"enabled": True, "github_connected": False,
                    "connect_url": "https://app.example.test/i"}

    def rest(url, *a, **k):
        calls.append(url)
        return _Reply(200, disconnected)

    with tempfile.TemporaryDirectory() as td:
        pr_link.STATE_DIR = Path(td) / "prlink"
        _with_stub(rest, lambda: pr_link.check("https://github.com/orgA/x/pull/1"))
        check("the disconnected org's answer is cached", len(calls) == 1)
        _with_stub(rest, lambda: pr_link.check("https://github.com/orgA/x/pull/2"))
        check("…and reused for another PR in the SAME repo", len(calls) == 1, str(calls))
        _with_stub(rest, lambda: pr_link.check("https://github.com/orgB/y/pull/1"))
        check("…but a DIFFERENT repo asks the server again",
              len(calls) == 2, str(calls))
    pr_link.STATE_DIR = Path(_HOME) / ".config" / "memhub-plugin" / "prlink"


def test_a_newline_separates_commands_like_a_semicolon():
    """`\n` was in the punctuation set but shlex's WHITESPACE rule consulted
    first and swallowed it — so a multi-line command, which agents write
    constantly, collapsed into ONE segment and every segment-based guard here
    silently did nothing on it (Codex review, PR #182)."""
    tokens = pr_link._tokens("gh pr create --fill\ntrue")
    check("a newline survives tokenisation", "\n" in (tokens or []), str(tokens))
    check("…and splits the command in two",
          len(pr_link._segments(tokens)) == 2, str(pr_link._segments(tokens)))
    connected = {"enabled": True, "github_connected": True, "repo_in_install": True}

    def b1(command):
        got = pr_link.context_for_call(
            "Bash", {"command": command},
            {"stdout": "https://github.com/o/r/pull/5", "stderr": "", "exit_code": 0},
            "s1", checker=lambda _u: connected) or ""
        return "you just opened" in got

    # `true` after a newline makes the tool report ITS status, so a failed
    # create looks successful and its stderr still holds the existing PR's URL.
    check("a newline follower blocks the self-link",
          not b1("gh pr create --fill\ntrue"))
    check("…as does any other newline follower",
          not b1("gh pr create --fill\ncat /tmp/url"))
    check("a newline BEFORE the create is harmless",
          b1("cd /repo\ngh pr create --fill"))
    check("…including a multi-line prelude",
          b1("git add -A\ngit commit -m x\ngh pr create --fill"))


def test_a_chained_gh_pr_create_cannot_claim_the_other_prs_url():
    """`gh pr create >/dev/null && gh pr view 99` opens one pull request and
    prints another's. The REST lane had this guard; the `gh pr` lane, which
    used a whole-command regex, did not (Codex review, PR #182)."""
    for command in (
        "gh pr create --fill >/dev/null && gh pr view 99 --json url -q .url",
        # …and across the two lanes at once.
        "gh pr create --fill && curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
    ):
        check(f"ambiguous, so no self-link: {command[:46]!r}",
              pr_link.touches_github("Bash", {"command": command})
              and not pr_link.creates_pr("Bash", {"command": command}))
    for command in ("gh pr create --fill",
                    "cd .. && gh pr create",
                    # `pr` here is a quoted TITLE value, not the subcommand.
                    'gh pr create --title "pr" --body b'):
        check(f"one create is still a create: {command[:40]!r}",
              pr_link.creates_pr("Bash", {"command": command}))


def test_a_create_that_FAILED_opened_nothing():
    """`gh pr create` on a branch that already has one prints THAT pull
    request's URL to stderr and exits non-zero; an MCP failure returns
    `isError` with the same shape. Either way B1 would have recorded a
    confirmed `session_self` link to a pull request somebody else opened.
    `pr_babysit_trigger` guards this exact case (Codex review, PR #182)."""
    connected = {"enabled": True, "github_connected": True, "repo_in_install": True}
    url = "https://github.com/o/r/pull/5"

    def ctx(response, tool="Bash", payload=None):
        return pr_link.context_for_call(
            tool, payload if payload is not None else {"command": "gh pr create --fill"},
            response, "s1", checker=lambda _u: connected) or ""

    for label, response in (
        ("exit_code", {"stdout": "", "stderr": f"already exists:\n{url}", "exit_code": 1}),
        ("is_error", {"stdout": "", "stderr": url, "is_error": True}),
        ("success:false", {"stdout": "", "stderr": url, "success": False}),
    ):
        got = ctx(response)
        check(f"a failed create does not self-link ({label})",
              "you just opened" not in got, got[:110])
        check(f"…it falls to B2, where the model judges ({label})",
              "IF IT WAS NOT" in got, got[:110])
    for label, response in (
        ("exit_code 0", {"stdout": url, "stderr": "", "exit_code": 0}),
        ("no status fields at all", {"stdout": url, "stderr": ""}),
    ):
        check(f"a successful create still self-links ({label})",
              "you just opened" in ctx(response))
    # MCP spells it `isError`, which pr_provenance's helper does not know.
    body = {"content": [{"type": "text", "text": json.dumps({"html_url": url})}]}
    check("a failed MCP create does not self-link",
          "you just opened" not in ctx(dict(body, isError=True),
                                       "mcp__github__create_pull_request", {"title": "x"}))
    check("…but a successful one does",
          "you just opened" in ctx(body, "mcp__github__create_pull_request", {"title": "x"}))


def test_curl_next_is_a_second_request_not_a_second_flag():
    """curl takes several requests in one invocation, separated by `--next`,
    each with its own options. Treating the whole call as one request took the
    first target with the first operation's method — so a create followed by a
    read of a DIFFERENT pull request claimed the create had produced the
    second one's URL (Codex review, PR #182)."""
    command = ("curl -X POST https://api.github.com/repos/o/a/pulls -d @body "
               "-o /dev/null --next https://api.github.com/repos/o/b/pulls/7")
    check("both operations are seen", len(pr_link._api_matches(command)) == 2,
          str(pr_link._api_matches(command)))
    check("it still counts as addressing GitHub",
          pr_link.touches_github("Bash", {"command": command}))
    check("…but cannot claim to have opened the PR that came back",
          not pr_link.creates_pr("Bash", {"command": command}))
    check("a single-operation POST is unaffected",
          pr_link.creates_pr("Bash", {"command":
              "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'"}))


def test_gh_pr_new_is_an_alias_for_create():
    for command in ("gh pr new --fill", "cd .. && gh pr new"):
        check(f"{command!r} creates", pr_link.creates_pr("Bash", {"command": command}))
    check("gh pr view is still not a create",
          not pr_link.creates_pr("Bash", {"command": "gh pr view 12"}))


def test_gh_inherited_flags_and_dry_run():
    """`gh`'s inherited flags sit between `pr` and its subcommand, and
    `--dry-run` prints the pull request it WOULD open (Codex review, PR #182)."""
    for command in ("gh pr -R o/r create --fill", "gh pr --repo o/r new",
                    "gh pr -R ghe.corp/o/r create --fill"):
        check(f"an inherited flag does not hide the subcommand: {command[:40]!r}",
              pr_link.creates_pr("Bash", {"command": command}))
    check("…and the subcommand is still read correctly",
          not pr_link.creates_pr("Bash", {"command": "gh pr -R o/r view 12"}))
    # A dry run opens nothing; if its proposed body quotes a PR URL, B1 would
    # have claimed authorship of THAT pull request.
    for command in ("gh pr create --fill --dry-run",
                    # gh accepts the attached boolean spellings too.
                    "gh pr create --fill --dry-run=true",
                    "gh pr create --fill --dry-run=TRUE"):
        check(f"not a creation: {command[24:]!r}",
              not pr_link.creates_pr("Bash", {"command": command}))
    for command in ("gh pr create --fill --dry-run=false",
                    "gh pr create --fill --dry-run=0"):
        check(f"…but an explicitly disabled dry run IS: {command[24:]!r}",
              pr_link.creates_pr("Bash", {"command": command}))
    # …including when the command will not tokenise at all. ANSI-C quoting
    # ($'…') defeats shlex, and the guard sat AFTER that fallback returned,
    # so the dry run claimed authorship of whatever its details mentioned.
    unparseable = "gh pr create --dry-run --body $'can\\'t x'"
    check("the command really is unparseable", pr_link._tokens(unparseable) is None)
    check("…and --dry-run is still not a creation",
          not pr_link.creates_pr("Bash", {"command": unparseable}))
    check("…while an unparseable real create still is",
          pr_link.creates_pr("Bash", {"command": "gh pr create --fill --body $'can\\'t x'"}))
    check("…while a real create is unaffected",
          pr_link.creates_pr("Bash", {"command": "gh pr create --fill"}))


def test_an_enterprise_gh_pr_create_recovers_its_host():
    """`gh -R [HOST/]OWNER/REPO` is the ordinary GHES `gh pr` path, and with no
    REST target in the command there was nothing to learn the host from, so the
    link stayed silent (Codex review, PR #182)."""
    check("the host comes from -R",
          pr_link.github_api_host("gh -R ghe.corp/o/r pr create --fill") == "ghe.corp")
    check("…and from --repo=", 
          pr_link.github_api_host("gh --repo=ghe.corp/o/r pr create") == "ghe.corp")
    check("a two-segment -R names no host",
          pr_link.github_api_host("gh -R o/r pr create --fill") is None)
    # `gh help environment`: GH_HOST is the hostname for commands that name no
    # other, and it is the ordinary way to point gh at GHES.
    check("GH_HOST names the host",
          pr_link.github_api_host("GH_HOST=ghe.corp gh pr create --fill") == "ghe.corp")
    # …in the bare-endpoint api lane too, not just `gh pr`.
    check("…including for `gh api`",
          pr_link.github_api_host(
              "GH_HOST=ghe.corp gh api --method POST repos/o/r/pulls -f t=x") == "ghe.corp")
    check("…and a github.com `gh api` still names no extra host",
          pr_link.github_api_host("gh api --method POST repos/o/r/pulls -f t=x") is None)
    check("…but GH_HOST=github.com is not an enterprise host",
          pr_link.github_api_host("GH_HOST=github.com gh pr create --fill") is None)
    got = pr_link.context_for_call(
        "Bash", {"command": "gh -R ghe.corp/o/r pr create --fill"},
        {"stdout": "https://ghe.corp/o/r/pull/7\n", "stderr": "", "exit_code": 0},
        "s1", checker=lambda _u: {"enabled": True, "github_connected": True,
                                  "repo_in_install": True})
    check("an enterprise gh pr create reaches B1",
          bool(got) and "you just opened" in got and "ghe.corp/o/r/pull/7" in got,
          str(got)[:140])


def test_b1_needs_the_url_to_be_provably_the_creates_own():
    """Counting recognised invocations is not enough — ANY segment can print a
    URL. `gh pr create >/dev/null && cat /tmp/pr-url` shows one that provably
    is not the create's (Codex review, PR #182)."""
    for command in (
        "gh pr create --fill >/dev/null && cat /tmp/pr-url",   # redirected away
        "gh pr create --fill > out.txt",
        "gh pr create --fill 2>&1 | tee log",
        "gh pr create --fill ; cat /tmp/pr-url",               # runs even if it failed
        "gh pr create --fill || cat /tmp/pr-url",
        "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}' > /dev/null",
        # `&` BACKGROUNDS the create, so the tool reports whatever ran next —
        # `& wait` exits 0 even when the create failed and its stderr still
        # holds the existing PR's URL (Codex review, PR #182).
        "gh pr create --fill & wait",
        "gh pr create --fill & echo done",
        "gh pr create --fill &",                       # trailing: no next segment
        "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}' &",
    ):
        check(f"no self-link when the URL's source is uncertain: {command[:44]!r}",
              pr_link.touches_github("Bash", {"command": command})
              and not pr_link.creates_pr("Bash", {"command": command}))
    # `&&` proves the create succeeded, so its URL IS in the output — and a
    # second URL would trip the exactly-one rule into silence anyway. A
    # pipeline carries the create's own stdout onward.
    # A PIPELINE is not safe after all: without `set -o pipefail` the call
    # reports the LAST command's status, so `gh pr create | tee log` exits 0
    # even when the create failed because the PR already exists — and its
    # stderr still carries THAT pull request's URL (Codex review, PR #182).
    check("a pipeline can hide a failed create, so it declines",
          not pr_link.creates_pr("Bash", {"command": "gh pr create --fill | tee log"}))
    # …while a separator BEFORE the create cannot supply the URL or mask the
    # status, so it must NOT downgrade — that broke the documented guarantee
    # that a session opening a pull request always links itself.
    for command in ("gh pr create --fill",
                    "cd .. && gh pr create --fill",
                    "cd /repo; gh pr create --fill",
                    "(cd sub && gh pr create)",
                    "echo hi; curl -X POST https://api.github.com/repos/o/r/pulls -d @b",
                    "git add -A && git commit -m x && gh pr create --fill"):
        check(f"…and a create still links: {command[:44]!r}",
              pr_link.creates_pr("Bash", {"command": command}))


def test_an_ambiguous_multi_target_call_can_never_self_link():
    """One stdout, two pulls calls, no way to say which made the URL."""
    command = ("gh api --method POST repos/o/a/pulls >/dev/null && "
               "gh api --method GET repos/o/b/pulls/7 --jq .html_url")
    check("it still counts as addressing GitHub",
          pr_link.touches_github("Bash", {"command": command}))
    check("…but it cannot claim to have opened anything",
          not pr_link.creates_pr("Bash", {"command": command}))
    got = pr_link.context_for_call(
        "Bash", {"command": command},
        {"stdout": "https://github.com/o/b/pull/7"}, "s1",
        checker=lambda _u: {"enabled": True, "github_connected": True,
                            "repo_in_install": True})
    # B2 also contains "without asking" (inside its *conditional* branch), so
    # the thing that separates the two is B1's opening claim.
    check("…so the model judges instead of self-linking",
          bool(got) and "IF IT WAS NOT" in got and "you just opened" not in got,
          str(got)[:160])


def test_the_gate_is_acting_on_github_not_mentioning_a_pr():
    response = {"stdout": "see https://github.com/o/r/pull/7 for context\n"}
    check("a `cat` whose output names a PR does not touch GitHub",
          not pr_link.touches_github("Bash", {"command": "cat CHANGELOG.md"}))
    check("…so it produces no context at all",
          pr_link.context_for_call("Bash", {"command": "cat CHANGELOG.md"},
                                   response, "s1",
                                   checker=lambda _u: {"enabled": True,
                                                       "github_connected": True}) is None)


def test_pr_url_from_response_requires_exactly_one():
    one = "https://github.com/o/r/pull/12"
    check("one URL on stdout", pr_link.pr_url_from_response({"stdout": one}) == one)
    check("one URL on stderr only",
          pr_link.pr_url_from_response(
              {"stdout": "", "stderr": f"a pull request already exists: {one}"}) == one)
    check("a bare string response is scanned",
          pr_link.pr_url_from_response(one + "\n") == one)
    check("three URLs (a `gh pr list`) → None",
          pr_link.pr_url_from_response({"stdout": "\n".join(
              f"https://github.com/o/r/pull/{n}" for n in (1, 2, 3))}) is None)
    check("no URL → None", pr_link.pr_url_from_response({"stdout": "branch-name\n"}) is None)
    check("a non-string stdout yields nothing",
          pr_link.pr_url_from_response({"stdout": {"nested": one}}) is None)
    check("an #issuecomment suffix still resolves to the PR",
          pr_link.pr_url_from_response(
              {"stdout": one + "#issuecomment-99"}) == one)
    check("a non-dict, non-string response → None",
          pr_link.pr_url_from_response(12) is None)


def test_a_create_response_body_resolves_to_its_html_url():
    # The PR object's other URLs are all api.github.com (which _PR_URL_RE does
    # not match) and head.repo.html_url has no /pull/ segment — so the
    # exactly-one rule resolves a create response with no special casing.
    body = {
        "html_url": "https://github.com/octo/hello/pull/1347",
        "issue_url": "https://api.github.com/repos/octo/hello/issues/1347",
        "comments_url": "https://api.github.com/repos/octo/hello/issues/1347/comments",
        "review_comments_url": "https://api.github.com/repos/octo/hello/pulls/1347/comments",
        "_links": {"self": {"href": "https://api.github.com/repos/octo/hello/pulls/1347"}},
        "head": {"repo": {"html_url": "https://github.com/octo/hello"}},
    }
    curl = {"stdout": json.dumps(body), "stderr": ""}
    check("a curl POST response body → the html_url",
          pr_link.pr_url_from_response(curl) == "https://github.com/octo/hello/pull/1347")
    mcp = {"content": [{"type": "text", "text": json.dumps(body)}]}
    check("an MCP create result → the same",
          pr_link.pr_url_from_response(mcp) == "https://github.com/octo/hello/pull/1347")


def test_conversation_id_is_namespaced_once_per_host():
    check("claude → the bare id",
          pr_link.conversation_id_for("claude", "abc-123") == "abc-123")
    check("codex → prefixed",
          pr_link.conversation_id_for("codex", "01J") == "codex-01J")
    check("cursor → prefixed",
          pr_link.conversation_id_for("cursor", "u1") == "cursor-u1")
    check("an already-namespaced id is not double-prefixed",
          pr_link.conversation_id_for("codex", "codex-01J") == "codex-01J")
    check("an unknown host is left bare",
          pr_link.conversation_id_for("zed", "x") == "x")
    for empty in (None, "", "   ", 12):
        check(f"no session id → None ({empty!r})",
              pr_link.conversation_id_for("claude", empty) is None)


class _Reply:
    def __init__(self, status, data):
        self.status, self.data, self.etag = status, data, None


def _with_stub(rest, fn):
    real_rest, real_resolve = mcp_http.rest, None
    import _memhub_auth
    real_resolve = _memhub_auth.resolve_bearer
    mcp_http.rest = rest
    _memhub_auth.resolve_bearer = lambda url=None, refresh=True: (
        "https://api.example.test/mcp/", "mhk_test")
    try:
        return fn()
    finally:
        mcp_http.rest = real_rest
        _memhub_auth.resolve_bearer = real_resolve


def test_check_degrades_to_none_and_never_raises():
    good = {"enabled": True, "github_connected": True, "repo_in_install": True}
    check("200 → the dict",
          _with_stub(lambda *a, **k: _Reply(200, good),
                     lambda: pr_link.check("https://github.com/o/r/pull/1")) == good)
    for label, rest in (
        ("500", lambda *a, **k: (_ for _ in ()).throw(mcp_http.McpError("boom", 500))),
        ("timeout", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())),
        ("garbage body", lambda *a, **k: _Reply(200, "not a dict")),
        ("non-200", lambda *a, **k: _Reply(204, None)),
        # A reply object that is not shaped like one is one more reason to be
        # silent, not a traceback in the middle of someone's session.
        ("a reply that is not a RestReply", lambda *a, **k: "not a reply"),
    ):
        check(f"{label} → None, no raise",
              _with_stub(rest, lambda: pr_link.check(
                  "https://github.com/o/r/pull/1")) is None)

    import _memhub_auth
    real = _memhub_auth.resolve_bearer
    _memhub_auth.resolve_bearer = lambda url=None, refresh=True: ("u", None)
    try:
        check("no credential → None, and no request is made",
              pr_link.check("https://github.com/o/r/pull/1") is None)
    finally:
        _memhub_auth.resolve_bearer = real


def test_the_pr_url_cannot_add_a_query_parameter():
    """The URL comes from untrusted tool output; it must stay one value."""
    seen: list[str] = []

    def rest(url, *a, **k):
        seen.append(url)
        return _Reply(200, {"enabled": False})

    with tempfile.TemporaryDirectory() as td:
        pr_link.STATE_DIR = Path(td) / "prlink"
        _with_stub(rest, lambda: pr_link.check(
            "https://github.com/o/r/pull/7?admin=1&x=2#frag"))
    url = seen[0] if seen else ""
    check("exactly one '?' — the PR URL's own is escaped", url.count("?") == 1, url)
    check("no unescaped '&' or '#' reaches the query",
          "&" not in url and "#" not in url, url)
    check("the whole PR URL is percent-encoded as one value",
          "pr_url=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F7%3Fadmin%3D1" in url, url)
    pr_link.STATE_DIR = Path(_HOME) / ".config" / "memhub-plugin" / "prlink"


def test_a_hostile_or_unwritable_cache_is_never_fatal():
    disconnected = {"enabled": True, "github_connected": False}
    with tempfile.TemporaryDirectory() as td:
        pr_link.STATE_DIR = Path(td) / "prlink"
        pr_link.STATE_DIR.mkdir(parents=True)
        path = pr_link._cache_path("https://api.example.test")
        for label, body in (("corrupt json", "{ not json"), ("a list", "[1,2]"),
                            ("no 'at'", '{"answer": {}}'),
                            ("'at' is a string", '{"at": "yesterday", "answer": {}}'),
                            ("answer is not a dict", '{"at": 1, "answer": "x"}'),
                            ("empty", "")):
            path.write_text(body, encoding="utf-8")
            check(f"{label} is ignored, not fatal",
                  pr_link._cached_negative("https://api.example.test", time.time()) is None)

        # A clock that moved backwards must not pin a stale answer forever.
        pr_link._store_negative("https://api.example.test", disconnected, time.time())
        check("a fresh entry is used",
              pr_link._cached_negative("https://api.example.test", time.time()) is not None)
        check("a clock jumped a week BACK does not read the cache",
              pr_link._cached_negative("https://api.example.test",
                                       time.time() - 7 * 86400) is None)
        check("past the TTL it is not read either",
              pr_link._cached_negative("https://api.example.test",
                                       time.time() + 25 * 3600) is None)

    # An unwritable state directory must not take the hook down with it.
    with tempfile.TemporaryDirectory() as td:
        parent = Path(td) / "ro"
        parent.mkdir()
        pr_link.STATE_DIR = parent / "prlink"
        os.chmod(parent, 0o500)
        try:
            pr_link._store_negative("https://api.example.test", disconnected, time.time())
            pr_link.breadcrumb("probe", RuntimeError("x"))
            check("an unwritable state dir raises nothing", True)
        except Exception as exc:  # noqa: BLE001
            check("an unwritable state dir raises nothing", False, repr(exc))
        finally:
            os.chmod(parent, 0o700)
    pr_link.STATE_DIR = Path(_HOME) / ".config" / "memhub-plugin" / "prlink"


def test_only_the_negative_org_answer_is_cached():
    calls: list[str] = []
    disconnected = {"enabled": True, "github_connected": False,
                    "connect_url": "https://app.example.test/i"}

    def rest(url, *a, **k):
        calls.append(url)
        return _Reply(200, disconnected)

    with tempfile.TemporaryDirectory() as td:
        pr_link.STATE_DIR = Path(td) / "prlink"
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/1"))
        check("a disconnected reply is written to the cache",
              len(list((Path(td) / "prlink").glob("*.json"))) == 1)
        again = _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/2"))
        check("…and a second call inside the TTL makes no request",
              len(calls) == 1 and again == disconnected, str(calls))

        # A connected reply changes constantly (linked_sessions, pr.known) and
        # is never cached.
        calls.clear()
        pr_link.STATE_DIR = Path(td) / "prlink2"
        connected = {"enabled": True, "github_connected": True, "repo_in_install": True}
        _with_stub(lambda *a, **k: (calls.append(1), _Reply(200, connected))[1],
                   lambda: pr_link.check("https://github.com/o/r/pull/1"))
        check("a connected reply writes nothing",
              not (Path(td) / "prlink2").exists()
              or not list((Path(td) / "prlink2").glob("*.json")))

        # A corrupt cache file is ignored, not fatal.
        pr_link.STATE_DIR = Path(td) / "prlink3"
        pr_link.STATE_DIR.mkdir(parents=True)
        for path in ((Path(td) / "prlink3" / f)
                     for f in ("a.json",)):
            path.write_text("{ not json", encoding="utf-8")
        calls.clear()
        got = _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/9"))
        check("a corrupt cache file is ignored, not fatal",
              got == disconnected and len(calls) == 1)
    pr_link.STATE_DIR = Path(_HOME) / ".config" / "memhub-plugin" / "prlink"


def test_the_contexts_say_the_right_thing_for_each_case():
    url = "https://github.com/o/r/pull/7"
    base = {"enabled": True, "github_connected": True, "repo_in_install": True,
            "pr": {"repo_full_name": "o/r", "pr_number": 7, "state": "open"},
            "linked_sessions": [{"session_id": "a", "is_mine": True},
                                {"session_id": "b", "is_mine": False}]}

    created = pr_link.context_for(base, url, "s1", created=True)
    check("B1 tells the agent to link without asking",
          "without asking" in created and 'session_ids=["s1"]' in created
          and 'link_source="session_self"' in created, created)
    check("B1 carries no authorship conditional",
          "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" not in created)
    check("B1 names what is already linked",
          "Already linked: 2 sessions (1 of them this user's)." in created, created)
    check("B1 names the PR", f"{url}" in created and "(o/r#7" in created, created)

    in_play = pr_link.context_for(base, url, "s1", created=False)
    check("B2 makes the model judge",
          "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION" in in_play
          and "IF IT WAS NOT" in in_play, in_play)
    check("B2 ends with the already-linked escape that keeps a babysit loop quiet",
          in_play.rstrip().endswith("say nothing at all."), in_play)
    check("B2 offers the finder and forbids running it unasked",
          "/memhub:find-contributing-sessions" in in_play
          and "without the user saying yes" in in_play)

    disconnected = pr_link.context_for(
        {"enabled": True, "github_connected": False,
         "connect_url": "https://app.example.test/i"}, url, "s1", created=True)
    check("A names the connect URL and says it once",
          "https://app.example.test/i" in disconnected
          and "ONCE per session" in disconnected, disconnected)
    check("A never tells the agent to link", "link_pr" not in disconnected)

    not_installed = pr_link.context_for(
        {"enabled": True, "github_connected": True, "repo_in_install": False,
         "connect_url": "https://app.example.test/i",
         "pr": {"repo_full_name": "o/r", "pr_number": 7}}, url, "s1", created=True)
    check("the repo variant of A names the repo",
          "o/r isn't part of the install" in not_installed, not_installed)

    check("enabled:false is total silence",
          pr_link.context_for({"enabled": False, "github_connected": True},
                              url, "s1", created=True) is None)
    check("no linked sessions → no already-linked line",
          "Already linked" not in pr_link.context_for(
              {"enabled": True, "github_connected": True, "repo_in_install": True},
              url, "s1", created=False))


def test_context_for_call_wires_the_gates_together():
    connected = {"enabled": True, "github_connected": True, "repo_in_install": True}
    url = "https://github.com/o/r/pull/7"

    def checker(_url):
        return connected

    got = pr_link.context_for_call("Bash", {"command": "cd .. && gh pr create"},
                                   {"stdout": url}, "s1", checker=checker)
    check("a chained create still reaches B1", got and "without asking" in got)
    got = pr_link.context_for_call("Bash", {"command": "gh pr view 7"},
                                   {"stdout": url}, "s1", checker=checker)
    check("a view reaches B2", got and "IF IT WAS NOT" in got)
    got = pr_link.context_for_call("Bash", {"command": "gh pr create"},
                                   {"stdout": url}, "s1", host="codex", checker=checker)
    check("the host namespaces the session id", got and '["codex-s1"]' in got, got)
    check("a server that never answers is silence",
          pr_link.context_for_call("Bash", {"command": "gh pr view 7"},
                                   {"stdout": url}, "s1",
                                   checker=lambda _u: None) is None)
    check("no PR URL is silence",
          pr_link.context_for_call("Bash", {"command": "gh pr list"},
                                   {"stdout": "nothing here"}, "s1",
                                   checker=checker) is None)


if __name__ == "__main__":
    print("pr_link")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all pr_link checks passed")
    sys.exit(1 if failures else 0)
