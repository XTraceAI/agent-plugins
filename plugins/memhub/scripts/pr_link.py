#!/usr/bin/env python3
"""Session ↔ pull-request linking: the detectors, the one server question, and
the texts the hook injects.

Stdlib only, no network at import, every public function pure except
``check()``. The hook entry point is ``pr_link_trigger.py``; the two skills
(`/memhub:link-pr`, `/memhub:find-contributing-sessions`) and
``capture.py current`` share ``conversation_id_for`` from here so a host cannot
silently stop linking because one call site spelled a prefix differently.

**Two questions, not one** (spec §4.1). ``touches_github`` asks whether this
tool call addressed GitHub at all — it is the gate, and without it a `cat
CHANGELOG.md` whose text mentions a PR would inject linking context about a
pull request nobody is working on. ``creates_pr`` asks whether the call
*opened* the pull request, which selects an unconditional self-link (B1) over
a judgment the model makes (B2). They are independent, and ``creates_pr``
implies ``touches_github`` for every input.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_provenance  # noqa: E402

CHECK_TIMEOUT_S = 4.0
# An org with GitHub disconnected cannot change that answer without an admin
# acting, so it is the one reply worth caching. A day is short enough that a
# freshly connected integration is picked up on its own, and /memhub:link-pr
# always asks live.
NEGATIVE_TTL_S = 24 * 3600
STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "prlink"

HOSTS = ("claude", "codex", "cursor")

# ---------------------------------------------------------------- detectors

# Quoted segments are stripped before matching so a search pattern like
# grep "gh pr view" can never look like a call that addressed GitHub.
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")

# `gh ... pr <anything>` with `gh` at command position — start of string or
# after a separator (&&, ;, |, subshell paren, backtick, newline). Before
# `gh`, only env-var assignments and common wrappers (env, sudo, nohup,
# command, exec, timeout) with their flags/duration args are tolerated; any
# other leading token (grep, rg, echo…) keeps the command from matching.
# Flags are allowed between `gh` and `pr` but never across a separator
# (`gh repo view && foo pr create` must not match).
#
# Copied from pr_babysit_trigger.GH_PR_CREATE with `\bpr\s+create\b` widened
# to `\bpr\b`, rather than imported: that module is a hook entry point, and a
# shared import between two hooks is a coupling neither wants. The eight lines
# are covered by an agreement test over a shared command corpus.
#
# Deliberately NO subcommand allowlist: `gh pr list` and `gh pr status` are
# excluded by the exactly-one-URL rule in `pr_url_from_response`, not by
# naming subcommands — a rule that keeps working when `gh` adds one.
_GH_PREFIX = (
    r"(?:^|[;&|`\n(]|\$\()\s*"
    r"(?:(?:\w+=\S*|env|sudo|nohup|command|exec|timeout|--?[\w=:,.-]+|\d[\w.]*)\s+)*"
    r"gh\b[^|;&\n]*?"
)
GH_PR = re.compile(_GH_PREFIX + r"\bpr\b")
GH_PR_CREATE = re.compile(_GH_PREFIX + r"\bpr\s+create\b")

# The GitHub REST API, on github.com and on an enterprise host. Matched against
# a single shell TOKEN, because the command is tokenised before this runs.
_API_URL = re.compile(
    r"^https?://(api\.github\.com)/repos/([\w.-]+)/([\w.-]+)/pulls(?:/(\d+))?(?:[/?].*)?$"
    r"|^https?://([\w.-]+)/api/v3/repos/([\w.-]+)/([\w.-]+)/pulls(?:/(\d+))?(?:[/?].*)?$",
    re.I,
)
# `gh api` takes a bare path instead of a URL.
_API_PATH = re.compile(r"^/?repos/[\w.-]+/[\w.-]+/pulls(?:/(\d+))?(?:/.*)?$")
_HTTP_CLIENTS = frozenset({"curl", "wget", "http", "https", "xh", "xhs"})
_WRAPPERS = frozenset({"env", "sudo", "doas", "nohup", "command", "exec",
                       "timeout", "time", "stdbuf"})
_ASSIGN_TOKEN = re.compile(r"[A-Za-z_]\w*=")
_HOSTNAME_FLAG = "--hostname"

# Text-level equivalents, used ONLY when the command will not tokenise. The
# command already ran, so it was valid shell — `shlex` merely models less of it
# than the shell does ($'…', line continuations, process substitution). Going
# silent there would lose a call that genuinely addressed GitHub, so the
# fallback still recognises the target; it just never infers a write from text
# it could not parse, so an unreadable command can reach B2 but never B1.
_API_URL_TEXT = re.compile(
    r"https?://(api\.github\.com)/repos/[\w.-]+/[\w.-]+/pulls(/\d+)?"
    r"|https?://([\w.-]+)/api/v3/repos/[\w.-]+/[\w.-]+/pulls(/\d+)?", re.I)
_API_PATH_TEXT = re.compile(r"(?:^|\s)/?repos/[\w.-]+/[\w.-]+/pulls(/\d+)?(?![\w.-])")
_GH_API_TEXT = re.compile(_GH_PREFIX + r"\bapi\b")
_HTTP_CLIENT_TEXT = re.compile(
    r"(?:^|[;&|`\n(]|\$\()\s*(?:\w+=\S*\s+)*(?:curl|wget|http|https|xh|xhs)\b")


def _api_call_untokenised(command: str) -> tuple[str | None, bool, str | None]:
    """Best effort for a command `shlex` refused. Never reports a write."""
    text = _unquoted(command)
    is_gh_api = bool(_GH_API_TEXT.search(text))
    if not (is_gh_api or _HTTP_CLIENT_TEXT.search(text)):
        return None, False, None
    match = _API_URL_TEXT.search(command[:MAX_COMMAND_CHARS])
    if match:
        host = (match.group(1) or match.group(3) or "").casefold()
        number = match.group(2) or match.group(4)
    elif is_gh_api:
        path = _API_PATH_TEXT.search(command[:MAX_COMMAND_CHARS])
        if not path:
            return None, False, None
        host, number = "", path.group(1)
    else:
        return None, False, None
    enterprise = host if host and host not in ("api.github.com", "github.com") else None
    return ("pull_item" if number else "pulls_collection"), False, enterprise
# Flags are read from shell TOKENS, not with a regex over the text. A regex has
# to be told where quoting starts and stops, and it kept getting that wrong in
# both directions: `--method 'GET'` had its value blanked away, so an explicit
# GET looked like no method at all and `-f` then inferred a POST — a listing
# read as a pull-request creation. Tokenising lets the shell's own rules decide
# what is a flag and what is a value, so a `-X POST` sitting INSIDE a quoted
# JSON body stays one token of data.
_METHOD_FLAGS = ("-X", "--request", "--method")
# `gh api` sends fields as POST unless `--method GET` says otherwise.
_GH_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# curl's `-d`/`--data*`/`-F` imply POST. NOT `-f`, which is curl's `--fail`.
_CURL_DATA_FLAGS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "--data-ascii", "-F", "--form", "--form-string"})
# wget documents `--post-data=STRING` / `--post-file=FILE` as "use the POST
# method". Accepting wget as a client and then only knowing curl's flags meant
# a real creation through it fell to B2 — safe, but the client was listed as
# supported while being half-supported.
_WGET_POST_FLAGS = ("--post-data", "--post-file", "--body-data", "--body-file")
# HTTPie and xh take the method as a POSITIONAL argument: `http POST <url>`.
_HTTPIE_CLIENTS = frozenset({"http", "https", "xh", "xhs"})
_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD",
                         "OPTIONS"})
_SHELL_PUNCTUATION = ";&|`()<>\r\n"


def _tokens(command: str) -> list[str] | None:
    """The command as shell words, or None if it does not parse.

    None means "cannot read the flags", and the caller treats that as "do not
    infer a write" — an unbalanced quote must never manufacture a create.
    """
    try:
        lexer = shlex.shlex(command[:MAX_COMMAND_CHARS], posix=True,
                            punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _explicit_method(tokens: list[str]) -> str | None:
    """The method the command NAMES, in any spelling, or None."""
    for index, token in enumerate(tokens):
        for flag in _METHOD_FLAGS:
            if token == flag and index + 1 < len(tokens):
                return tokens[index + 1].upper()
            if token.startswith(flag + "="):
                return token[len(flag) + 1:].upper()
        if token.startswith("-X") and len(token) > 2:      # -XPOST
            return token[2:].upper()
    return None

# The SERVER segment must name GitHub — `mcp__<server>__<tool>`. Matching
# `github` anywhere in the whole name would catch `mcp__notes__github_summary`,
# which is a note-taking tool.
#
# The segment is taken up to the FIRST `__`, not with `[^_]*`: server names
# contain underscores all the time. `github_enterprise` is one, and any
# plugin-provided server is another — this repo's own tools arrive as
# `mcp__plugin_memhub-staging_memhub__add_memory`. `[^_]*` stopped at the first
# underscore and rejected every tool from such a server, so neither the create
# link nor the in-play judgment could ever fire for them.
_MCP_SPLIT = re.compile(r"(?i)^mcp__(.+?)__(.+)$")
_MCP_IS_GITHUB = re.compile(r"(?i)github")
# Loose on the verb, strict on the object — but read as TOKENS rather than as
# one regex, because both halves of that sentence have to hold at once and a
# regex kept getting one of them wrong:
#
#   * `create_pull_request_review`, `create_pull_request_comment` and
#     `submit_pull_request_review` are NOT openings. Reviewing someone else's
#     pull request read as "you opened this", and B1 then told the reviewing
#     session to record itself as the author of code it was only reading.
#   * `create_pr` is an opening, and `\bpr\b` never matched it: `_` is a word
#     character, so there is no boundary between `create_` and `pr`.
#
# So: split the tool segment into words (camelCase and `_`/`-` both count),
# require a creation verb anywhere, and require the TAIL to be the pull request
# itself. The asymmetry decides the trade — a missed create falls through to
# B2, where the model judges and links only if it wrote the code, while a false
# create writes a confirmed authorship claim for work the session did not do.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CREATE_VERBS = frozenset({"create", "open", "submit", "new"})
_PR_HEADS = frozenset({"pr", "prs", "pullrequest", "pullrequests"})
_PR_TAILS = (["pull", "request"], ["pull", "requests"])
# An object that can only be attached TO a pull request, wherever it appears in
# the name. The tail check alone accepted `create_review_for_pull_request` and
# `create_comment_on_pull_request`, which end in the right words while creating
# something else entirely — and B1 then told a session reviewing a teammate's
# pull request to record itself as its author.
_NOT_THE_PR = frozenset({
    "review", "reviews", "comment", "comments", "thread", "threads",
    "reply", "replies", "annotation", "annotations", "suggestion",
    "suggestions", "label", "labels", "assignee", "assignees",
    "reviewer", "reviewers", "milestone"})

MAX_COMMAND_CHARS = pr_provenance.MAX_COMMAND_CHARS


def _unquoted(command: object) -> str:
    """The command with quoted segments BLANKED, bounded like pr_provenance.

    This is the text every command-position question is asked of, because
    blanking is what stops `grep "gh pr view"` from looking like a `gh` call.
    """
    if not isinstance(command, str) or not command:
        return ""
    return QUOTED.sub(" ", command[:MAX_COMMAND_CHARS])


def is_gh_pr_command(command: object) -> bool:
    """Any `gh pr …` at command position — view, checkout, comment, merge…"""
    return bool(GH_PR.search(_unquoted(command)))


def is_gh_pr_create(command: object) -> bool:
    """`gh pr create` at command position. Narrower than is_gh_pr_command, and
    a separate predicate rather than a refinement of it: this one decides B1
    vs B2, and a session that ran it links itself without being asked."""
    return bool(GH_PR_CREATE.search(_unquoted(command)))


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split on shell operators into simple commands.

    One Bash call routinely chains several. Reading flags from the whole string
    bound them to the wrong invocation: `gh api --method POST …/issues && gh
    api --method GET …/pulls` took the POST and classified the LISTING as a
    creation, and the reverse order silently missed a real one.
    """
    out: list[list[str]] = []
    segment: list[str] = []
    for token in tokens:
        if token and all(ch in _SHELL_PUNCTUATION for ch in token):
            if segment:
                out.append(segment)
            segment = []
        else:
            segment.append(token)
    if segment:
        out.append(segment)
    return out


