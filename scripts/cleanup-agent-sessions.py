#!/usr/bin/env python3
"""Delete the production sessions one workflow attempt captured — and only those.

Reads the run-owned manifest ``check-agent-session.py --session-manifest``
wrote and issues one ``DELETE /v1/team/conversations?session_id=eq.<id>`` per
listed session with the dedicated test account's key (ENG-1074). Exact ids
from the manifest, never a listing or a pattern, so fixtures, rules and every
session this attempt did not create are untouchable from here.

Runs from an ``always()`` step, so it must cope with every way the agent job
can end: no manifest (the run never reached a session), a manifest with no
sessions, a key that is not provisioned, and a backend that has not shipped
key-authenticated deletion yet. Each session is attempted independently — one
failure never skips the rest — and each outcome is reported by name in the
cleanup report and the job summary, separately from the release evidence.

Verification is a second DELETE: the backend answers ``404
conversation_not_found`` for an id that resolves to nothing, so a 404 after a
200 proves the session is gone, and a second 200 proves a capture flush
re-created it after the harness exited — reported as ``recreated``, deleted
once more, and counted as a failure so the race is visible rather than
silently absorbed.

The capture hooks are asynchronous. A run that died after announcing its
session may still have a flush in flight when this runs, and that flush can
land AFTER a first "gone" answer — an initial 404 is then not the end of the
story. The manifest says whether the run saw every flush acknowledged
(``captured``); for a session it did not, "gone" is re-checked once after a
bounded wait, and whatever landed in between is deleted and reported.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse

from release_check_lib import compat, read_json

ROOT = Path(__file__).resolve().parents[1]
DELETE_PATH = "/v1/team/conversations"
NOT_FOUND = "conversation_not_found"
OK = frozenset({"deleted", "absent"})
_TIMEOUT_S = 30
#: How long an unacknowledged session's flush is given to land before "gone"
#: is believed. The agent check itself waits 240 s for the acknowledgement; a
#: flush that has not landed 90 s after the host and its hooks were killed is
#: not coming, and one session per job keeps the wait affordable.
LATE_CAPTURE_WINDOW_S = 90


def load_manifest(path):
    """The manifest's sessions, or ``None`` when there is no manifest at all."""
    path = Path(path)
    if not path.is_file():
        return None
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise compat.GateError("session manifest is malformed")
    sessions = []
    for row in manifest.get("sessions", []):
        hid, org = row.get("harness_session_id"), row.get("org_id")
        if not (isinstance(hid, str) and re.fullmatch(r"(codex-|cursor-)?[A-Za-z0-9_-]{8,128}", hid)
                and isinstance(org, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", org)):
            raise compat.GateError("session manifest carries an unusable session entry")
        sessions.append({"host": row.get("host"), "harness_session_id": hid, "org_id": org,
                         "captured": row.get("captured") is True})
    return sessions


def _reason(exc):
    """The backend's ``data.reason`` out of a transport error, or ``None``.

    ``mcp_http.rest`` folds the (truncated) error body into the message after
    ``"failed (<status>): "``; the envelope this needs is ~90 bytes, so it is
    always intact. Parsed, not pattern-matched, so a proxy page that happens
    to contain the words cannot pass as the backend's answer.
    """
    _head, sep, body = str(exc).partition("): ")
    if not sep:
        return None
    try:
        data = json.loads(body).get("data")
    except (ValueError, AttributeError):
        return None
    return data.get("reason") if isinstance(data, dict) else None


def _delete(rest, token, session):
    """One DELETE. Returns ``(status, reason)``: the HTTP code, and on a 404
    the backend's ``data.reason`` — which is what distinguishes "this id
    resolves to nothing" from a proxy or route 404 that proved nothing."""
    url = compat.PRODUCTION + DELETE_PATH + "?session_id=eq." + urllib.parse.quote(
        session["harness_session_id"], safe="")
    try:
        reply = rest(url, token, method="DELETE", headers={"X-Org-Id": session["org_id"]},
                     timeout=_TIMEOUT_S)
        return reply.status, None
    except Exception as exc:  # McpError and anything the transport raised
        status = getattr(exc, "status", None)
        return status, (_reason(exc) if status == 404 else None)


def _gone(status, reason):
    """Only the backend's own reason counts: a bare 404 is a proxy or a
    missing route, and proves nothing about the session."""
    return status == 404 and reason == NOT_FOUND


def _verify(rest, token, session, result):
    """Prove ``result`` with one more DELETE. A 200 here means a capture flush
    re-created the session in between — the call just deleted it again, and
    the race is the finding."""
    status, reason = _delete(rest, token, session)
    if _gone(status, reason):
        return result
    if status == 200:
        return {"outcome": "recreated", "http_status": status}
    return {"outcome": "unverified", "http_status": status}


def cleanup_session(rest, token, session, sleep=time.sleep):
    """Delete one session and prove it is gone. Never raises; the outcome is
    one of a fixed vocabulary so the report holds no server strings."""
    status, reason = _delete(rest, token, session)
    if status in (401, 403):
        return {"outcome": "unauthorized", "http_status": status}
    if status == 200:
        result = _verify(rest, token, session, {"outcome": "deleted"})
    elif _gone(status, reason):
        # Already gone, never captured, or not this key's session — every one
        # of those means "nothing of ours remains", and a retried cleanup lands
        # here by design.
        result = {"outcome": "absent"}
    else:
        return {"outcome": "failed", "http_status": status}
    if result["outcome"] in OK and not session.get("captured"):
        # The run never saw its flushes acknowledged, so one may still be in
        # flight and land after the answer above. Wait it out, then look
        # again; a session that appears now is deleted and verified like any
        # other, and named as late so the report shows the race happened.
        sleep(LATE_CAPTURE_WINDOW_S)
        status, reason = _delete(rest, token, session)
        if status == 200:
            return _verify(rest, token, session, {"outcome": "deleted", "late_capture": True})
        if not _gone(status, reason):
            return {"outcome": "unverified", "http_status": status}
    return result


def summarize(results):
    counts = {}
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()

    report = {"schema_version": 1, "sessions": [], "ok": True}
    try:
        sessions = load_manifest(args.manifest)
    except compat.GateError as exc:
        sessions = None
        report.update(ok=False, error=str(exc))
    if sessions is None and report["ok"]:
        report["note"] = "no session manifest; the run never captured a production session"
    elif not sessions and report["ok"]:
        report["note"] = "session manifest lists no sessions"
    elif sessions:
        token = os.environ.get("MEMHUB_PROD_E2E_TOKEN", "")
        if not token.startswith("mhk_") or any(c.isspace() for c in token):
            report.update(ok=False, error="MEMHUB_PROD_E2E_TOKEN is not provisioned; sessions were NOT deleted")
            report["sessions"] = [dict(s, outcome="skipped") for s in sessions]
        else:
            http = compat.load_module("cleanup_http", ROOT / "plugins/memhub/scripts/mcp_http.py")
            for session in sessions:
                result = cleanup_session(http.rest, token, session)
                report["sessions"].append({**session, **result})
                print(f"{session['host']} {session['harness_session_id']}: {result['outcome']}")
            report["counts"] = summarize(report["sessions"])
            report["ok"] = all(r["outcome"] in OK for r in report["sessions"])

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")

    line = "Session cleanup: " + (
        "ok" if report["ok"] else "FAILED") + (
        f" — {report['note']}" if report.get("note") else "") + (
        f" — {report['error']}" if report.get("error") else "") + (
        " — " + ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items()))
        if report.get("counts") else "")
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write(line + "\n")
    if not report["ok"]:
        print("::warning::" + line)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
