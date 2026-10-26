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
# acting, so it is the one reply worth caching. But nothing invalidates a
# negative when the condition flips POSITIVE, so this TTL is the entire blast
# radius of a wrong entry: for 24h it silenced linking on every machine that
# happened to ask while the feature was off, with no request, no error and no
# log line. Half an hour keeps the round trips away without latching the day.
# Overridable per machine with MEMHUB_PRLINK_NEGATIVE_TTL_S; /memhub:link-pr
# always asks live regardless.
NEGATIVE_TTL_S = 30 * 60
NEGATIVE_TTL_ENV = "MEMHUB_PRLINK_NEGATIVE_TTL_S"
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
# `gh api --help` documents `{owner}`/`{repo}` placeholders, and a query
# string is the documented way to filter — `repos/o/r/pulls?state=open`.
# Rejecting both meant a PR opened through the placeholder form got no context
# at all.
_API_PATH = re.compile(
    r"^/?repos/(?:[\w.-]+|\{[\w.-]+\})/(?:[\w.-]+|\{[\w.-]+\})"
    r"/pulls(?:/(\d+))?(?:[/?].*)?$")
# `gh pr new` is a documented alias of `gh pr create`.
_GH_CREATE_SUBS = frozenset({"create", "new"})
_HTTP_CLIENTS = frozenset({"curl", "wget", "http", "https", "xh", "xhs"})
_WRAPPERS = frozenset({"env", "sudo", "doas", "nohup", "command", "exec",
                       "timeout", "time", "stdbuf"})