def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _command_name(segment: list[str]) -> tuple[str, list[str]]:
    """``(basename, args)`` for a simple command.

    Leading env assignments and the usual wrappers are stepped over, so
    `env X=1 sudo timeout 5 /usr/local/bin/curl …` still reads as curl.
    """
    index, after_wrapper = 0, False
    while index < len(segment):
        token = segment[index]
        if _ASSIGN_TOKEN.match(token):
            index += 1
            continue
        if _basename(token) in _WRAPPERS:
            index, after_wrapper = index + 1, True
            continue
        if token.startswith("-") or (after_wrapper and token[:1].isdigit()):
            index += 1
            continue
        break
    if index >= len(segment):
        return "", []
    return _basename(segment[index]), segment[index + 1:]


def _positional_method(args: list[str]) -> str | None:
    """HTTPie/xh name the method as a bare word before the URL."""
    for token in args:
        if token.startswith("-"):
            continue
        upper = token.upper()
        if upper in _HTTP_VERBS:
            return upper
        return None            # the first bare word was the URL, not a verb
    return None


def _wget_posts(segment: list[str]) -> bool:
    return any(t == f or t.startswith(f + "=")
               for t in segment for f in _WGET_POST_FLAGS)


def _hostname_flag(args: list[str]) -> str | None:
    """`gh api --hostname ghe.corp …` — an enterprise call with no URL in it."""
    for index, token in enumerate(args):
        if token == _HOSTNAME_FLAG and index + 1 < len(args):
            return args[index + 1].casefold()
        if token.startswith(_HOSTNAME_FLAG + "="):
            return token[len(_HOSTNAME_FLAG) + 1:].casefold()
    return None


