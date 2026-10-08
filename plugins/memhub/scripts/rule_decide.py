#!/usr/bin/env python3
"""Activate or reject one proposed Rulebook rule (stdlib only).

/memhub:onboard runs this (`decide(rule_id, "activate")`) to switch on the
starter rules a person picks; it is also the CLI for answering a proposed rule
from a terminal. The companion's Activate / Reject buttons send the same PATCH
themselves through `$.http.fetch` (companion/feed.ts) and no longer run it. It
is Studio's own move over REST — ``PATCH /v1/team/rulebook/rules/
{rule_id}`` with ``{"status": "active"}`` or ``{"status": "dismissed"}``, the
only two ways out of ``proposed`` — sent with the plugin's personal access key,
which that route accepts as it accepts a Studio session.

Who may: declining a proposal is the owner's (or an admin's) call, and a rule
the harness filed is owned by the person it was filed for. ACTIVATING is a
rulebook or org admin's alone — "self-approval is not review" — so for everyone
else Activate is refused, and that refusal is an answer to report, not an
error: the rule is still waiting in Studio for an admin.

Usage:  rule_decide.py <rule_id> activate|reject [--env staging|production]
        rule_decide.py url [<rule_id>] [--env staging|production]
        rule_decide.py proposed --session <session_id>

`proposed` answers which rules the harness filed from this session and are
still waiting: `{"proposed": [{"title", "rule_id", "env"}]}`, newest first.
The server is the only record of a filing (harness-tied-memory-spec §3.4a), so
this asks it — every `proposed` rule XTrace authored in the org, kept when its
`source_ref` names this session (`<session_id>#<turn>`). A backend that does
not return `source_ref` yet answers an empty list. When the server could not
be asked (no key, a transport error, an odd reply) the list is empty AND
carries `"error"`: the companion drops a waiting rule the server no longer
lists, so "could not ask" must not read as "nothing is waiting".

`url` answers where the rule opens in MemHub Studio — harness_stop.rule_url(),
the link the Stop notice ends with, so the animal links the same page — and
`{"url": ""}` where there is none (the plugin points at another MemHub). With
no rule id it is the rulebook page itself, for the demo.

Prints ONE JSON line and exits 0 whatever happened, so the caller reads the
outcome rather than an exit code:

    {"outcome": "active" | "dismissed" | "forbidden" | "decided" | "gone"
               | "wrong_env" | "no_key" | "error", "msg": "..."}
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_TIMEOUT_S = 20
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_STATUS = {"activate": "active", "reject": "dismissed"}


def env_of(url: str) -> str:
    """harness_stop.env_name(), for the URL already in hand."""
    return "staging" if "staging" in url else "production"


def _api() -> tuple[str, str, dict]:
    """(url, rest base, headers) from the plugin's stored credential; raises
    when there is none or it cannot be used without a login."""
    import pak  # noqa: PLC0415
    from _memhub_auth import resolve_url_and_auth  # noqa: PLC0415
    url, headers, _auth = resolve_url_and_auth(interactive=False)
    return url, pak.api_base(url), headers


def decide(rule_id: str, action: str, env: str = "") -> dict:
    if action not in _STATUS or not _UUID.match(rule_id or ""):
        return {"outcome": "error", "msg": f"usage: rule_decide.py <rule_id> {'|'.join(_STATUS)}"}
    try:
        url, base, headers = _api()
    except Exception as exc:  # no credential, or one that cannot be used cold
        return {"outcome": "no_key", "msg": f"not logged in ({type(exc).__name__})"}
    # A rule filed against one MemHub is not there in the other: say so rather
    # than let the other answer "not found" about a rule that exists.
    if env and env_of(url) != env:
        return {"outcome": "wrong_env",
                "msg": f"filed in {env}, but the plugin points at {env_of(url)}"}
    if not str(headers.get("Authorization", "")).startswith("Bearer "):
        return {"outcome": "no_key", "msg": "no stored access key; run /memhub:login"}
    req = urllib.request.Request(
        f"{base}/v1/team/rulebook/rules/{rule_id}", method="PATCH",
        data=json.dumps({"status": _STATUS[action]}).encode(),
        headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("msg") or ""
        except (ValueError, AttributeError):
            msg = ""
        outcome = {403: "forbidden", 404: "gone", 400: "decided", 409: "decided"}.get(e.code, "error")
        return {"outcome": outcome, "msg": str(msg)[:200] or f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"outcome": "error", "msg": str(e)[:200]}
    # REST envelope: a non-zero code is a failure the transport reported as 200.
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        return {"outcome": "error", "msg": str(payload.get("msg") or payload)[:200]}
    data = payload.get("data") if isinstance(payload, dict) else None
    status = (data or {}).get("status") if isinstance(data, dict) else None
    return {"outcome": status or _STATUS[action], "msg": ""}


def proposed(session: str) -> dict:
    """The rules the harness filed from `session` that are still `proposed`.
    Never raises: no credential, a transport error or an odd reply is an empty
    list with an `error` — nothing to announce, but not proof that nothing is
    waiting, so the companion keeps the asks it already shows."""
    if not session:
        return {"proposed": []}
    try:
        url, base, headers = _api()
        if not str(headers.get("Authorization", "")).startswith("Bearer "):
            # decide()'s rule: only the stored access key, never a login flow
            return {"proposed": [], "error": "no_key"}
        req = urllib.request.Request(
            f"{base}/v1/team/rulebook/rules?status=eq.proposed&author=eq.xtrace"
            "&order=created_at.desc", headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read() or b"{}")
    except Exception as exc:
        return {"proposed": [], "error": type(exc).__name__}
    data = payload.get("data") if isinstance(payload, dict) else None
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return {"proposed": [], "error": "unexpected reply"}
    mine = [r for r in rules if isinstance(r, dict)
            and str(r.get("source_ref") or "").startswith(f"{session}#")]
    return {"proposed": [{"title": str(r.get("title") or ""), "rule_id": str(r.get("rule_id") or ""),
                          "env": env_of(url)} for r in mine]}


def studio_url(rule_id: str, env: str = "") -> str:
    """harness_stop.rule_url(): one mapping from the API to its web app."""
    if rule_id and not _UUID.match(rule_id):
        return ""
    try:
        import harness_stop  # noqa: PLC0415
        env = env or harness_stop.env_name()
        # rule_url needs an id; the demo, which has none, takes the page it opens on
        link = harness_stop.rule_url(rule_id or "00000000-0000-0000-0000-000000000000", env)
    except Exception:
        return ""
    return link if rule_id else link.split("?", 1)[0]


def main(argv: list[str]) -> int:
    args = list(argv)
    env = ""
    if "--env" in args:
        i = args.index("--env")
        env = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    if args[:1] == ["proposed"]:
        session = args[args.index("--session") + 1] if "--session" in args[:-1] else ""
        print(json.dumps(proposed(session)))
        return 0
    if args[:1] == ["url"]:
        print(json.dumps({"url": studio_url((args + [""])[1], env)}))
        return 0
    rule_id, action = (args + ["", ""])[:2]
    print(json.dumps(decide(rule_id, action, env)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