_ASSIGN_TOKEN = re.compile(r"[A-Za-z_]\w*=")
_HOSTNAME_FLAG = "--hostname"
# Wrapper flags that take a separate operand. `env -u DEBUG curl …` skipped
# `env` and `-u`, then read `DEBUG` as the executable and saw no GitHub call at
# all. (`env -u/--unset`, `sudo -u/-g/-p/-U/-C`, `timeout -s/-k`.)
_WRAPPER_VALUE_FLAGS = frozenset({
    "-u", "--unset", "-g", "--group", "-p", "--prompt", "-U", "-C",
    "--close-from", "-s", "--signal", "-k", "--kill-after"})

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
# `curl --help all` on `--json <data>`: "HTTP POST JSON".
_CURL_DATA_FLAGS = frozenset({
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "--data-ascii", "-F", "--form", "--form-string", "--json"})
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

    Heredoc bodies come out FIRST. A body is prose, and prose has unbalanced
    quotes, so `gh pr create --body-file - <<'EOF' … EOF` — the shape every
    real creation has — raised out of shlex and took the unparseable fallback,
    which skips `_url_source_is_certain` entirely. Measured over 15,134 real
    tool calls: 20 of 20 B1 decisions were made without the ordering guard
    ever running. Stripping the body first is what puts them back under it.
    """
    try:
        lexer = shlex.shlex(strip_heredocs(command)[:MAX_COMMAND_CHARS], posix=True,
                            punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        # `\n` and `\r` are in `_SHELL_PUNCTUATION`, but shlex's WHITESPACE
        # rule is consulted first and swallowed them — so a multi-line command
        # (which agents write constantly) collapsed into a single segment and
        # every segment-based guard here silently did nothing on it. Bash
        # treats a newline as a control operator, and so must this.
        lexer.whitespace = " \t"
        return list(lexer)
    except ValueError:
        return None


# Long options that take a separate operand. Without consuming them,
# `curl -H '-XPOST' …` read the HEADER as a method flag and called a GET a
# creation — the argument of a read-only option must never be interpreted as
# an option itself.
_VALUE_LONG_OPTS = frozenset({
    "--header", "--data", "--data-raw", "--data-binary", "--data-urlencode",
    "--data-ascii", "--form", "--form-string", "--json", "--url", "--output",
    "--user", "--user-agent", "--referer", "--cookie", "--cookie-jar",
    "--dump-header", "--upload-file", "--proxy", "--cert", "--key",
    "--cacert", "--capath", "--connect-timeout", "--max-time", "--retry",
    "--range", "--write-out", "--config", "--hostname", "--input",
    "--field", "--raw-field", "--template", "--jq", "--method", "--request",
    "--post-data", "--post-file", "--body-data", "--body-file",
    # `--url-query <data>` adds a query part and its operand was being read as
    # a method. This list IS the risk surface: any value-taking option missing
    # from it lets its argument be re-interpreted as an option, so prefer
    # adding one speculatively over leaving it out.
    "--url-query", "--aws-sigv4", "--oauth2-bearer", "--proxy-header",
    "--resolve", "--connect-to", "--request-target", "--unix-socket",
    "--interface", "--noproxy", "--preproxy", "--proxy-user", "--netrc-file",
    "--output-dir", "--max-filesize", "--max-redirs", "--limit-rate",
    "--retry-delay", "--retry-max-time", "--trace", "--trace-ascii",
    "--libcurl", "--login-options", "--mail-from", "--mail-rcpt", "--quote",
    "--speed-limit", "--speed-time", "--tls-max", "--tlsuser",
    "--tlspassword", "--tlsauthtype", "--service-name", "--sasl-authzid",
    "--proto", "--proto-default", "--proto-redir", "--pubkey", "--engine",
    "--ftp-account", "--ftp-method", "--ftp-port", "--krb", "--pass",
    "--dns-servers", "--local-port", "--expect100-timeout",
    "--happy-eyeballs-timeout-ms", "--continue-at", "--socks4", "--socks5"})


# Short options known to take NO operand, across the clients we accept. Like
# the long list below, this exists to keep the common cases working — the
# DEFAULT is what provides the safety.
# Per CLIENT, because the same letter means different things: `-O` is curl's
# boolean --remote-name and wget's value-taking --output-document, and `-p` is
# curl's --proxytunnel but gh's --preview <strings>. One shared set meant a
# read-only option's operand was re-read as `-XPOST` in whichever client
# disagreed with curl. Each set is deliberately SMALL — the default for
# anything absent is "consumes an operand", which can only cost a link.
_NO_OPERAND_SHORT = {
    "curl": frozenset("sSfLkvViIgG#OJNq46lBM0123pn"),
    "wget": frozenset("qvdcNSxrmkbE"),
    "gh": frozenset("i"),
    "gh.exe": frozenset("i"),
    "http": frozenset("hv"),
    "https": frozenset("hv"),
    "xh": frozenset("hv"),
    "xhs": frozenset("hv"),
}
_NO_OPERAND_SHORT_DEFAULT = frozenset()
# Options known to take NO operand. This list exists to make the DEFAULT below
# safe rather than to be exhaustive.
_NO_OPERAND_LONG = frozenset({
    "--silent", "--show-error", "--fail", "--fail-early", "--fail-with-body",
    "--location", "--location-trusted", "--insecure", "--verbose", "--include",
    "--head", "--compressed", "--get", "--next", "--no-buffer", "--netrc",
    "--progress-bar", "--no-progress-meter", "--path-as-is", "--tcp-nodelay",
    "--http1.0", "--http1.1", "--http2", "--http3", "--create-dirs", "--raw",
    "--remote-name", "--remote-header-name", "--globoff", "--ipv4", "--ipv6",
    "--anyauth", "--basic", "--digest", "--ntlm", "--negotiate", "--disable",
    "--list-only", "--append", "--use-ascii", "--crlf", "--junk-session-cookies",
    # wget
    "--debug", "--quiet", "--no-verbose", "--continue", "--timestamping",
    "--spider", "--no-check-certificate", "--content-disposition",
    "--server-response", "--mirror", "--recursive", "--no-clobber",
    # gh
    "--dry-run", "--fill", "--fill-first", "--draft", "--web", "--paginate",
    # httpie / xh
    "--ignore-stdin", "--follow", "--offline", "--print-body"})


def _consumes_operand(token: str, client: str = "curl") -> bool:
    """Does this option take the NEXT token as its value?

    The DEFAULT for an unrecognised option is YES, and that inversion is the
    point. When the default was "no", every value-taking option missing from
    the list let its argument be re-read as an option — five separate findings
    ended that way, each a listing reported as a creation because somebody's
    filename or header happened to contain `-XPOST`.

    Defaulting to "consumes" makes an incomplete list fail the other way: the
    worst case is that a real method flag is skipped, the call reports no
    explicit method, and it falls to B2 where the model judges. A missed
    creation costs a link; a false one makes a confirmed authorship claim about
    work the session did not do.
    """
    if not token.startswith("-") or token == "-":
        return False
    if token.startswith("--"):
        if token in _VALUE_LONG_OPTS:
            return True
        if "=" in token or token in _NO_OPERAND_LONG:
            return False
        return True                    # unknown long option: assume a value
    # A short-option run follows getopt: the first value-taking character
    # consumes the REST of the token if there is any, otherwise the next token.
    for position, char in enumerate(token[1:], start=1):
        if char in _CURL_VALUE_OPTS:
            return position == len(token) - 1
    # The same inversion as for long options, and for the same reason: the
    # value table above is curl's and is shared with every client, so `curl -A`
    # (user agent) and `wget -P` (directory prefix) were not in it and their
    # operands were re-read as `-XPOST`. A run made ENTIRELY of characters
    # known to take no operand is trusted; anything else is assumed to consume
    # one, which can only cost a link, never manufacture a claim.
    booleans = _NO_OPERAND_SHORT.get(client, _NO_OPERAND_SHORT_DEFAULT)
    return not all(char in booleans for char in token[1:])


def _explicit_method(tokens: list[str], client: str = "curl") -> str | None:
    """The method the command NAMES, in any spelling, or None.

    Walked with operand consumption: an option's argument is skipped rather
    than re-examined, so a header, a body or a filename that happens to look
    like `-XPOST` is data, not a method.
    """
    found = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        matched = False
        for flag in _METHOD_FLAGS:
            if token == flag and index + 1 < len(tokens):
                found, matched = tokens[index + 1].upper(), True
                break
            if token.startswith(flag + "="):
                found, matched = token[len(flag) + 1:].upper(), True
                break
        if not matched and (token.startswith("-X") and len(token) > 2
                            and not token.startswith("--")):        # -XPOST
            found = token[2:].upper()
            matched = True
        if not matched and (token.startswith("-") and not token.startswith("--")
                            and len(token) > 2 and "X" in token[1:]):
            # A BUNDLED short run can carry the method flag: `curl -sX GET`
            # is `-s -X GET`, and `curl -sXPOST` is `-s -XPOST`. Reading only
            # a token that STARTS with `-X` missed both, so an explicitly
            # requested GET was invisible and a later `-d` was then read as a
            # write (Codex review, PR #182).
            #
            # getopt semantics: everything after `X` in the run is its value
            # if there is any, otherwise the NEXT token is.
            cut = token.index("X", 1)
            rest = token[cut + 1:]
            if rest:
                found, matched = rest.upper(), True
            elif index + 1 < len(tokens):
                found, matched = tokens[index + 1].upper(), True
                index += 2
                continue
        # Keep scanning: curl documents that when `-X/--request` is given
        # several times, the LAST one is used. Returning the first read
        # `-X POST -X GET` as a creation.
        index += 2 if _consumes_operand(token, client) else 1
    return found

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


# A heredoc BODY is prose, not command text, and reading it as command text
# fails in BOTH directions — measured over 15,134 real tool calls:
#
#   * `cat > pr.md <<'EOF' … EOF` then `gh pr create --body-file pr.md`: the
#     body's unbalanced apostrophes ("doesn't", "user's") make `QUOTED` below
#     blank the REAL `gh pr create` that follows the terminator. Four genuine
#     creations produced no context at all because `is_gh_pr_create` answered
#     False on a command that plainly runs it.
#   * `python3 - <<'PY' … PY` and `git commit -F - <<'MSG' … MSG` whose body
#     merely CONTAINS the text `gh pr create`: 19 such commands matched. They
#     are stopped today only by the exactly-one-URL rule downstream — and they
#     do fire `pr_babysit_trigger`, which has no such rule.
#
# Bounded well above MAX_COMMAND_CHARS on purpose: a PR body of a few tens of
# KB pushes the `gh pr create` that follows it past the cap, so the body has to
# come out BEFORE the command is truncated, not after.
MAX_HEREDOC_SCAN_CHARS = 256 * 1024
# An identifier tag only: `2 << 3` is arithmetic, not a heredoc. And a
# HERE-STRING is not a heredoc: `cat <<<EOF` feeds one word to stdin and the
# next line is ordinary command text — matching from the second `<` treated
# `EOF` as an unterminated tag and swallowed the rest of the command, so
# `cat <<<EOF\ngh pr create --fill` stopped being a creation entirely
# (Codex review, PR #182).
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command: str) -> str:
    """The command with every heredoc BODY removed, the opening line kept.

    Line-oriented, which is the shape a heredoc has: the body runs from the
    line after the one that opened it to a line holding the terminator alone.
    A terminator sharing a line with other code is not handled — bash does not
    accept that either.
    """
    if "<<" not in command:
        return command
    lines = command[:MAX_HEREDOC_SCAN_CHARS].split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        # One line can open several (`cmd <<'A' <<'B'`); bash reads them in
        # the order they appear.
        for match in _HEREDOC_OPEN.finditer(line):
            tag = match.group(2)
            while index < len(lines) and lines[index].strip() != tag:
                index += 1
            index += 1                     # the terminator line goes too
    return "\n".join(kept)


def _unquoted(command: object) -> str:
    """The command with heredoc bodies REMOVED and quoted segments BLANKED,
    bounded like pr_provenance.

    This is the text every command-position question is asked of. Blanking is
    what stops `grep "gh pr view"` from looking like a `gh` call; stripping is
    what stops a heredoc body from doing either half of that damage.
    """
    if not isinstance(command, str) or not command:
        return ""
    return QUOTED.sub(" ", strip_heredocs(command)[:MAX_COMMAND_CHARS])


def is_gh_pr_command(command: object) -> bool:
    """Any `gh pr …` at command position — view, checkout, comment, merge…"""
    return bool(GH_PR.search(_unquoted(command)))


def is_gh_pr_create(command: object) -> bool:
    """`gh pr create` at command position. Narrower than is_gh_pr_command, and
    a separate predicate rather than a refinement of it: this one decides B1
    vs B2, and a session that ran it links itself without being asked."""
    return bool(GH_PR_CREATE.search(_unquoted(command)))


def _segments_with_ops(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """``(operator_before, segment)`` for each simple command.

    The operator matters for one question only — whether the segment before it
    is known to have SUCCEEDED. `&&` says yes, so a `gh pr create &&` chain has
    the created PR's URL in its output; `;` and `||` run their follower even
    when the create failed, in which case the only URL in the result came from
    somewhere else entirely.
    """
    out: list[tuple[str, list[str]]] = []
    segment: list[str] = []
    op = ""
    for token in tokens:
        if token and all(ch in _SHELL_PUNCTUATION for ch in token):
            if segment:
                out.append((op, segment))
                segment = []
            op = token
        else:
            segment.append(token)
    if segment:
        out.append((op, segment))
    return out


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
        if token.startswith("-"):
            # A wrapper flag with a separate operand takes the next token too.
            index += 2 if (after_wrapper and token in _WRAPPER_VALUE_FLAGS
                           and "=" not in token) else 1
            continue
        if after_wrapper and token[:1].isdigit():      # `timeout 5 …`
            index += 1
            continue
        break
    if index >= len(segment):
        return "", []
    return _basename(segment[index]), segment[index + 1:]


def _positional_method(args: list[str]) -> str | None:
    """HTTPie/xh name the method as a bare word before the URL.

    Read from the OPERANDS, not the raw arguments: `http --session POST <url>`
    names a session called POST, and taking it as the method turned a GET
    listing into a claimed creation.
    """
    for token in _operands_of(args, "http"):
        upper = token.upper()
        if upper in _HTTP_VERBS:
            return upper
        return None            # the first bare word was the URL, not a verb
    return None


# curl short options that CONSUME a value. Once one of these appears in a
# bundle, everything after it in the same token is that option's argument and
# must not be read as more options — `-Dheaders` is `--dump-header headers`,
# and a pattern that scanned the whole token found the `d` in "headers" and
# called a GET a creation. `-o Food`, `-A friend` and `-H Food:x` failed the
# same way.
_CURL_VALUE_OPTS = frozenset("abCcDdEeFHKmoTtUuwXxYyZz")
_CURL_DATA_OPTS = frozenset("dF")
# `curl --help all` on `-G, --get`: "Put the post data in the URL and use GET".
# So `-G -d state=open` is a LISTING that carries data, and reading the `-d` as
# a POST turned it into a claimed creation.
_CURL_GET_OPT = "G"


def _curl_short_run_posts(token: str) -> bool:
    """Does this single-dash short-option run carry request data?

    Walked character by character: a boolean flag (`-s`, `-f`, `-L`, `-k`)
    continues the run, `d`/`F` are the data options, and any other
    value-taking option ends the run because the rest of the token is its
    argument.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return False
    for char in token[1:]:
        if char in _CURL_DATA_OPTS:
            return True
        if char in _CURL_VALUE_OPTS:
            return False
        if not char.isalpha():
            return False
    return False