def _api_call(command: object) -> tuple[str | None, bool, str | None]:
    """``(target, is_write, enterprise_host)`` for a call to the pulls API.

    Everything is decided WITHIN the simple command that carries the target, so
    a chained call cannot lend its method to a different invocation.
    """
    if not isinstance(command, str) or not command:
        return None, False, None
    tokens = _tokens(command)
    if tokens is None:
        return _api_call_untokenised(command)
    if not tokens:
        return None, False, None

    matches: list[tuple[str, bool, str | None]] = []
    for segment in _segments(tokens):
        name, args = _command_name(segment)
        is_gh_api = name in ("gh", "gh.exe") and "api" in args
        is_http = name in _HTTP_CLIENTS
        if not (is_gh_api or is_http):
            continue

        target = number = host = None
        for arg in args:
            match = _API_URL.match(arg)
            if match:
                host = (match.group(1) or match.group(5) or "").casefold()
                number = match.group(4) or match.group(8)
                target = "pull_item" if number else "pulls_collection"
                break
            if is_gh_api:
                path = _API_PATH.match(arg)
                if path:
                    number = path.group(1)
                    target = "pull_item" if number else "pulls_collection"
                    break
        if target is None:
            continue

        if host is None or host == "api.github.com":
            # `gh api --hostname ghe.corp repos/o/r/pulls` is an enterprise
            # call with no URL anywhere in it (documented in `gh api --help`).
            host = _hostname_flag(args) if is_gh_api else host
        enterprise = host if host and host not in ("api.github.com", "github.com") else None

        # An explicit method always wins over an inferred one, on BOTH clients.
        # `gh api --help`: "To send the parameters as a GET query string
        # instead, use --method GET" — so `gh api --method GET …/pulls -f
        # state=open` is a documented LISTING that carries `-f`. Reading it as
        # a write made `creates_pr` true, and B1 then told a session that had
        # only listed pull requests to record a confirmed self-link on one.
        method = _explicit_method(segment)
        if method is None and name in _HTTPIE_CLIENTS:
            method = _positional_method(args)
        if method is not None:
            write = method == "POST"
        elif is_gh_api and any(t in _GH_FIELD_FLAGS for t in segment):
            write = True
        elif name == "wget" and _wget_posts(segment):
            write = True
        elif is_http and any(t in _CURL_DATA_FLAGS or t.startswith("--data")
                             for t in segment):
            write = True
        else:
            write = False
        matches.append((target, write, enterprise))

    if not matches:
        return None, False, None
    if len(matches) == 1:
        return matches[0]
    # Several pulls-API calls in ONE shell call. The tool result is their
    # combined output, and there is no way to tell which segment produced the
    # single URL in it — so `gh api --method POST repos/o/a/pulls >/dev/null &&
    # gh api --method GET repos/o/b/pulls/7 --jq .html_url` would have taken
    # the POST's verdict and applied it to PR b's URL, linking the session as
    # the author of a pull request it did not open. The call still addressed
    # GitHub (so it reaches B2 and the model judges), but it can never take the
    # unconditional B1 path.
    hosts = {host for _t, _w, host in matches}
    return matches[0][0], False, hosts.pop() if len(hosts) == 1 else None


