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

# The GitHub REST API, on github.com and on an enterprise host. `gh api` also
# accepts a bare `repos/<owner>/<repo>/pulls` path argument.
_API_URL = re.compile(
    r"https?://api\.github\.com/repos/([\w.-]+)/([\w.-]+)/pulls(/\d+)?"
    r"|https?://[\w.-]+/api/v3/repos/([\w.-]+)/([\w.-]+)/pulls(/\d+)?",
    re.I,
)
_API_PATH = re.compile(r"(?:^|\s)/?repos/[\w.-]+/[\w.-]+/pulls(/\d+)?(?![\w.-])")
_GH_API = re.compile(_GH_PREFIX + r"\bapi\b")
_POST = re.compile(r"(?:^|\s)(?:-X\s*POST\b|-XPOST\b|--request[\s=]+POST\b|--method[\s=]+POST\b)", re.I)
_EXPLICIT_METHOD = re.compile(r"(?:^|\s)(?:-X\s*\w+|-X\w+|--request[\s=]+\w+|--method[\s=]+\w+)", re.I)
_CURL_DATA = re.compile(r"(?:^|\s)(?:-d\b|--data(?:-raw|-binary|-urlencode)?\b)")
_GH_API_WRITE = re.compile(r"(?:^|\s)(?:-f\b|-F\b|--field\b|--raw-field\b|--input\b)")
# `curl`, `http`, `wget`, `xh` — the shells people actually paste. `gh api`
# is recognised separately because its path argument has no scheme.
_HTTP_CLIENT = re.compile(r"(?:^|[;&|`\n(]|\$\()\s*(?:\w+=\S*\s+)*(?:curl|wget|http|https|xh|xhs)\b")

# The SERVER segment must name GitHub. Matching `github` anywhere in the tool
# name would catch `mcp__notes__github_summary`, which is a note-taking tool.
_MCP_GITHUB = re.compile(r"(?i)^mcp__[^_]*github[^_]*__")
# Loose on the verb, strict on the object: a server spelling it
# `open_pull_request` should not need a plugin release.
_MCP_CREATE = re.compile(r"(?i)(create|open|submit).*(pull.?request|\bpr\b)")

MAX_COMMAND_CHARS = pr_provenance.MAX_COMMAND_CHARS


def _unquoted(command: object) -> str:
    """The command with quoted segments blanked, bounded like pr_provenance."""
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


def github_api_call(command: object) -> tuple[str | None, bool]:
    """``(target, is_write)`` for a REST call to GitHub's pulls API.

    ``target`` is ``"pulls_collection"`` (a POST to which OPENS a pull
    request), ``"pull_item"`` (``…/pulls/<n>`` — editing one, not opening
    one), or None. ``is_write`` is whether the command carries a POST.

    `curl` with `-d` and no explicit `-X` **is** a POST — that is the shape in
    the wild, and reading it as a GET would miss every hand-rolled PR creation.
    """
    text = _unquoted(command)
    if not text:
        return None, False
    is_gh_api = bool(_GH_API.search(text))
    is_http = bool(_HTTP_CLIENT.search(text))
    if not (is_gh_api or is_http):
        return None, False

    match = _API_URL.search(text)
    number = None
    if match:
        number = match.group(3) or match.group(6)
    elif is_gh_api:
        path = _API_PATH.search(text)
        if not path:
            return None, False
        number = path.group(1)
    else:
        return None, False

    target = "pull_item" if number else "pulls_collection"
    if _POST.search(text):
        return target, True
    if is_gh_api and _GH_API_WRITE.search(text):
        return target, True
    # curl's `-d` implies POST, but only when no method was named explicitly.
    if is_http and _CURL_DATA.search(text) and not _EXPLICIT_METHOD.search(text):
        return target, True
    return target, False


def _mcp_tool_name(tool_name: object) -> str:
    return tool_name if isinstance(tool_name, str) else ""


def is_github_mcp_tool(tool_name: object) -> bool:
    return bool(_MCP_GITHUB.match(_mcp_tool_name(tool_name)))


def is_github_mcp_create(tool_name: object) -> bool:
    name = _mcp_tool_name(tool_name)
    if not _MCP_GITHUB.match(name):
        return False
    segment = name.split("__", 2)[-1]
    return bool(_MCP_CREATE.search(segment))


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


def pr_url_from_response(tool_response: object) -> str | None:
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
        for url in pr_provenance.urls_from_output_text(text):
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

def _cache_path(api_base: str) -> Path:
    digest = hashlib.sha256(api_base.encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"{digest}.json"


def _cached_negative(api_base: str, now: float) -> dict | None:
    """A stored `enabled:false` / `github_connected:false` answer, if fresh."""
    try:
        raw = json.loads(_cache_path(api_base).read_text(encoding="utf-8"))
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


def _store_negative(api_base: str, answer: dict, now: float) -> None:
    try:
        import atomic_write

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write.publish(_cache_path(api_base),
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

    cached = _cached_negative(api_base, now)
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
            _store_negative(api_base, data, now)
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
    pr_url = pr_url_from_response(tool_response)
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
    "github_api_call", "is_gh_pr_command", "is_gh_pr_create",
    "is_github_mcp_create", "is_github_mcp_tool", "pr_url_from_response",
    "touches_github",
]
