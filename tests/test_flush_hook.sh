#!/usr/bin/env bash
# Executable proof of the PostToolUse flush hook's two-stage prefilter.
#
# Runs the EXACT command string from claude-hooks.json (JSON-decoded, like the
# hook runtime does) against synthetic hook inputs; the command hands the
# payload to scripts/hook_entry.py. Documents two semantics that have
# repeatedly confused reviewers:
#
#   1. Stage 1 is a shell-`case` glob (`*git*commit*` / `*gh*pr*`) over
#      tool_input.command, the same field stage 2 reads — so it is never
#      narrower than stage 2. Hook JSON like {"command": "gh pr create"}
#      reaches the flush script; tool output never opens stage 1.
#   2. Stage 2 (flush_prefilter.py) decides on tool_input.command ALONE, so
#      stdout that merely mentions "git commit" / "gh pr" stays silent.
#
# Usage: bash tests/test_flush_hook.sh   (from repo root)
set -u
cd "$(dirname "$0")/.." || exit 1
export CLAUDE_PLUGIN_ROOT="$PWD/plugins/memhub"
# PostToolUse holds several Bash-matching blocks, so select the flush block by
# its unique hook_entry.py route name, never by position. The file is
# claude-hooks.json since the multi-host rename (explicit manifest pointer;
# no host default-mounts another host's hooks).
CMD=$(python3 -c "import json; cmds=[h['command'] for e in json.load(open('plugins/memhub/hooks/claude-hooks.json'))['hooks']['PostToolUse'] for h in e['hooks'] if h['command'].endswith(' PostToolUse flush_session')]; assert len(cmds)==1, cmds; print(cmds[0])")
[ -n "$CMD" ] || { echo "FATAL: no flush hook command extracted from claude-hooks.json" >&2; exit 1; }
fail=0

run() { printf %s "$1" | bash -c "$CMD" 2>&1; }

# Reaches flush_session.py (which logs + skips on the synthetic input).
# Includes prefixed forms: env-var assignments and wrapper commands put
# `git` after a plain space, not a segment separator.
for cmd in "gh pr create --title x" "gh pr merge 5 --squash" \
           "git commit -m x" "git -C /repo commit -am fix" \
           "cd /x && git commit -m y" \
           "GIT_EDITOR=true git commit --amend" \
           "env VAR=1 git commit -m x" \
           "nohup git commit -m x" \
           "timeout 60 git commit -m x" \
           "cd /x; FOO=1 gh pr create --fill" \
           "git -c user.name=x -c user.email=y -c core.editor=vim commit -m z"; do
  out=$(run "{\"tool_input\":{\"command\":\"$cmd\"}}")
  case "$out" in
    *"[memhub-flush]"*) echo "PASS  flush ran:   $cmd" ;;
    *) echo "FAIL  arm did not run: $cmd"; fail=1 ;;
  esac
done

# Must stay SILENT (no flush spawn).
silent() {
  out=$(run "$1")
  if [ -z "$out" ]; then echo "PASS  silent:      $2"; else echo "FAIL  fired on: $2 -> $out"; fail=1; fi
}
silent '{"tool_input":{"command":"ls -la"}}' "innocent command"
silent '{"tool_input":{"command":"cat notes.md"},"tool_response":{"stdout":"run gh pr create later; also git commit"}}' "stdout mention only"
silent '{"tool_input":{"command":"git log --oneline | grep commit"}}' "cross-pipe git…commit"
silent '{"tool_input":{"command":"echo git commit"}}' "echoed mention"
silent '{"tool_input":{"command":"MSG=\"please git commit\" ls"}}' "mention inside assignment value"
silent '{"tool_input":{"command":"git diff main commit"}}' "commit as ref word after non-flag"
silent '{"tool_input":{"command":"git commit-tree abc123 -m x"}}' "commit-prefixed subcommand"

# Unset CLAUDE_PLUGIN_ROOT must fail LOUDLY (a log line), not die silently.
# The hook command itself cannot be reached without it, so run the
# dispatcher it names directly.
out=$(printf %s '{"tool_input":{"command":"git commit -m x"}}' | env -u CLAUDE_PLUGIN_ROOT python3 plugins/memhub/scripts/hook_entry.py PostToolUse flush_session 2>&1)
case "$out" in
  *"CLAUDE_PLUGIN_ROOT unset"*) echo "PASS  loud skip:   unset CLAUDE_PLUGIN_ROOT" ;;
  *) echo "FAIL  no unset-root log -> $out"; fail=1 ;;
esac

[ "$fail" -eq 0 ] && echo "ALL PASS" || echo "FAILURES PRESENT"
exit "$fail"