def github_api_call(command: object) -> tuple[str | None, bool]:
    """``(target, is_write)`` for a REST call to GitHub's pulls API.

    ``target`` is ``"pulls_collection"`` (a POST to which OPENS a pull
    request), ``"pull_item"`` (``…/pulls/<n>`` — editing one, not opening
    one), or None. ``is_write`` is whether that call carries a POST.
    """
    target, is_write, _host = _api_call(command)
    return target, is_write


def github_api_host(command: object) -> str | None:
    """The GitHub host this command addressed, when it is not github.com.

    `_api_call` accepts an enterprise REST target, but
    `pr_provenance.urls_from_output_text` only recognises `github.com` URLs —
    so an enterprise create was detected and then produced no URL, and the hook
    went silent on exactly the calls it had just decided to care about.

    The host is taken from the COMMAND rather than from a widened pattern over
    the response: `https://<any host>/<o>/<r>/pull/<n>` would match unrelated
    sites that happen to use that path shape. Only the host the session itself
    just talked to is trusted.
    """
    return _api_call(command)[2]


def _urls_on_host(text: object, host: str) -> list[str]:
    """PR URLs on one specific enterprise host — same shape as the canonical
    rule in `pr_provenance`, with the host pinned rather than wildcarded."""
    if not isinstance(text, str) or not text:
        return []
    pattern = (r"https://" + re.escape(host) +
               r"/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
               r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]*)(?![A-Za-z0-9/])")
    found: list[str] = []
    budget = text[:pr_provenance.MAX_RESULT_TEXT_BYTES]
    for match in re.finditer(pattern, budget, re.I):
        owner, repo, number = match.groups()
        url = f"https://{host}/{owner.lower()}/{repo.lower()}/pull/{int(number)}"
        if url not in found:
            found.append(url)
            if len(found) >= pr_provenance.MAX_URLS:
                break
    return found