def _is_gh_field_flag(token: str) -> bool:
    """`gh api … -f title=x` and `-ftitle=x` are the same request parameter,
    and adding one switches the method to POST (`gh api --help`)."""
    if token in _GH_FIELD_FLAGS:
        return True
    # `--field=title=x`, `--raw-field=title=x`, `--input=body.json`.
    if any(token.startswith(flag + "=")
           for flag in _GH_FIELD_FLAGS if flag.startswith("--")):
        return True
    # `-ftitle=x` — a short flag with its value attached.
    return (len(token) > 2 and token[0] == "-" and token[1] in "fF"
            and not token.startswith("--"))


def _curl_short_run_has(token: str, wanted: str) -> bool:
    """Is `wanted` a real option in this short-option run, rather than a
    character inside an attached value? Same walk as `_curl_short_run_posts`."""
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return False
    for char in token[1:]:
        if char == wanted:
            return True
        if char in _CURL_VALUE_OPTS or not char.isalpha():
            return False
    return False


def _curl_forces_get(segment: list[str]) -> bool:
    return any(t == "--get" or _curl_short_run_has(t, _CURL_GET_OPT)
               for t in _options_of(segment, "curl"))


def _options_of(args: list[str], client: str = "curl"):
    """The tokens that are OPTIONS, skipping every option's operand.

    Every flag question in this module asks it of this walk rather than of the
    raw list. Scanning flatly meant a read-only option's ARGUMENT could look
    like a flag — `curl -H '-d' …` read the header as POST data, and
    `curl -H '-XPOST' …` read it as a method — which turned listings into
    claimed creations.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-") and token != "-":
            yield token
        index += 2 if _consumes_operand(token, client) else 1


_LOOKS_LIKE_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Options whose VALUE is a destination rather than an ordinary argument.
_URL_OPTIONS = ("--url",)


def _url_option_values(args: list[str]) -> list[str]:
    """The destinations given through `--url <url>` / `--url=<url>`."""
    found: list[str] = []
    for index, token in enumerate(args):
        if token in _URL_OPTIONS and index + 1 < len(args):
            found.append(args[index + 1])
        for flag in _URL_OPTIONS:
            if token.startswith(flag + "="):
                found.append(token[len(flag) + 1:])
    return found


def _operands_of(args: list[str], client: str = "curl"):
    """The tokens that are NOT options and NOT an option's value.

    The endpoint is one of these. Scanning every token for it meant a URL
    handed to a read-only option could be mistaken for the destination —
    `curl --referer https://api.github.com/…/pulls … https://example.test/echo`
    posts to example.test, and reading the REFERRER as the target called it a
    pull-request creation.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-") and token != "-":
            index += 2 if _consumes_operand(token, client) else 1
            continue
        yield token
        index += 1


