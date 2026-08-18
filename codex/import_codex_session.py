#!/usr/bin/env python3
"""DEPRECATED shim — Codex import moved to the unified multi-host entry:

    uv run --with 'mcp<2' python plugins/memhub/scripts/capture.py \
        import --host codex --session <rollout-path|session-id|latest> [...]

This wrapper keeps the documented ``codex/import_codex_session.py`` command
line working for one release by forwarding its arguments, unchanged, to
``capture.py import --host codex``. ``rollout_uuid``/``resolve_rollout`` are
re-exported for existing importers (``resolve_rollout`` is now
``readers.codex.locate``).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from readers.codex import rollout_uuid  # noqa: F401,E402
from readers.codex import locate as resolve_rollout  # noqa: F401,E402


def main() -> int:
    import capture
    sys.argv = [sys.argv[0], "import", "--host", "codex", *sys.argv[1:]]
    return capture.main()


if __name__ == "__main__":
    raise SystemExit(main())