def _mcp_tool_name(tool_name: object) -> str:
    return tool_name if isinstance(tool_name, str) else ""


def _mcp_parts(tool_name: object) -> tuple[str, str] | None:
    """``(server, tool)`` for an MCP tool name, or None if it is not one."""
    m = _MCP_SPLIT.match(_mcp_tool_name(tool_name))
    return (m.group(1), m.group(2)) if m else None


def is_github_mcp_tool(tool_name: object) -> bool:
    parts = _mcp_parts(tool_name)
    return bool(parts and _MCP_IS_GITHUB.search(parts[0]))


def _words(segment: str) -> list[str]:
    """`createPullRequest` and `create_pull_request` both → the same words."""
    spaced = _CAMEL_SPLIT.sub(" ", segment)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def is_github_mcp_create(tool_name: object) -> bool:
    """Does this GitHub MCP tool OPEN a pull request (not review or comment on
    one)? The object must be the pull request itself — the tail of the name."""
    parts = _mcp_parts(tool_name)
    if not parts or not _MCP_IS_GITHUB.search(parts[0]):
        return False
    words = _words(parts[1])
    if not words or not _CREATE_VERBS.intersection(words):
        return False
    if _NOT_THE_PR.intersection(words):
        return False
    return words[-1] in _PR_HEADS or words[-2:] in _PR_TAILS


def _command_of(tool_name: object, tool_input: object) -> str:
    """The shell command this call carries, or ''.

    Only a shell tool's `command` is a shell command. Another tool's input may
    hold a field of that name meaning something else entirely, and reading it
    as shell is how a hook fires on a tool it knows nothing about.
    """
    if not isinstance(tool_input, dict):
        return ""
    if not isinstance(tool_name, str) or is_github_mcp_tool(tool_name):
        return ""
    if tool_name not in ("Bash", "shell", "local_shell", "exec", "exec_command"):
        return ""
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def touches_github(tool_name: object, tool_input: object) -> bool:
    """Did this tool call address GitHub at all? The gate for the whole hook."""
    if is_github_mcp_tool(tool_name):
        return True
    command = _command_of(tool_name, tool_input)
    if not command:
        return False
    return is_gh_pr_command(command) or github_api_call(command)[0] is not None