def _curl_posts(segment: list[str]) -> bool:
    for token in _options_of(segment, "curl"):
        if token in _CURL_DATA_FLAGS or token.startswith("--data"):
            return True
        if _curl_short_run_posts(token):
            return True
    return False


def _wget_posts(segment: list[str]) -> bool:
    return any(t == f or t.startswith(f + "=")
               for t in _options_of(segment, "wget") for f in _WGET_POST_FLAGS)


# HTTPie/xh default to GET with no request data and POST with some. Only BODY
# items count: `k=v` (string field), `k:=v` (raw JSON) and `k@file` (upload).
# `k==v` is a query parameter and `Header:value` is a header — neither is a
# body, and reading either as one would turn a listing into a claimed
# creation. Anchored, so a URL (`https://…`, or one carrying `?state=open`)
# can never look like a field: it fails at the very first character.
_HTTPIE_BODY_ITEM = re.compile(r"^[^:=@\s]+(?:=(?!=)|:=|@)")


def _httpie_posts(args: list[str]) -> bool:
    """…and its body items are operands too: `http --session ./foo=bar <url>`
    passes a session PATH that happens to contain `=`."""
    return any(_HTTPIE_BODY_ITEM.match(t) for t in _operands_of(args, "http"))


def _hostname_flag(args: list[str]) -> str | None:
    """`gh api --hostname ghe.corp …` — an enterprise call with no URL in it."""
    for index, token in enumerate(args):
        if token == _HOSTNAME_FLAG and index + 1 < len(args):
            return args[index + 1].casefold()
        if token.startswith(_HOSTNAME_FLAG + "="):
            return token[len(_HOSTNAME_FLAG) + 1:].casefold()
    return None


# `>` and its variants arrive as their own operator token (`>`, `>&`, `>>`),
# so the test is simply "this operator contains a redirect character".


def _url_source_is_certain(command: str) -> bool:
    """Could the single URL in this result have come from somewhere other than
    the pull-request-creating segment?

    B1's claim is "the URL that came back is the pull request you just opened",
    and it is only sound when the create's own output is what produced it. Two
    shapes break that, and both are cheap to spot:

    * the create's stdout is **redirected away** — `gh pr create >/dev/null &&
      cat /tmp/pr-url` shows a URL that provably is not the create's;
    * a follower runs **even if the create failed** — after `;` or `||`. A
      failed create prints no URL, so the only one in the result came from the
      follower. `&&` is safe: it proves the create succeeded, so its URL is in
      the output, and any second URL would trip the exactly-one rule into
      silence anyway. A pipeline (`| tee log`) carries the create's own stdout
      onward, so it is safe for the same reason.
    """
    tokens = _tokens(command)
    if tokens is None:
        return False
    # A TRAILING `&` has no segment after it, so `_segments_with_ops` has
    # nowhere to hang it — `gh pr create --fill &` would otherwise look like a
    # plain create. Read it off the token list directly.
    if tokens and "&" in tokens[-1] and tokens[-1] != "&&" and all(
            ch in _SHELL_PUNCTUATION for ch in tokens[-1]):
        return False
    segments = _segments_with_ops(tokens)
    # Only what comes AFTER the creating segment can supply the URL or mask its
    # status. A separator BEFORE it — `cd /repo; gh pr create` — is harmless,
    # and rejecting those broke the documented guarantee that the session which
    # opens a pull request always links itself.
    creator = _creating_segment_index(segments)
    if creator is None:
        return False
    for index, (op, _segment) in enumerate(segments):
        if index <= creator:
            continue
        if ">" in op:                      # stdout (or stderr) sent elsewhere
            return False
        # A NEWLINE sequences exactly like `;` — the follower runs whatever the
        # create did, and its exit status is the one the tool reports.
        if op in (";", "||") or op.strip("\r\n") == "":
            return False
        # `&` BACKGROUNDS the command before it, so the tool reports whatever
        # ran next — `gh pr create --fill & wait` exits 0 even when the create
        # failed, and its stderr still carries the existing PR's URL. `&&` is
        # a different operator and stays trusted.
        if "&" in op and op != "&&":
            return False
        if op == "|":
            # Without `set -o pipefail` a pipeline reports the LAST command's
            # status, so `gh pr create | tee log` exits 0 even when the create
            # failed because the pull request already exists — and its stderr
            # still carries THAT pull request's URL. The failed-create guard
            # cannot see through that, so the pipeline declines instead.
            return False
    # A redirect inside the creating segment itself sends its URL away — and a
    # create that is itself backgrounded (`gh pr create --fill &`) has no
    # status to read at all.
    if creator + 1 < len(segments):
        following = segments[creator + 1][0]
        if "&" in following and following != "&&":
            return False
    return ">" not in (segments[creator][0] if creator else "")


def _creating_segment_index(segments: list[tuple[str, list[str]]]) -> int | None:
    """Which simple command opens the pull request, if exactly one does."""
    found = None
    for index, (_op, segment) in enumerate(segments):
        name, args = _command_name(segment)
        creates = False
        if name in ("gh", "gh.exe") and "pr" in args:
            # ONE implementation of "which subcommand is this", shared with
            # `_gh_pr_subcommands`. They were written separately and drifted
            # immediately: teaching one about `gh pr -R o/r create` and not the
            # other left the create recognised and then rejected.
            creates = (_gh_subcommand_after(args, args.index("pr"))
                       in _GH_CREATE_SUBS)
        elif name in ("gh", "gh.exe") or name in _HTTP_CLIENTS:
            is_gh_api = name in ("gh", "gh.exe") and "api" in args
            for operation in _operations(name, args):
                match = _operation_match(name, operation, is_gh_api=is_gh_api,
                                         is_http=name in _HTTP_CLIENTS)
                if match and match[0] == "pulls_collection" and match[1]:
                    creates = True
        if creates:
            if found is not None:
                return None
            found = index
    return found


# `gh`'s inherited flags may sit between `pr` and its subcommand
# (`gh pr -R o/r create`), and `-R/--repo` takes an operand.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname", "--template",
                             "--jq", "-q"})
# `--dry-run` prints the pull request it WOULD open and opens nothing. gh also
# accepts the attached boolean spellings, so an exact-token test missed
# `--dry-run=true` — the third distinct way this one flag has been written.
_GH_DRY_RUN = "--dry-run"
_FALSEY = frozenset({"false", "0", "no", "off"})


