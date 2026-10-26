"""Cheap stdin gate for the reactive (PostToolUse) directive recall hook.

Exit 0 iff the hook input's ``tool_response`` looks like a FAILURE — only then
is it worth paying the ``uv run`` + MCP round-trip of ``directive_recall.py``.
Plain stdlib and no imports beyond json/re/sys, so the common case (a
successful tool call) costs one fast python3 startup and nothing else.
Mirrors ``flush_prefilter.py`` / ``directive_prefilter.py``: the shell command
in hooks.json only proceeds when this exits 0. Fail-closed here is fail-open
for the agent — on any parse problem we exit 1 and simply skip recall.
"""
import json
import re
import sys

# Keep in sync with _ERROR_RE in directive_recall.py (the authoritative gate —
# this one only exists to skip the uv startup on quiet successes).
_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|\b[A-Z][a-zA-Z]*Error\b"
    r"|\bERROR\b|\bError\b|error:|✘|npm ERR!|FAILED\b|fatal:|Exception\b"
    r"|command not found|No such file or directory)"
)


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        resp = data.get("tool_response")
        if resp is None:
            return 1
        if isinstance(resp, dict):
            parts = [v for k in ("stderr", "stdout", "output", "error", "text")
                     if isinstance(v := resp.get(k), str) and v]
            # Raw parts, never json.dumps — dumps escapes non-ASCII, so the
            # ✘ failure marker could never match through it.
            text = "\n".join(parts) if parts else json.dumps(resp, ensure_ascii=False)
        else:
            text = str(resp)
        return 0 if _ERROR_RE.search(text[-4000:]) else 1
    except Exception:  # noqa: BLE001 — a broken gate must skip, never crash
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
