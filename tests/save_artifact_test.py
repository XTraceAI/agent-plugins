"""Self-test for save_artifact.py's routing (audit #5/#10).

- `--help` works under a bare python3: the mcp SDK is imported after argparse.
- On a room-cache miss the repo's room is resolved from the server over the
  already-open session (`brain_resolve.resolve_repo_brain`), so a hand-saved
  artifact lands in the repo room whenever one exists.
- `--no-room` / `--agent-brain-id` still bypass resolution entirely.
- A cached room the server disowns ("Agent brain not found") is evicted, so the
  next save resolves again; an explicit `--agent-brain-id` never is.

Run: python3 tests/save_artifact_test.py  (stdlib only; the SDK is faked).
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        FAILS += 1


# --help must not need the mcp SDK (the hook/skill docs run it under python3)
proc = subprocess.run([sys.executable, str(SCRIPTS / "save_artifact.py"), "--help"],
                      capture_output=True, text=True)
check(proc.returncode == 0 and "--no-room" in proc.stdout, f"--help under bare python3: rc={proc.returncode}")

# Fake the SDK so main() can run end-to-end without a server.
calls: list[dict] = []


class _Result:
    structuredContent = {"id": "art-1", "action": "created"}
    content: list = []


class _Missing:
    """The server's answer for an id that is not a brain it has."""
    structuredContent = None
    isError = True
    content = [types.SimpleNamespace(
        type="text", text="Error executing tool save_artifact: Agent brain not found")]


RESULT: list = [_Result()]


class _Session:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def initialize(self): pass
    async def call_tool(self, name, arguments=None):
        calls.append({"tool": name, **(arguments or {})})
        return RESULT[0]


class _Transport:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return (None, None, None)
    async def __aexit__(self, *a): return False


mcp = types.ModuleType("mcp"); cli = types.ModuleType("mcp.client")
sess = types.ModuleType("mcp.client.session"); sess.ClientSession = _Session
sh = types.ModuleType("mcp.client.streamable_http"); sh.streamablehttp_client = _Transport
sys.modules.update({"mcp": mcp, "mcp.client": cli, "mcp.client.session": sess,
                    "mcp.client.streamable_http": sh})

import save_artifact as sa  # noqa: E402

sa.resolve_url_and_auth = lambda *a, **k: ("http://test", {}, None)
sa.env_for_url = lambda u: "staging"

resolved: list = []


async def fake_resolve(session, cwd, env):
    resolved.append((cwd, env))
    return {"brain_id": "B-RESOLVED", "name": "Repo: x/y"}


sa.resolve_repo_brain = fake_resolve


def run(*argv: str) -> int:
    calls.clear(); resolved.clear()
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

    print("cache hit → no resolution")
    sa.read_room = lambda cwd, env: {"brain_id": "B-CACHED", "name": "Repo: x/y"}
    run("--file", str(doc), "--name", "Spec: X")
    check(resolved == [] and calls[-1].get("agent_brain_id") == "B-CACHED", "cached brain used, resolver untouched")

    print("overrides")
    sa.read_room = lambda cwd, env: None
    run("--file", str(doc), "--name", "Spec: X", "--no-room")
    check(resolved == [] and "agent_brain_id" not in calls[-1], "--no-room: neither cache nor resolver, personal memory")
    run("--file", str(doc), "--name", "Spec: X", "--agent-brain-id", "B-EXPLICIT")
    check(resolved == [] and calls[-1].get("agent_brain_id") == "B-EXPLICIT", "--agent-brain-id wins without resolving")

    print("resolver finds nothing → personal memory, still saves")
    async def none_resolve(session, cwd, env):
        resolved.append((cwd, env)); return None
    sa.resolve_repo_brain = none_resolve
    rc = run("--file", str(doc), "--name", "Spec: X")
    check(rc == 0 and len(resolved) == 1 and "agent_brain_id" not in calls[-1], "no room anywhere → saved without a brain")

    print("server disowns the cached room → evicted, so the next save resolves again")
    forgot: list = []
    sa.forget_room = lambda cwd=None, env=None: forgot.append((cwd, env)) or True
    sa.read_room = lambda cwd, env: {"brain_id": "B-DEAD", "name": "Repo: x/y"}
    RESULT[0] = _Missing()
    rc = run("--file", str(doc), "--name", "Spec: X")
    check(rc == 0 and forgot == [(doc.resolve().parent, "staging")],
          f"dead cached room forgotten for the file's repo: {forgot}")
    forgot.clear()
    run("--file", str(doc), "--name", "Spec: X", "--agent-brain-id", "B-EXPLICIT")
    check(forgot == [], "an explicit --agent-brain-id is never evicted from the cache")
    RESULT[0] = _Result()

print()
print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
sys.exit(1 if FAILS else 0)