def _is_dry_run(tokens: list[str]) -> bool:
    for token in tokens:
        if token == _GH_DRY_RUN:
            return True
        if token.startswith(_GH_DRY_RUN + "="):
            return token[len(_GH_DRY_RUN) + 1:].casefold() not in _FALSEY
    return False


def _gh_subcommand_after(args: list[str], start: int) -> str:
    """The subcommand following `pr`, skipping inherited flags."""
    index = start + 1
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return token
        index += 2 if (token in _GH_VALUE_FLAGS and "=" not in token) else 1
    return ""


_GH_HOST_VARS = ("GH_HOST", "GITHUB_HOST")


def _env_host(segment: list[str]) -> str | None:
    """`GH_HOST=ghe.corp gh pr create` — `gh help environment` defines it as
    the hostname for commands that name no other."""
    for token in segment:
        if not _ASSIGN_TOKEN.match(token):
            break
        name, _, value = token.partition("=")
        if name in _GH_HOST_VARS and value:
            host = value.casefold()
            if host not in ("github.com", "api.github.com"):
                return host
    return None


def _gh_repo_host(args: list[str]) -> str | None:
    """The host from `gh -R [HOST/]OWNER/REPO`, when one is given.

    `gh -R ghe.corp/o/r pr create` prints an enterprise URL on success, and
    with no REST target in the command there was nothing else to learn the host
    from — so the automatic link stayed silent on the ordinary GHES `gh pr`
    path.
    """
    for index, token in enumerate(args):
        value = None
        if token in ("-R", "--repo") and index + 1 < len(args):
            value = args[index + 1]
        elif token.startswith("--repo="):
            value = token[len("--repo="):]
        elif token.startswith("-R") and len(token) > 2:
            value = token[2:]
        if value and value.count("/") == 2:
            host = value.split("/", 1)[0].casefold()
            if host and host not in ("github.com", "api.github.com"):
                return host
    return None


def _gh_pr_subcommands(command: object) -> list[str] | None:
    """The subcommand of each `gh pr …` simple command, or None if unparseable.

    `gh pr create >/dev/null && gh pr view 99 --json url` is one Bash call with
    two pull requests in it; the whole-command regex saw the create and the
    response carried the OTHER PR's URL.
    """
    if not isinstance(command, str) or not command:
        return []
    tokens = _tokens(command)
    if tokens is None:
        return None
    subs: list[str] = []
    for segment in _segments(tokens):
        name, args = _command_name(segment)
        if name in ("gh", "gh.exe") and "pr" in args:
            subs.append(_gh_subcommand_after(args, args.index("pr")))
    return subs


def _api_matches(command: object) -> list[tuple[str, bool, str | None]]:
    """Every pulls-API invocation in the command, one entry per segment."""
    if not isinstance(command, str) or not command:
        return []
    tokens = _tokens(command)
    if tokens is None:
        target, write, host = _api_call_untokenised(command)
        return [(target, write, host)] if target else []
    return _api_segment_matches(tokens)


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

    matches = _api_segment_matches(tokens)
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


def _operation_match(name: str, args: list[str], *, is_gh_api: bool,
                     is_http: bool,
                     env_host: str | None = None) -> tuple[str, bool, str | None] | None:
    """``(target, is_write, enterprise_host)`` for ONE request, or None.

    "One request" is narrower than one shell command: `curl` takes several in a
    single invocation, separated by `--next`, each with its own options.
    """
    # Every place a destination can come from: a bare operand, or the value of
    # curl's documented `--url <url>` (which `_operands_of` correctly skips as
    # an option's argument, and which therefore hid the endpoint entirely).
    destinations = list(_operands_of(args, name)) + _url_option_values(args)
    # curl performs one transfer per URL in a SINGLE operation, so
    # `curl -X POST -d @body -o /dev/null …/a/pulls https://example.test/echo`
    # discards the create's response and prints the second transfer's. With
    # more than one transfer, nothing says which produced the URL that came
    # back — the same reasoning as `--next` and as two shell segments.
    transfers = [d for d in destinations if _LOOKS_LIKE_URL.match(d)]
    ambiguous = len(transfers) > 1

    target = number = host = None
    for arg in destinations:
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
        return None

    if host is None or host == "api.github.com":
        # `gh api --hostname ghe.corp repos/o/r/pulls` is an enterprise call
        # with no URL anywhere in it (documented in `gh api --help`).
        host = _hostname_flag(args) if is_gh_api else host
    enterprise = host if host and host not in ("api.github.com", "github.com") else None
    # `GH_HOST=ghe.corp gh api --method POST repos/o/r/pulls` is a supported
    # GHES creation whose host appears only in the environment — the same
    # mechanism already read for `gh pr`, which this lane was not consulting.
    if enterprise is None and is_gh_api:
        enterprise = env_host

    # An explicit method always wins over an inferred one, on BOTH clients.
    # `gh api --help`: "To send the parameters as a GET query string instead,
    # use --method GET" — so `gh api --method GET …/pulls -f state=open` is a
    # documented LISTING that carries `-f`. Reading it as a write made
    # `creates_pr` true, and B1 then told a session that had only listed pull
    # requests to record a confirmed self-link on one.
    method = _explicit_method(args, name)
    if method is None and name in _HTTPIE_CLIENTS:
        method = _positional_method(args)
    if method is not None:
        write = method == "POST"
    elif is_gh_api and any(_is_gh_field_flag(t) for t in _options_of(args, "gh")):
        write = True
    elif name in _HTTPIE_CLIENTS and _httpie_posts(args):
        write = True
    elif name == "wget" and _wget_posts(args):
        write = True
    elif name == "curl" and _curl_forces_get(args):
        write = False              # -G/--get: the data goes in the query string
    elif name == "curl" and _curl_posts(args):
        # curl's flags ONLY. `wget -d` is `--debug`, not data, and reading it
        # as a POST turned a listing into a claimed creation; wget has its own
        # branch above and HTTPie/xh theirs.
        write = True
    else:
        write = False
    return target, (write and not ambiguous), enterprise


def _operations(name: str, args: list[str]) -> list[list[str]]:
    """One argument list per request in this command.

    curl's `--next` starts a NEW operation with a clean option state, so
    `curl -X POST …/a/pulls -d @body -o /dev/null --next …/b/pulls/7` is a
    create AND a read in one invocation — and taking the first target with the
    first operation's method claimed the create had produced the second one's
    URL. Each operation is its own match, which lets the existing
    more-than-one-target rule refuse the B1 path.
    """
    if name != "curl" or "--next" not in args:
        return [args]
    groups: list[list[str]] = [[]]
    for arg in args:
        if arg == "--next":
            groups.append([])
        else:
            groups[-1].append(arg)
    return [g for g in groups if g]


