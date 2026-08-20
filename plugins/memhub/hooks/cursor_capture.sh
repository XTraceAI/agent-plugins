#!/bin/sh
# Instant-ack shim between cursor hooks and cursor_flush.py.
#
# Cursor's before* hooks BLOCK the agent on our stdout for a permission
# verdict, and a flush is a network round trip — so answer the contract
# immediately and run the flusher DETACHED on a copy of the payload. The
# verdict is always "allow": capture observes, it never gates.
#
# Paths are derived from this script's own location: CURSOR_PLUGIN_ROOT was
# observed pointing at a DIFFERENT plugin's cache (Spike C) — never trust it.
EVENT="${1:-unknown}"
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp "${TMPDIR:-/tmp}/memhub-cursor-hook.XXXXXX")" || exit 0
cat > "$TMP"
# nohup + its own stdin/stdout: `( … ) &` alone leaves the child in the
# hook's process group, so it can take SIGHUP when Cursor reaps the hook and
# lose the upload mid-flight. setsid when available adds a new session (also
# detaching the controlling terminal); nohup is the portable floor.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup sh -c 'python3 "$1" "$2" < "$3"; rm -f "$3"' -- \
    "$DIR/../scripts/cursor_flush.py" "$EVENT" "$TMP" </dev/null >/dev/null 2>&1 &
else
  nohup sh -c 'python3 "$1" "$2" < "$3"; rm -f "$3"' -- \
    "$DIR/../scripts/cursor_flush.py" "$EVENT" "$TMP" </dev/null >/dev/null 2>&1 &
fi
printf '{"permission":"allow"}\n'
