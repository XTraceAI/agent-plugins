"""Self-test for save_artifact.py's routing (audit #5/#10).

- `--help` works under a bare python3, and nothing imports the mcp SDK.
- On a room-cache miss the repo's room is resolved from the server over the
  already-open session (`brain_resolve.resolve_repo_brain`), so a hand-saved
  artifact lands in the repo room whenever one exists.
- `--no-room` / `--agent-brain-id` still bypass resolution entirely.
- A cached room the backend answers "Agent brain not found" for is evicted,
  since session capture no longer does it as a side effect.
- `--attach` ships a deliverable's BYTES as the artifact's file bundle, and
  refuses the shapes the server would reject anyway (missing file, colliding
  bundle path, an `--entrypoint` naming nothing attached).

Run: python3 tests/save_artifact_test.py  (stdlib only; only the HTTP layer,
mcp_http.request, is faked — the real mcp_http Session/ToolResult path runs).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        FAILS += 1


# --help, and the script as a whole, run under a bare python3: no SDK, no uv.
proc = subprocess.run([sys.executable, str(SCRIPTS / "save_artifact.py"), "--help"],
                      capture_output=True, text=True)
check(proc.returncode == 0 and "--no-room" in proc.stdout, f"--help under bare python3: rc={proc.returncode}")
src = (SCRIPTS / "save_artifact.py").read_text(encoding="utf-8")
check("from mcp" not in src and "import mcp." not in src and "uv run" not in src,
      "no mcp SDK import and no uv launcher left in save_artifact.py")

# Only the HTTP layer is faked: every call goes through the REAL
# mcp_http.Session -> mcp_http.call_tool -> ToolResult path, so a transport
# mismatch (an SDK-only attribute, a different result shape) fails here.
import mcp_http  # noqa: E402

calls: list[dict] = []
timeouts: list = []


class _Reply:
    """The JSON-RPC ``result`` the fake server answers tools/call with."""
    structuredContent = {"id": "art-1", "action": "created"}
    content: list = []
    isError = False


def fake_request(url, bearer, method, params=None, timeout=None):
    assert method == "tools/call", method
    assert bearer == "test-key", bearer
    calls.append({"tool": params["name"], **(params.get("arguments") or {})})
    timeouts.append(timeout)
    out = {"content": [{"type": "text", "text": t} for t in _Reply.content]}
    if _Reply.structuredContent is not None:
        out["structuredContent"] = _Reply.structuredContent
    if _Reply.isError:
        out["isError"] = True
    return out


mcp_http.request = fake_request

import save_artifact as sa  # noqa: E402

sa.resolve_url_and_auth = lambda *a, **k: (
    "http://test", {"Authorization": "Bearer test-key"}, None)

# open_session is the REAL _memhub_auth one: the static bearer in the headers
# rides straight through to mcp_http, as a stored access key does.
sa.env_for_url = lambda u: "staging"

resolved: list = []


async def fake_resolve(session, cwd, env):
    resolved.append((cwd, env))
    return {"brain_id": "B-RESOLVED", "name": "Repo: x/y"}


sa.resolve_repo_brain = fake_resolve


def run(*argv: str) -> int:
    calls.clear(); resolved.clear(); timeouts.clear()
    sys.argv = ["save_artifact.py", *argv]
    return asyncio.run(sa.main())


with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "repo"
    repo.mkdir()
    doc = repo / "spec.md"
    doc.write_text("# Spec\nbody\n", encoding="utf-8")
    sa.repo_root = lambda d: repo          # the file lives in a repo…
    sa.read_room = lambda cwd, env: None   # …whose room is not cached

    print("cache miss → resolved over the open session")
    rc = run("--file", str(doc), "--name", "Spec: X")
    check(rc == 0, "exit 0")
    check(resolved == [(doc.resolve().parent, "staging")], f"resolve_repo_brain(session, <file dir>, env) called once: {resolved}")
    check(calls[-1]["tool"] == "save_artifact" and calls[-1].get("agent_brain_id") == "B-RESOLVED",
          "saved into the resolved room")
    check(timeouts == [mcp_http.SDK_READ_TIMEOUT_S],
          f"the save waits as long as the SDK did, not the 60s default: {timeouts}")

    print("cache hit → no resolution")
    sa.read_room = lambda cwd, env: {"brain_id": "B-CACHED", "name": "Repo: x/y"}
    run("--file", str(doc), "--name", "Spec: X")
    check(resolved == [] and calls[-1].get("agent_brain_id") == "B-CACHED", "cached brain used, resolver untouched")
    check("org_id" not in calls[-1], "a room with no org recorded sends none (default org)")

    print("room in a non-default org → its org rides along")
    sa.read_room = lambda cwd, env: {"brain_id": "B-CACHED", "name": "Repo: x/y", "org_id": "ORG-2"}
    run("--file", str(doc), "--name", "Spec: X")
    check(calls[-1].get("agent_brain_id") == "B-CACHED" and calls[-1].get("org_id") == "ORG-2",
          "org_id sent with the room's brain id")

    print("overrides")
    sa.read_room = lambda cwd, env: None
    run("--file", str(doc), "--name", "Spec: X", "--no-room")
    check(resolved == [] and "agent_brain_id" not in calls[-1], "--no-room: neither cache nor resolver, personal memory")
    run("--file", str(doc), "--name", "Spec: X", "--agent-brain-id", "B-EXPLICIT")
    check(resolved == [] and calls[-1].get("agent_brain_id") == "B-EXPLICIT", "--agent-brain-id wins without resolving")
    check("org_id" not in calls[-1], "an explicit brain with no --org-id sends none (default org)")
    run("--file", str(doc), "--name", "Spec: X", "--agent-brain-id", "B-EXPLICIT", "--org-id", "ORG-3")
    check(calls[-1].get("agent_brain_id") == "B-EXPLICIT" and calls[-1].get("org_id") == "ORG-3",
          "--org-id rides with an explicit brain (else a non-default-org brain is 'not found')")

    print("--topic (nav spec §8.3.2a): sent when given, absent otherwise")
    sa.read_room = lambda cwd, env: {"brain_id": "B-CACHED", "name": "Repo: x/y"}
    run("--file", str(doc), "--name", "Spec: X", "--topic", " billing ")
    check(calls[-1].get("topic") == "billing", f"--topic sent stripped: {calls[-1].get('topic')!r}")
    run("--file", str(doc), "--name", "Spec: X")
    check("topic" not in calls[-1], "no --topic → no topic key (an older backend sees the old call)")
    run("--file", str(doc), "--name", "Spec: X", "--topic", "   ")
    check("topic" not in calls[-1], "a blank --topic sends nothing")
    sa.read_room = lambda cwd, env: None

    print("resolver finds nothing → personal memory, still saves")
    async def none_resolve(session, cwd, env):
        resolved.append((cwd, env)); return None
    sa.resolve_repo_brain = none_resolve
    rc = run("--file", str(doc), "--name", "Spec: X")
    check(rc == 0 and len(resolved) == 1 and "agent_brain_id" not in calls[-1], "no room anywhere → saved without a brain")

    print("a refused save is not a saved artifact")
    real_structured = _Reply.structuredContent
    _Reply.structuredContent = None
    _Reply.isError = True
    _Reply.content = ["tags are required; this brain uses: billing, retries"]
    try:
        rc = run("--file", str(doc), "--name", "Spec: X")
        check(rc == 1, f"isError from the server → exit 1, not 0 (rc={rc})")
    finally:
        _Reply.structuredContent = real_structured
        _Reply.isError = False
        _Reply.content = []

    print("a plain-text reply is printed as _raw, not dropped")
    _Reply.structuredContent = None
    _Reply.content = ["saved artifact art-9"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = run("--file", str(doc), "--name", "Spec: X")
        check(rc == 0 and '"_raw": "saved artifact art-9"' in buf.getvalue(),
              f"text block read off the mcp_http result: {buf.getvalue()[-120:]!r}")
    finally:
        _Reply.structuredContent = real_structured
        _Reply.content = []

    # Session capture used to be what evicted a cached room the backend
    # disowns; sessions never name a brain now, so this writer owns it.
    print("a disowned cached room is evicted, only for this error")
    forgot: list = []
    sa.forget_room = lambda cwd=None, env=None: forgot.append((cwd, env)) or True

    def refuse(text):
        _Reply.structuredContent = None
        _Reply.isError = True
        _Reply.content = [text]

    try:
        sa.read_room = lambda cwd, env: {"brain_id": "B-DEAD", "name": "Repo: x/y"}
        refuse("Error executing tool save_artifact: Agent brain not found")
        rc = run("--file", str(doc), "--name", "Spec: X")
        check(rc == 1 and forgot == [(doc.resolve().parent, "staging")],
              f"'Agent brain not found' for a cached room → forgotten ({forgot})")
        check(len([c for c in calls if c["tool"] == "save_artifact"]) == 1,
              "no retry: the next save resolves the room again")

        forgot.clear()
        refuse("tags are required; this brain uses: billing")
        run("--file", str(doc), "--name", "Spec: X")
        check(forgot == [], "any other refusal keeps the cached room")

        forgot.clear()
        refuse("Agent brain not found")
        run("--file", str(doc), "--name", "Spec: X", "--agent-brain-id", "B-EXPLICIT")
        check(forgot == [], "an explicit --agent-brain-id is never ours to forget")
    finally:
        _Reply.structuredContent = real_structured
        _Reply.isError = False
        _Reply.content = []

with tempfile.TemporaryDirectory() as td:
    out = Path(td)
    sa.repo_root = lambda d: None          # outside any repo: personal memory
    sa.read_room = lambda cwd, env: None
    sa.resolve_repo_brain = fake_resolve

    page = out / "report.html"
    page.write_bytes(b"<html>hi</html>")
    summary = out / "summary.md"
    summary.write_text("what the report says\n", encoding="utf-8")

    print("--attach ships bytes as the file bundle")
    rc = run("--attach", str(page), "--entrypoint", "report.html",
             "--file", str(summary), "--name", "Report")
    files = calls[-1].get("files") if calls else None
    check(rc == 0, "exit 0")
    check(isinstance(files, list) and len(files) == 1, f"one file sent: {files!r}")
    check(files[0]["path"] == "report.html", "bundle path is the basename")
    check(base64.b64decode(files[0]["content_base64"]) == b"<html>hi</html>",
          "the file's real bytes round-trip through base64")
    check(files[0].get("content_type") == "text/html", "content_type guessed from the name")
    check(calls[-1].get("entrypoint") == "report.html", "entrypoint passed through")
    check(calls[-1]["content"] == "what the report says\n",
          "--file stays the searchable text body")

    print("attachment-only save (no text body)")
    rc = run("--attach", str(page), "--name", "Report")
    check(rc == 0 and calls[-1]["content"] == "" and len(calls[-1]["files"]) == 1,
          "empty content is allowed when a bundle carries the artifact")

    print("a tree keeps the paths the page references")
    assets = out / "assets"; assets.mkdir()
    chart = assets / "chart.png"; chart.write_bytes(b"\x89PNG\r\n\x1a\n")
    rc = run("--attach", str(page), "--attach", str(chart),
             "--entrypoint", "report.html", "--name", "Report")
    paths = sorted(f["path"] for f in calls[-1]["files"])
    check(rc == 0 and paths == ["assets/chart.png", "report.html"],
          f"nested asset keeps its relative path: {paths}")
    check(calls[-1].get("entrypoint") == "report.html",
          "the entrypoint still names the page at the bundle root")
    deep = out / "build" / "static" / "js"; deep.mkdir(parents=True)
    app_js = deep / "app.js"; app_js.write_text("console.log(1)\n", encoding="utf-8")
    idx = out / "build" / "index.html"; idx.write_bytes(b"<html>x</html>")
    rc = run("--attach", str(idx), "--attach", str(app_js), "--name", "Build")
    paths = sorted(f["path"] for f in calls[-1]["files"])
    check(rc == 0 and paths == ["index.html", "static/js/app.js"],
          f"common parent is the bundle root, not the filesystem root: {paths}")

    shared = out / "shared"; shared.mkdir()
    (shared / "app.js").write_text("console.log(2)\n", encoding="utf-8")
    site = out / "site"; site.mkdir()
    (site / "index.html").write_bytes(b"<script src=assets/app.js></script>")
    try:
        (site / "assets").symlink_to(Path("..") / "shared", target_is_directory=True)
    except (OSError, NotImplementedError):
        print("  (skipped: this filesystem has no symlinks)")
    else:
        rc = run("--attach", str(site / "index.html"), "--attach", str(site / "assets" / "app.js"),
                 "--entrypoint", "index.html", "--name", "Site")
        # Resolving the link instead would store `shared/app.js` and reject the
        # entrypoint, so nothing is sent at all: report that, do not index into it.
        paths = sorted(f["path"] for f in calls[-1]["files"]) if calls else []
        check(rc == 0 and paths == ["assets/app.js", "index.html"],
              f"a symlinked asset dir keeps the path the page references, not the link target: {paths}")
        check(rc == 0 and calls and calls[-1].get("entrypoint") == "index.html",
              "--entrypoint still names the page when an asset dir is a symlink")

    print("refusals")
    rc = run("--attach", str(out / "nope.png"), "--name", "Report")
    check(rc == 2 and not calls, "a missing attachment is an error, nothing is sent")
    rc = run("--attach", str(page), "--attach", str(page), "--name", "R")
    check(rc == 2 and not calls, "the same file attached twice is refused")
    rc = run("--attach", str(page), "--entrypoint", "index.html", "--name", "R")
    check(rc == 2 and not calls, "--entrypoint must name an attached file")
    rc = run("--entrypoint", "report.html", "--name", "R")
    check(rc == 2 and not calls, "--entrypoint without --attach is an error")
    rc = run("--name", "R")
    check(rc == 2 and not calls, "no --file, --stdin or --attach is an error")

print()
print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
sys.exit(1 if FAILS else 0)