def _api_segment_matches(tokens: list[str]) -> list[tuple[str, bool, str | None]]:
    """``(target, is_write, enterprise_host)`` for every request in the call."""
    matches: list[tuple[str, bool, str | None]] = []
    for segment in _segments(tokens):
        name, args = _command_name(segment)
        is_gh_api = name in ("gh", "gh.exe") and "api" in args
        is_http = name in _HTTP_CLIENTS
        if not (is_gh_api or is_http):
            continue
        env_host = _env_host(segment)
        for operation in _operations(name, args):
            match = _operation_match(name, operation, is_gh_api=is_gh_api,
                                     is_http=is_http, env_host=env_host)
            if match is not None:
                matches.append(match)
    return matches


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
    host = _api_call(command)[2]
    if host:
        return host
    tokens = _tokens(command if isinstance(command, str) else "")
    if tokens is None:
        return None
    for segment in _segments(tokens):
        name, args = _command_name(segment)
        if name in ("gh", "gh.exe") and "pr" in args:
            found = _gh_repo_host(args) or _env_host(segment)
            if found:
                return found
    return None


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


# `sh -c` / `bash -lc` and friends: the script is an ARGUMENT, so it has to be
# unwrapped rather than joined.
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "busybox"})


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
        if isinstance(value, list):
            # Codex's `shell` tool passes an ARGV LIST, not a command string —
            # `{"command": ["bash", "-lc", "gh pr create --fill"]}`. Accepting
            # only strings made `touches_github` false for every Codex shell
            # call, silently disabling PR linking on that host: the bridge
            # joined the list for its prefilter and then forwarded the original
            # payload here (Codex review, PR #182).
            parts = [part for part in value if isinstance(part, str)]
            if not parts:
                return ""
            # `sh -c <script>` carries the real command in ONE element; joining
            # the list would put `gh` after `bash -lc`, which is not command
            # position, so the detectors would still miss it.
            if (len(parts) >= 3 and Path(parts[0]).name in _SHELL_WRAPPERS
                    and parts[1].startswith("-") and "c" in parts[1]):
                return parts[2]
            return " ".join(parts)
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
    """Is this call OPENING a pull request?

    One shell call can name several pull requests, and its tool result is their
    combined output with nothing to say which produced the single URL in it.
    So the question is asked of the WHOLE call: unless exactly one segment in
    it produces a pull request, this can never claim the session opened the one
    that came back — `gh pr create >/dev/null && gh pr view 99` opened one PR
    and printed another's URL.
    """
    if is_github_mcp_create(tool_name):
        return True
    command = _command_of(tool_name, tool_input)
    if not command:
        return False

    subs = _gh_pr_subcommands(command)
    if subs is None:
        # Unparseable (ANSI-C quoting, say). The regex alone cannot be checked
        # against the ordering guard or the dry-run guard, so B1 — an
        # authorship claim nobody can withdraw — is not available here. B2 is,
        # and the model judges.
        #
        # This used to be the ONLY path B1 ever took, because a heredoc body
        # made every real `gh pr create` unparseable; the guards were being
        # skipped on 20 of 20 real fires. Now that `_tokens` strips heredoc
        # bodies first, the parsed path handles them and this branch is reached
        # by nothing in a 15,287-call sample — so closing it costs nothing
        # measurable and shuts the last unguarded route to B1.
        return False
    producing = len(subs) + len(_api_matches(command))
    if producing != 1:
        return False
    # Counting only the RECOGNISED invocations is not enough: any other segment
    # can print a URL too (`gh pr create >/dev/null && cat /tmp/pr-url`).
    if not _url_source_is_certain(command):
        return False
    if subs:
        if _is_dry_run(_tokens(command) or []):
            # `gh pr create --dry-run` prints the pull request it WOULD open.
            # If the proposed body quotes a PR URL, B1 would have claimed
            # authorship of THAT one.
            return False
        return subs[0] in _GH_CREATE_SUBS
    # An HTTP client POSTing to a pulls collection is NOT treated as a
    # creation. Recognising a write across curl/wget/httpie/xh meant parsing
    # each client's option grammar to find its method and body, and B1's
    # failure mode is an authorship claim nobody can withdraw. Measured over
    # 15,134 real tool calls, that layer decided nothing: every real creation
    # is a `gh pr create`, and none of them reached B1 through the HTTP path.
    # A `curl -X POST .../pulls` now falls to B2, where the model judges —
    # the safe direction — and `/memhub:link-pr` covers it explicitly.
    return False


def _call_failed(tool_response: object) -> bool:
    """Did the call that produced this result FAIL?

    `pr_provenance._execution_status` reads the shell shape (`exit_code`,
    `is_error`, `success`) and is reused rather than re-derived — but MCP
    results spell it `isError`, which that helper does not know and which this
    module must not teach it (pr_provenance is shared with the provenance
    path and the spec keeps it unmodified). So the camelCase spelling is
    checked here, beside the caller that needs it.
    """
    if not isinstance(tool_response, dict):
        return False
    if tool_response.get("isError") is True:
        return True
    return pr_provenance._execution_status(tool_response) == "failure"


