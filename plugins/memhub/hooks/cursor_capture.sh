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

# Resolve an interpreter up front. Hooks run with whatever PATH the host
# gives them, and a box with only `python` (a venv, some distros) would
# otherwise run `python3`, fail, and — because the child's output is
# discarded — drop every capture SILENTLY with no log line, since
# cursor_flush.py never starts. Leave a breadcrumb where its own log lives.
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  LOG="${HOME}/.config/memhub-plugin/cursorflush/log"
  mkdir -p "$(dirname "$LOG")" 2>/dev/null \
    && printf '%s [cursor-flush] no python3/python on PATH (%s) — capture skipped\n' \
       "$(date '+%Y-%m-%d %H:%M:%S')" "${PATH}" >> "$LOG" 2>/dev/null
  printf '{"permission":"allow"}\n'
  exit 0
fi

# The verdict is the ONLY thing the agent is blocked on, so every exit path
# prints it — including this one. Exiting quiet when /tmp is full would hang
# or default-deny the user's command, which is precisely the gating this
# shim exists to prevent.
TMP="$(mktemp "${TMPDIR:-/tmp}/memhub-cursor-hook.XXXXXX")" || {
  printf '{"permission":"allow"}\n'
  exit 0
}
cat > "$TMP"
# nohup + its own stdin/stdout: `( … ) &` alone leaves the child in the
# hook's process group, so it can take SIGHUP when Cursor reaps the hook and
# lose the upload mid-flight. setsid when available adds a new session (also
# detaching the controlling terminal); nohup is the portable floor.
if command -v setsid >/dev/null 2>&1; then
  setsid nohup sh -c '"$1" "$2" "$3" < "$4"; rm -f "$4"' -- \
    "$PY" "$DIR/../scripts/cursor_flush.py" "$EVENT" "$TMP" </dev/null >/dev/null 2>&1 &
else
  nohup sh -c '"$1" "$2" "$3" < "$4"; rm -f "$4"' -- \
    "$PY" "$DIR/../scripts/cursor_flush.py" "$EVENT" "$TMP" </dev/null >/dev/null 2>&1 &
fi
printf '{"permission":"allow"}\n'