def creates_pr(tool_name: object, tool_input: object) -> bool:
    """Is this call OPENING a pull request? First match wins; no scoring."""
    if is_github_mcp_create(tool_name):
        return True
    command = _command_of(tool_name, tool_input)
    if not command:
        return False
    if is_gh_pr_create(command):
        return True
    target, is_write = github_api_call(command)
    return target == "pulls_collection" and is_write


def pr_url_from_response(tool_response: object, host: str | None = None) -> str | None:
    """The one PR URL in this tool result, or None.

    Zero URLs (a failed command, a `gh pr checkout` printing only a branch) or
    two-or-more (`gh pr list`, `gh pr status`) both answer None, and the hook
    is then silent. That single rule is what keeps the listing commands quiet
    while `create`, `view`, `checkout`, `comment`, `merge`, `ready` and `edit`
    still resolve — as do a `curl` POST response body and a GitHub MCP result,
    which each carry exactly one `html_url`.

    stderr counts: `gh pr create` on a branch that already has one prints the
    existing PR's URL there, and that is still the PR being worked on.
    """
    texts: list[str] = []
    if isinstance(tool_response, str):
        texts.append(tool_response)
    elif isinstance(tool_response, dict):
        for key in ("stdout", "stderr"):
            value = tool_response.get(key)
            if isinstance(value, str):
                texts.append(value)
        # An MCP result is a nested object; reuse pr_provenance's bounded
        # walker (depth, node and byte caps) rather than writing a second one.
        texts.extend(pr_provenance._result_strings(
            tool_response,
            [pr_provenance.MAX_RESULT_TEXT_BYTES],
            [pr_provenance.MAX_RESULT_NODES],
        ))
    elif isinstance(tool_response, list):
        texts.extend(pr_provenance._result_strings(
            tool_response,
            [pr_provenance.MAX_RESULT_TEXT_BYTES],
            [pr_provenance.MAX_RESULT_NODES],
        ))
    else:
        return None

    urls: list[str] = []
    for text in texts:
        found = pr_provenance.urls_from_output_text(text)
        if host:
            found = found + _urls_on_host(text, host)
        for url in found:
            if url not in urls:
                urls.append(url)
        if len(urls) > 1:
            return None
    return urls[0] if len(urls) == 1 else None


# ------------------------------------------------------------ session ids

def conversation_id_for(host: object, session_id: object) -> str | None:
    """The conversation id the capture client already sends for this session.

    Claude Code sends the bare session UUID; Codex and Cursor namespace theirs
    ``<host>-<uuid>`` so server-side watermarks stay per host. An id that
    already carries its prefix is returned unchanged — double-prefixing is how
    a retry links nothing.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    sid = session_id.strip()
    host = host if isinstance(host, str) else ""
    host = host.strip().lower()
    if host in ("codex", "cursor"):
        prefix = f"{host}-"
        return sid if sid.startswith(prefix) else prefix + sid
    return sid


# ---------------------------------------------------------------- the check

_PR_URL_SCOPE = re.compile(r"https://([\w.-]+)/([^/]+)/([^/]+)/pull/\d+", re.I)


def _repo_of(pr_url: str) -> str:
    """``host/owner/repo`` from a PR URL, or "" if it is not one.

    The HOST is part of the key, not just the repo: two enterprise deployments
    are two different backends' worth of orgs. A github.com-only pattern gave
    every enterprise URL an empty scope, so all of them shared one cache file
    and a single disconnected enterprise repo silenced linking for every other
    one — the bug this scoping was added to fix, reintroduced for enterprise.
    """
    m = _PR_URL_SCOPE.search(pr_url or "")
    return f"{m.group(1)}/{m.group(2)}/{m.group(3)}".casefold() if m else ""


def _cache_path(api_base: str, scope: str = "") -> Path:
    """Where a negative answer for THIS deployment and THIS repo is stored.

    ``enabled`` and ``github_connected`` are properties of the MemHub ORG that
    owns the repo, not of the deployment — one person can be in several orgs on
    one backend. Keying on the api_base alone meant a single disconnected org
    silenced linking for every other org's pull requests for 24 hours, without
    a request, which is exactly the silent failure the feature is meant to
    avoid. The repo is the coarsest thing in the request that determines which
    org answers, so it is what scopes the entry; the cost is one round trip per
    repo per day instead of one per `gh pr` call.
    """
    digest = hashlib.sha256(f"{api_base}\n{scope}".encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"{digest}.json"


def _cached_negative(api_base: str, now: float, scope: str = "") -> dict | None:
    """A stored `enabled:false` / `github_connected:false` answer, if fresh."""
    try:
        raw = json.loads(_cache_path(api_base, scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    answer = raw.get("answer")
    if not isinstance(at, (int, float)) or not isinstance(answer, dict):
        return None
    # A clock that moved backwards must not pin a stale answer forever.
    if not (0 <= now - at < NEGATIVE_TTL_S):
        return None
    return answer


def _store_negative(api_base: str, answer: dict, now: float, scope: str = "") -> None:
    try:
        import atomic_write

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write.publish(_cache_path(api_base, scope),
                             json.dumps({"at": now, "answer": answer}))
    except Exception:
        pass


def breadcrumb(what: str, exc: object) -> None:
    """Why this machine went quiet — local only, best effort, never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "breadcrumb").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {what}: {exc!r}\n")
    except OSError:
        pass