def _response_texts(tool_response: object) -> list[str] | None:
    """Every string in a tool result, bounded. None if it is not a result shape.

    A shell result is a dict with `stdout`/`stderr`, or a bare string; an MCP
    result is a nested object. Both are handled here so the URL rule and the
    enterprise-host lookup read exactly the same bytes.
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
    return texts


_ANY_HOST_PR_RE = re.compile(
    r"https://([\w.-]+)/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"
    r"/([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]*)(?![A-Za-z0-9/])", re.I)


def _gh_reported_pr_url(tool_response: object) -> str | None:
    """The single PR URL a `gh pr` command printed, on ANY host.

    `gh` infers its host from the repository's remote, so the ordinary GHES
    `gh pr create --fill` names no host anywhere in the command — not in a URL,
    not in `-R`, not in an environment assignment — and the enterprise URL it
    prints was being dropped by the github.com-only parser.

    Accepting any host is safe HERE and nowhere else: the caller has already
    established that this command was a `gh pr` invocation, so the text being
    read is gh's own output about the repository it just acted on, not the
    stdout of an arbitrary program. The exactly-one rule still applies, so a
    second URL from anywhere silences it.

    But "gh's own output" is not the same as "written by gh": `gh pr view N
    --json body -q .body` prints a pull-request BODY, and a body saying "this
    supersedes https://github.com/evil/repo/pull/777" would otherwise choose
    the pull request this session gets pointed at — on a host of the author's
    choosing. So only a URL gh reports STRUCTURALLY counts: alone on its line,
    or as the value of a `key:` field, which is how `gh pr create` and
    `gh pr view` print one. A URL cited mid-sentence in prose does not.
    """
    texts = _response_texts(tool_response)
    if texts is None:
        return None
    found: list[str] = []
    for text in texts:
        for line in text[:pr_provenance.MAX_RESULT_TEXT_BYTES].splitlines():
            match = _gh_reported_match(line.strip())
            if match is None:
                continue
            host, owner, repo, number = match.group("host"), match.group("owner"), \
                match.group("repo"), match.group("number")
            url = f"https://{host.lower()}/{owner.lower()}/{repo.lower()}/pull/{int(number)}"
            if url not in found:
                found.append(url)
            if len(found) > 1:
                return None
    return found[0] if len(found) == 1 else None


# The exact shapes gh REPORTS a pull-request URL in, and no others.
#
# An earlier version allowed any `<word>:` prefix, which let a body whose one
# line reads `Related: https://github.com/evil/repo/pull/777` pass as gh
# metadata — `gh pr view N --json body -q .body` prints exactly that (Codex
# review, PR #182). The key is now gh's actual field name, `url`.
_PR_URL_PART = (
    r"https?://(?P<host>[\w.-]+)/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})"
    r"/(?P<repo>[A-Za-z0-9_.-]{1,100})/pull/(?P<number>[1-9][0-9]*)/?")
_GH_REPORTED_FORMS = (
    # `gh pr create`, and `gh pr view --json url -q .url`: the URL alone.
    re.compile(r"^" + _PR_URL_PART + r"$"),
    # `gh pr view`'s default table: `url:\thttps://…`.
    re.compile(r"^url:\s*" + _PR_URL_PART + r"$"),
    # `gh pr view 7 --json url` with no `--jq`: `{"url":"https://…"}`, and the
    # multi-field form `{"title":"X","url":"https://…"}`. Anchored on a line
    # that IS a JSON object, so prose containing a URL cannot reach it.
    re.compile(r'^\{.*"url"\s*:\s*"' + _PR_URL_PART + r'"'),
)


def _gh_reported_match(line: str):
    """The first reported-URL form this line is, or None."""
    for form in _GH_REPORTED_FORMS:
        match = form.match(line)
        if match is not None:
            return match
    return None

_HTML_URL_RE = re.compile(
    r'"html_url"\s*:\s*"(https://([\w.-]+)/[A-Za-z0-9][A-Za-z0-9-]{0,38}'
    r'/[A-Za-z0-9_.-]{1,100}/pull/[1-9][0-9]*)"')


def _mcp_result_host(tool_response: object) -> str | None:
    """The enterprise host named by a GitHub MCP result's own `html_url`.

    The MCP lane has no shell command, so `github_api_host` has nothing to read
    — and a GHES server's create was recognised and then dropped, because the
    default parser only knows github.com.

    This reads the `html_url` FIELD rather than scanning the text for any
    `/pull/` URL. A shell command's stdout can contain anything at all, which
    is why the shell lane takes its host from the command; an MCP result is the
    structured reply of a server whose own name had to say "github" to get
    here, and `html_url` is that reply's canonical field. Free prose in the
    body (a PR description quoting some other site) is not consulted.
    """
    for text in _response_texts(tool_response):
        match = _HTML_URL_RE.search(text[:pr_provenance.MAX_RESULT_TEXT_BYTES])
        if match:
            host = match.group(2).casefold()
            if host not in ("github.com", "api.github.com"):
                return host
    return None


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
    texts = _response_texts(tool_response)
    if texts is None:
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


def _negative_ttl_s() -> float:
    """The negative cache's window, in seconds.

    Read at CALL time, not at import: the override exists so one machine can be
    pinned without waiting for a release, and a hook process is too short-lived
    for an import-time read to mean anything different. Anything unparseable —
    junk, a negative, NaN, an infinity — is the default rather than an error,
    because this runs inside a hook where a mistyped env var must not become the
    reason linking goes quiet. ``0`` disables serving from the cache entirely.

    The module constant is the fallback, so a test that monkeypatches
    ``NEGATIVE_TTL_S`` still steers this.
    """
    raw = os.environ.get(NEGATIVE_TTL_ENV, "").strip()
    if not raw:
        return float(NEGATIVE_TTL_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(NEGATIVE_TTL_S)
    # NaN fails the lower bound and an infinity the upper, so both land on the
    # default without a separate isfinite() check.
    return value if 0 <= value < 10 ** 9 else float(NEGATIVE_TTL_S)


def _cacheable(answer: object) -> bool:
    """Is this reply a FINDING about the org's GitHub integration, or a default?

    Only `github_connected:false` is worth storing, and only when the feature is
    enabled. Server-side, `probe()` returns `ProbeResult(enabled=False)` from a
    feature-flag gate *before it runs a single query*, and `github_connected`
    defaults to False on that result object — deliberately, so a dark-launched
    feature adds no queries to every `gh pr` in the fleet. So in an
    `enabled:false` reply `github_connected:false` is **a default nobody
    computed**, and caching it filed "this org disconnected GitHub" about orgs
    whose GitHub App was connected the whole time, then latched it for a day.
    That is the real 2026-09-08 incident behind this rule (spec §4.4).

    Applied on READ as well as on write. Note what actually retires the entries
    the 24h-era code already wrote: they were keyed without an identity, so the
    key they live at is one nothing looks up any more — they are orphaned, not
    consulted-and-rejected, and either way nobody has to clear the state dir by
    hand. The read-side gate is defence in depth for anything that reaches the
    file some other way, and it is what keeps "what we cache" and "what we would
    say" from ever drifting apart.
    """
    return (isinstance(answer, dict)
            and answer.get("enabled") is not False
            and answer.get("github_connected") is False)


def _cache_path(api_base: str, scope: str = "", identity: str = "") -> Path:
    """Where a negative answer for THIS deployment, repo and identity is stored.

    ``enabled`` and ``github_connected`` are properties of the MemHub ORG that
    owns the repo, not of the deployment — one person can be in several orgs on
    one backend. Keying on the api_base alone meant a single disconnected org
    silenced linking for every other org's pull requests, without a request,
    which is exactly the silent failure the feature is meant to avoid. The repo
    is the coarsest thing in the request that determines which org answers, so
    it scopes the entry.

    ``identity`` is the third: which org answers is ultimately decided by the
    BEARER, and `resolve_bearer` prefers $MEMHUB_TOKEN, then a stored personal
    access key, then a cached OAuth token — so the identity behind a check can
    change without the user doing anything deliberate. Without it, a negative
    earned under one identity silenced every later identity on the same machine
    and repo. It is a truncated hash; the credential itself is never stored.
    """
    digest = hashlib.sha256(
        f"{api_base}\n{scope}\n{identity}".encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"{digest}.json"


def _cached_negative(api_base: str, now: float, scope: str = "",
                     identity: str = "") -> dict | None:
    """A stored `github_connected:false` answer, if fresh and still cacheable."""
    try:
        raw = json.loads(
            _cache_path(api_base, scope, identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    answer = raw.get("answer")
    if not isinstance(at, (int, float)) or not _cacheable(answer):
        return None
    # A clock that moved backwards must not pin a stale answer forever.
    if not (0 <= now - at < _negative_ttl_s()):
        return None
    return answer


def _prune(now: float, budget: int = 500) -> None:
    """Drop entries no TTL could still serve. Bounded, best effort, never fatal.

    The key includes the credential, and on the OAuth path the access token
    rotates about daily — so without this the directory gains one permanently
    dead file per rotation, per repo, per deployment, forever. Nothing else in
    the plugin prunes it.

    The horizon is twice the LARGER of the active and the default window, so a
    widened `MEMHUB_PRLINK_NEGATIVE_TTL_S` can never delete an entry that is
    still servable, and a `0` (serving disabled) still prunes on the default
    hour rather than deleting everything on sight. `budget` caps the work
    because this runs on the hook path.
    """
    horizon = 2 * max(_negative_ttl_s(), float(NEGATIVE_TTL_S))
    for seen, path in enumerate(STATE_DIR.glob("*.json")):
        if seen >= budget:
            return
        try:
            if now - path.stat().st_mtime > horizon:
                path.unlink()
        except OSError:
            pass


def _store_negative(api_base: str, answer: dict, now: float, scope: str = "",
                    identity: str = "") -> None:
    """Write one negative. The decision to write is `_cacheable`'s, at the call
    site — this stays dumb so a test can plant an entry the current rules would
    refuse and prove the READ path refuses it too."""
    try:
        import atomic_write

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write.publish(_cache_path(api_base, scope, identity),
                             json.dumps({"at": now, "answer": answer}))
        _prune(now)
    except Exception:
        pass


def breadcrumb(what: str, exc: object) -> None:
    """Why this machine went quiet — local only, best effort, never fatal.

    REDACTED, because this is the only file that persists exception text and an
    exception can carry a credential in its own arguments — `UnicodeEncodeError`
    names the entire string it failed on, and a transport error can quote a
    header. It is append-only and never rotated, so anything landing here is
    permanent. If `redact` is somehow unavailable, the exception's type goes in
    alone rather than a repr nobody screened.
    """
    try:
        import redact

        detail = redact.redact_text(f"{exc!r}")
    except Exception:  # noqa: BLE001 — a breadcrumb is never worth an exception
        detail = type(exc).__name__
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "breadcrumb").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {what}: {detail}\n")
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
        # Hashed, never stored: the file name is all that survives.
        #
        # `surrogatepass`, NOT a bare encode. os.environ decodes with
        # surrogateescape, so a $MEMHUB_TOKEN carrying one non-UTF-8 byte is a
        # str holding a lone surrogate — and a bare .encode() would raise
        # UnicodeEncodeError, whose args carry THE WHOLE TOKEN, straight into
        # the breadcrumb below. surrogatepass round-trips any lone surrogate
        # and cannot raise, so the digest is always taken and nothing throws.
        identity = hashlib.sha256(
            bearer.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into a hook
        breadcrumb("resolve", exc)
        return None

    scope = _repo_of(pr_url)
    cached = _cached_negative(api_base, now, scope, identity)
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
        # `> 0` so that pinning the TTL to zero disables the cache outright
        # rather than leaving a machine writing entries it will never read.
        if _cacheable(data) and _negative_ttl_s() > 0:
            _store_negative(api_base, data, now, scope, identity)
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
    # A create that FAILED opened nothing. `gh pr create` on a branch that
    # already has one prints THAT pull request's URL to stderr and exits
    # non-zero, and a GitHub MCP failure returns `isError` with the same shape
    # — so B1 would tell the session to record a confirmed `session_self` link
    # to a pull request somebody else opened. `pr_babysit_trigger` guards this
    # exact case and the guard was dropped on the way over.
    #
    # It only downgrades B1 → B2: the URL is still a pull request that is in
    # play, and the model judges whether it wrote the code.
    created = creates_pr(tool_name, tool_input) and not _call_failed(tool_response)

    # `api_host`, NOT `host`: `host` is the SESSION's host (claude/codex/cursor)
    # and namespaces the conversation id below. Reusing the name here silently
    # made every Codex and Cursor session id un-namespaced.
    #
    # The shell lane takes its API host from the command it just ran; the MCP
    # lane has no command, so it reads the server's own `html_url` field.
    api_host = github_api_host(_command_of(tool_name, tool_input))
    if api_host is None and is_github_mcp_tool(tool_name):
        api_host = _mcp_result_host(tool_response)
    if _gh_pr_subcommands(_command_of(tool_name, tool_input)):
        # THE GH LANE READS GH'S REPORT, on any host, and nothing else.
        #
        # `gh pr view 123 --json body -q .body` prints a pull-request BODY —
        # prose composed by anyone with write access to that repository. The
        # permissive extractor below finds a github.com URL anywhere in the
        # text, so a body saying "this supersedes https://github.com/evil/
        # repo/pull/777" chose the pull request this session was pointed at.
        # Applying the structural rule only to the enterprise fallback fixed
        # the enterprise half and left github.com wide open (Codex review,
        # PR #182).
        #
        # gh reports a URL alone on its line (`gh pr create`, `--json url -q`)
        # or as a `key:\tvalue` field (`gh pr view`'s table). A URL cited
        # mid-sentence is the repository's content, not gh's answer. This also
        # supplies the enterprise host, which a `gh pr` command names nowhere.
        pr_url = _gh_reported_pr_url(tool_response)
    else:
        pr_url = pr_url_from_response(tool_response, host=api_host)
    if not pr_url:
        return None
    reply = checker(pr_url)
    if not isinstance(reply, dict):
        return None
    conv_id = conversation_id_for(host, session_id) or ""
    return context_for(reply, pr_url, conv_id, created=created)


__all__ = [
    "CHECK_TIMEOUT_S", "CONNECT_ADVISORY", "CREATED", "HOSTS", "IN_PLAY",
    "NEGATIVE_TTL_ENV", "NEGATIVE_TTL_S", "REPO_ADVISORY", "STATE_DIR",
    "breadcrumb", "check",
    "context_for", "context_for_call", "conversation_id_for", "creates_pr",
    "github_api_call", "github_api_host", "is_gh_pr_command", "is_gh_pr_create",
    "is_github_mcp_create", "is_github_mcp_tool", "pr_url_from_response",
    "touches_github",
]
