"""Self-test for the SessionStart repo-brain brief.

Covers the properties that decide whether this hook is safe to run before every
session's first prompt:

* ``brief`` makes NO network call and imports no transport module — it runs on
  the synchronous SessionStart path, where every millisecond is one the user
  waits;
* an unonboarded repo (no cached room) is SILENT, because a hook that nags in
  every checkout that is not the user's own is a hook they turn off;
* the agent-facing ``additionalContext`` is emitted every session, while the
  user-facing ``systemMessage`` fires only when the brain CHANGES — the
  every-session-noise failure ``capture_health`` exists to avoid;
* the brief tells the truth when per-turn capture is switched off, rather than
  claiming writes land in a brain they do not reach;
* a cached overview is injected and clipped, so a long digest cannot become a
  silent per-session token tax;
* ``refresh`` is throttled and never raises, whatever the cache contains.

Run: python3 tests/brain_brief_test.py  (from the repo root; stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Redirect HOME before importing so the module's import-time CACHE_DIR resolves
# inside the sandbox and no test can read or write the real ~/.config. Both
# spellings: POSIX expanduser reads HOME; Windows reads USERPROFILE and never
# consults HOME.
_TMP_HOME = tempfile.mkdtemp(prefix="brain-brief-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ.pop("MEMHUB_STATE_DIR", None)
os.environ.pop("MEMHUB_TURN_FLUSH", None)
os.environ.pop("MEMHUB_MCP_BASE_URL", None)

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import brain_brief  # noqa: E402
import room_map  # noqa: E402

BRAIN = "4e95c672-d00c-4cfd-8b7a-c2bd384fe53f"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _run(cmd: str, payload: dict) -> dict:
    """Invoke the script the way the hook does, and parse what it printed."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "brain_brief.py"), cmd],
        input=json.dumps(payload), capture_output=True, text=True,
        env={**os.environ, "HOME": _TMP_HOME, "USERPROFILE": _TMP_HOME},
    )
    check(f"{cmd}: exit 0", proc.returncode == 0)
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        check(f"{cmd}: stdout is JSON", False)
        return {}


def _stub_room(monkey: dict | None) -> None:
    """Pin what `read_room` returns — the tests are about the brief, not about
    room resolution, which room_map_test.py already covers."""
    brain_brief.room_map.read_room = lambda cwd=None, env=None: monkey  # type: ignore[assignment]
    brain_brief.room_map.current_env = lambda: "staging"  # type: ignore[assignment]


def _ctx(out: dict) -> str:
    return str(out.get("hookSpecificOutput", {}).get("additionalContext") or "")


# ── silence where silence is correct ───────────────────────────────────────
_stub_room(None)
out = brain_brief.cmd_brief({"cwd": "/tmp/nowhere"})
check("unonboarded repo prints nothing", out == 0)

# ── the brief itself ───────────────────────────────────────────────────────
room = {"brain_id": BRAIN, "name": "Repo: XTraceAI/memhub-claude-plugin"}
_stub_room(room)

import io  # noqa: E402
import contextlib  # noqa: E402


def _brief(payload: dict | None = None) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        brain_brief.cmd_brief(payload or {"cwd": "/repo"})
    raw = buf.getvalue().strip()
    return json.loads(raw) if raw else {}

first = _brief()
check("names the brain in agent context", BRAIN in _ctx(first))
check("names the room in agent context",
      "Repo: XTraceAI/memhub-claude-plugin" in _ctx(first))
check("states it is the default target", "DEFAULT target" in _ctx(first))
check("first resolution reaches the USER", "systemMessage" in first)

second = _brief()
check("agent context is emitted every session", BRAIN in _ctx(second))
check("unchanged brain does NOT re-notify the user",
      "systemMessage" not in second)

_stub_room({"brain_id": OTHER, "name": "Repo: XTraceAI/other"})
changed = _brief()
check("a CHANGED brain notifies the user again", "systemMessage" in changed)

# ── truthfulness about writes ──────────────────────────────────────────────
_stub_room(room)
os.environ["MEMHUB_TURN_FLUSH"] = "0"
off = _brief()
check("says so when capture is switched off", "capture is OFF" in _ctx(off))
os.environ.pop("MEMHUB_TURN_FLUSH")
on = _brief()
check("says sessions are captured when it is on",
      "captured into it automatically" in _ctx(on))

# ── the overview: injected, and clipped ────────────────────────────────────
cache = brain_brief._cache_path("staging", BRAIN)
cache.parent.mkdir(parents=True, exist_ok=True)
cache.write_text(json.dumps({
    "overview": "The repo is a Claude Code plugin marketplace.",
    "refreshed_at": time.time(),
}), encoding="utf-8")
with_ov = _brief()
check("a cached overview is injected",
      "Claude Code plugin marketplace" in _ctx(with_ov))

cache.write_text(json.dumps({
    "overview": "x" * (brain_brief._MAX_OVERVIEW_CHARS + 500),
    "refreshed_at": time.time(),
}), encoding="utf-8")
clipped = _brief()
check("a long overview is clipped, not injected whole",
      "truncated" in _ctx(clipped)
      and len(_ctx(clipped)) < brain_brief._MAX_OVERVIEW_CHARS + 1200)