def check(pr_url: str, *, timeout: float = CHECK_TIMEOUT_S, now: float | None = None) -> dict | None:
    """Ask the server about this pull request, or None.

    None — no credential, transport error, non-200, unexpected shape — means
    the hook emits nothing. Silence is always the safe answer here: this runs
    after a command in a live session, so it is never allowed to slow one down
    or fail one, and the user can still run `/memhub:link-pr`.
    """
    now = time.time() if now is None else now
    try:
        import _memhub_auth
        import mcp_http
        import pak

        url, bearer = _memhub_auth.resolve_bearer(refresh=False)
        if not bearer:
            return None
        api_base = pak.api_base(url)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into a hook
        breadcrumb("resolve", exc)
        return None

    scope = _repo_of(pr_url)
    cached = _cached_negative(api_base, now, scope)
    if cached is not None:
        return cached

    # Everything from the request to the last read of the reply is inside the
    # try: a reply object that does not look the way this expects is one more
    # reason to be silent, not a traceback in the middle of someone's session.
    # `quote(..., safe="")` escapes the whole URL — its own `?`, `&` and `#`
    # included — so a pull-request URL can never add a query parameter here.
    try:
        reply = mcp_http.rest(
            f"{api_base}/v1/team/pr-links/check?pr_url={quote(pr_url, safe='')}",
            bearer, "GET", timeout=timeout)
        if reply.status != 200 or not isinstance(reply.data, dict):
            return None
        data = reply.data
        if data.get("enabled") is False or data.get("github_connected") is False:
            _store_negative(api_base, data, now, scope)
        return data
    except Exception as exc:  # noqa: BLE001
        breadcrumb("check", exc)
        return None


# ---------------------------------------------------------------- contexts

CONNECT_ADVISORY = (
    "MemHub: this org has no GitHub integration connected, so the sessions behind this pull "
    "request can't be linked to it. Mention this to the user ONCE per session, briefly, and only "
    "if it is not intrusive right now: connecting GitHub on MemHub ({connect_url}) is what links "
    "their agent sessions to the code that shipped. Do not repeat it, do not offer to do it for "
    "them, and do not let it interrupt what they asked for."
)

REPO_ADVISORY = (
    "MemHub: GitHub is connected, but {repo} isn't part of the install, so this PR can't be "
    "linked. Mention this to the user ONCE per session, briefly, and only if it is not intrusive "
    "right now: adding this repository on MemHub ({connect_url}) is what links their agent "
    "sessions to the code that shipped. Do not repeat it, do not offer to do it for them, and do "
    "not let it interrupt what they asked for."
)

CREATED = (
    "MemHub: you just opened {pr_url}{pr_ref}.\n"
    "{linked}"
    "Link this session to it now: `link_pr` with pr_url=\"{pr_url}\", "
    "session_ids=[\"{session_id}\"], link_source=\"session_self\". Do this without asking — "
    "opening the pull request is itself work this session did, and the call is idempotent.\n"
    "Then, ONLY IF most of the code in this pull request was written in OTHER sessions (you "
    "opened it for work done earlier, or you are shipping someone else's branch), add one "
    "sentence offering `/memhub:find-contributing-sessions {pr_url}` so those sessions get "
    "linked too. A pull request has many sessions; yours does not displace them. Do not run it "
    "without a yes."
)

