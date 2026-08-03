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

# Keep in sync with _ERROR_RE / _STRONG_ERROR_RE / _READBACK_TOOLS in
# directive_recall.py (the authoritative gate — this one only exists to skip
# the uv startup on quiet successes).
_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|\b[A-Z][a-zA-Z]*Error\b"
    r"|\bERROR\b|\bError\b|error:|✘|npm ERR!|FAILED\b|fatal:|Exception\b"
    r"|command not found|No such file or directory"
    # Exit-0 commands that report failure in their payload (CI status polls).
    r"|\bFAILURE\b|\bTIMED_OUT\b)"
)

# Read-like tools return file CONTENT, so the weak markers above would match
# ordinary source (any except-clause). They are matched only to catch a
# background task's failure at the moment the agent reads the output back.
_READBACK_TOOLS = frozenset({"Read", "BashOutput"})
_STRONG_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|npm ERR!|command not found"
    r"|\bFAILURE\b|\bTIMED_OUT\b|✘|^fatal:|^FAILED\b|^E\s{3})",
    re.MULTILINE,
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
        marker = (
            _STRONG_ERROR_RE
            if (data.get("tool_name") or "") in _READBACK_TOOLS
            else _ERROR_RE
        )
        return 0 if marker.search(text[-4000:]) else 1
    except Exception:  # noqa: BLE001 — a broken gate must skip, never crash
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