cache.unlink()
missing = _brief()
check("no cached overview points at the tool instead of asserting emptiness",
      "get_brain_overview" in _ctx(missing))

# ── refresh: throttled, and never fatal ────────────────────────────────────
cache.write_text(json.dumps({"overview": "cached", "refreshed_at": time.time()}),
                 encoding="utf-8")
check("fresh cache is left alone", brain_brief._is_fresh(cache))
cache.write_text(json.dumps({"overview": "old", "refreshed_at": 1.0}),
                 encoding="utf-8")
check("a stale cache is refetched", not brain_brief._is_fresh(cache))
cache.write_text("not json at all", encoding="utf-8")
check("a corrupt cache reads as stale, not as a crash",
      not brain_brief._is_fresh(cache))
check("a corrupt cache does not break the brief", "systemMessage" in _brief()
      or BRAIN in _ctx(_brief()))

_stub_room(None)
check("refresh with no room is a no-op", brain_brief.cmd_refresh({}) == 0)

# An unwritable cache would be permanently stale, and Stop fires every turn —
# so without this the 6-hourly digest fetch becomes a per-turn network call
# forever. `brief` reads only from the cache, so a fetch that cannot be stored
# buys nothing: the honest response is not to make it.
_stub_room(room)
_real_dir = brain_brief.CACHE_DIR
# A cache dir whose PARENT is a regular file is unwritable on every platform.
# ("/proc/…" is only guaranteed unwritable on Linux; on Windows that path
# would simply be created under the drive root.)
_blocker = Path(_TMP_HOME) / "cache-blocker"
_blocker.write_text("", encoding="utf-8")
brain_brief.CACHE_DIR = _blocker / "memhub"
check("an unwritable cache is detected", not brain_brief._cache_is_writable())


def _boom(*a, **k):  # pragma: no cover - must never be reached
    raise AssertionError("refresh attempted a network call it could not persist")


_saved_extract = brain_brief._extract_overview
brain_brief._extract_overview = _boom
check("unwritable cache skips the network call entirely",
      brain_brief.cmd_refresh({"cwd": "/repo"}) == 0)
brain_brief._extract_overview = _saved_extract
brain_brief.CACHE_DIR = _real_dir
check("a writable cache still refreshes", brain_brief._cache_is_writable())


# ── the result shape: the envelope is UNWRAPPED, not cached whole ──────────
class _Block:
    def __init__(self, text):
        self.text = text


class _Res:
    def __init__(self, content=(), structured=None):
        self.content = list(content)
        self.structured = structured
        self.is_error = False


DIGEST = "# Repo: XTraceAI/x — Overview\nThis brain is dominated by …"

# What staging actually returns: a text block holding a JSON envelope. Caching
# it verbatim looked like it worked — a plausible-looking 3.5KB cache appeared —
# while injecting JSON punctuation as "what this brain knows".
envelope = _Res([_Block(json.dumps({"agent_brain_id": BRAIN,
                                    "overview": DIGEST}))])
check("a JSON envelope is unwrapped to the digest",
      brain_brief._extract_overview(envelope) == DIGEST)
check("structuredContent wins when present",
      brain_brief._extract_overview(
          _Res([_Block("ignored")], {"overview": DIGEST})) == DIGEST)
check("a bare text block is taken as the digest",
      brain_brief._extract_overview(_Res([_Block(DIGEST)])) == DIGEST)
check("an uncompiled digest (null) reads as a cache miss",
      brain_brief._extract_overview(
          _Res([_Block(json.dumps({"overview": None}))])) == "")
check("no content at all is a cache miss", brain_brief._extract_overview(_Res()) == "")

# ── the SessionStart budget: no network module on the brief path ───────────
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r);\n"
     "import json, io, contextlib\n"
     "import brain_brief\n"
     "brain_brief.room_map.read_room = lambda cwd=None, env=None: None\n"
     "buf = io.StringIO()\n"
     "with contextlib.redirect_stdout(buf): brain_brief.cmd_brief({})\n"
     "print(json.dumps(sorted(m for m in sys.modules\n"
     "      if m in ('mcp_http', '_memhub_auth', 'urllib.request'))))"
     % str(SCRIPTS)],
    capture_output=True, text=True,
    env={**os.environ, "HOME": _TMP_HOME, "USERPROFILE": _TMP_HOME},
)
loaded = json.loads(probe.stdout.strip() or "[]") if probe.returncode == 0 else ["?"]
check("brief imports no transport/auth module", loaded == [])

# ── the CLI, as the hook actually invokes it ───────────────────────────────
cli = _run("brief", {"cwd": _TMP_HOME})
check("CLI on a non-repo dir stays quiet", cli == {})
check("CLI refresh on a non-repo dir exits clean",
      _run("refresh", {"cwd": _TMP_HOME}) == {})

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    raise SystemExit(1)
print("all brain_brief checks passed")