IN_PLAY = (
    "MemHub: a pull request is in play — {pr_url}{pr_ref}.\n"
    "{linked}"
    "A pull request has many sessions and a session has many pull requests, so linking again "
    "from a different session is expected, not a duplicate.\n"
    "IF THE CODE IN THIS PULL REQUEST WAS WRITTEN IN THIS SESSION — you edited those files here "
    "— call `link_pr` with pr_url=\"{pr_url}\", session_ids=[\"{session_id}\"], "
    "link_source=\"session_self\". Do it without asking; it is a record of work you did, and it "
    "is idempotent.\n"
    "IF IT WAS NOT — you are reviewing, checking out, or commenting on someone else's work, or "
    "work from an earlier session — do not link. Instead offer, in one sentence, to run "
    "`/memhub:find-contributing-sessions {pr_url}` to find the sessions that did write it. Do "
    "not run it without the user saying yes.\n"
    "If this session already linked itself to this pull request, say nothing at all."
)


def _pr_ref(reply: dict) -> str:
    pr = reply.get("pr")
    if not isinstance(pr, dict):
        return ""
    repo = pr.get("repo_full_name")
    number = pr.get("pr_number")
    if not isinstance(repo, str) or not isinstance(number, int):
        return ""
    state = pr.get("state")
    bits = f"{repo}#{number}"
    if isinstance(state, str) and state:
        bits += f", {state}"
    return f" ({bits})"


def _linked_line(reply: dict) -> str:
    rows = reply.get("linked_sessions")
    if not isinstance(rows, list) or not rows:
        return ""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return ""
    mine = sum(1 for r in rows if r.get("is_mine") is True)
    plural = "s" if len(rows) != 1 else ""
    tail = f" ({mine} of them this user's)" if mine else ""
    return f"Already linked: {len(rows)} session{plural}{tail}.\n"


def _repo_name(reply: dict) -> str:
    pr = reply.get("pr")
    repo = pr.get("repo_full_name") if isinstance(pr, dict) else None
    return repo if isinstance(repo, str) and repo else "this repository"


def context_for(reply: dict, pr_url: str, session_id: str, *, created: bool) -> str | None:
    """The one instruction this call earns, or None for silence.

    ``enabled:false`` — the org has the feature off — is silence, not an
    advisory: an org that turned it off should never hear about it again.
    """
    if not isinstance(reply, dict) or reply.get("enabled") is False:
        return None
    connect_url = reply.get("connect_url")
    connect_url = connect_url if isinstance(connect_url, str) and connect_url \
        else "your MemHub settings"
    if reply.get("github_connected") is not True:
        return CONNECT_ADVISORY.format(connect_url=connect_url)
    if reply.get("repo_in_install") is False:
        return REPO_ADVISORY.format(repo=_repo_name(reply), connect_url=connect_url)
    if not session_id:
        # Nothing to link. The advisory paths above still help; this one would
        # tell the agent to call a tool with no argument.
        return None
    template = CREATED if created else IN_PLAY
    return template.format(pr_url=pr_url, pr_ref=_pr_ref(reply),
                           linked=_linked_line(reply), session_id=session_id)


def context_for_call(tool_name: object, tool_input: object, tool_response: object,
                     session_id: str, *, host: str = "claude",
                     checker=check) -> str | None:
    """stdin → the text to inject, or None. The whole hook, minus its I/O."""
    if not touches_github(tool_name, tool_input):
        return None
    pr_url = pr_url_from_response(
        tool_response, host=github_api_host(_command_of(tool_name, tool_input)))
    if not pr_url:
        return None
    reply = checker(pr_url)
    if not isinstance(reply, dict):
        return None
    conv_id = conversation_id_for(host, session_id) or ""
    return context_for(reply, pr_url, conv_id,
                       created=creates_pr(tool_name, tool_input))


__all__ = [
    "CHECK_TIMEOUT_S", "CONNECT_ADVISORY", "CREATED", "HOSTS", "IN_PLAY",
    "NEGATIVE_TTL_S", "REPO_ADVISORY", "STATE_DIR", "breadcrumb", "check",
    "context_for", "context_for_call", "conversation_id_for", "creates_pr",
    "github_api_call", "github_api_host", "is_gh_pr_command", "is_gh_pr_create",
    "is_github_mcp_create", "is_github_mcp_tool", "pr_url_from_response",
    "touches_github",
]
