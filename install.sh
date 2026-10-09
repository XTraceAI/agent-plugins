#!/bin/sh
# Install the MemHub plugin for every coding agent on this machine, then sign
# the machine in once.
#
#   curl -fsSL <url>/install.sh | sh                  # production plugin
#   curl -fsSL <url>/install.sh | sh -s -- --staging  # staging, from a local clone
#
# Options:
#   --staging        install memhub-staging from a local agent-plugins-internal
#                    clone instead of the released memhub plugin
#   --source PATH    the clone --staging installs from
#                    (default: $MEMHUB_PLUGINS_SOURCE, else ~/xtrace/agent-plugins-internal)
#   --agents LIST    comma-separated subset of claude,codex,cursor (default: all found)
#   --no-login       skip the sign-in step
#
# Safe to run again: every step checks what is already there. One sign-in
# covers every agent on the machine, because the capture hooks of all three
# read the same credential under ~/.config/memhub-plugin/.
set -eu

MODE=prod
SOURCE="${MEMHUB_PLUGINS_SOURCE:-$HOME/xtrace/agent-plugins-internal}"
AGENTS=""
LOGIN=1

while [ $# -gt 0 ]; do
  case "$1" in
    --staging) MODE=staging ;;
    --source) [ $# -ge 2 ] || { echo "--source needs a path" >&2; exit 2; }; SOURCE="$2"; shift ;;
    --agents) [ $# -ge 2 ] || { echo "--agents needs a list" >&2; exit 2; }; AGENTS="$2"; shift ;;
    --no-login) LOGIN=0 ;;
    -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "$MODE" = staging ]; then
  [ -f "$SOURCE/.claude-plugin/marketplace.json" ] \
    || fail "no agent-plugins-internal clone at $SOURCE (pass --source PATH)"
  SOURCE=$(cd "$SOURCE" && pwd)
  PLUGIN=memhub-staging
  CLAUDE_SRC="$SOURCE";  CLAUDE_MKT=memhub-internal
  CODEX_SRC="$SOURCE";   CODEX_MKT=memhub-internal
  CURSOR_SRC="$SOURCE"
  BACKEND=staging
else
  PLUGIN=memhub
  CLAUDE_SRC=XTraceAI/agent-plugins;                 CLAUDE_MKT=memhub
  CODEX_SRC=XTraceAI/agent-plugins;                  CODEX_MKT=xtrace-plugins
  CURSOR_SRC=https://github.com/XTraceAI/agent-plugins
  BACKEND=production
fi

command -v python3 >/dev/null 2>&1 \
  || fail "python3 is required: MemHub's hooks run on it"

wanted() {
  [ -z "$AGENTS" ] && return 0
  case ",$AGENTS," in *",$1,"*) return 0 ;; esac
  return 1
}

CLAUDE_BIN=""; CODEX_BIN=""; CURSOR_BIN=""
wanted claude && CLAUDE_BIN=$(command -v claude 2>/dev/null || true)
wanted codex && CODEX_BIN=$(command -v codex 2>/dev/null || true)
wanted cursor && CURSOR_BIN=$(command -v cursor-agent 2>/dev/null || true)
[ -n "$CLAUDE_BIN$CODEX_BIN$CURSOR_BIN" ] \
  || fail "found none of claude, codex, cursor-agent on PATH"

say "MemHub installer: $PLUGIN ($BACKEND)"
[ "$MODE" = staging ] && say "source: $SOURCE"

# The plugin root whose scripts/ sign the machine in. Any installed copy of
# the target plugin works: they all target the same backend.
PLUGIN_ROOT=""
[ "$MODE" = staging ] && PLUGIN_ROOT="$SOURCE/plugins/$PLUGIN"
LOGIN_HOST=""
NEXT=""
note() { NEXT="$NEXT
  - $*"; }

if [ -n "$CLAUDE_BIN" ]; then
  step "Claude Code"
  # Two commands, not `install --marketplace` (that flag needs Claude Code
  # 2.1.292+). Both are no-ops when already done.
  "$CLAUDE_BIN" plugin marketplace add "$CLAUDE_SRC"
  "$CLAUDE_BIN" plugin install "$PLUGIN@$CLAUDE_MKT" --scope user
  # A re-run must reach the current release: neither command above refreshes
  # an already-added marketplace or an installed plugin. --scope user, or
  # Claude updates a project/local copy instead of the one installed here.
  # A failed refresh (e.g. a network blip) leaves a working install: warn, and
  # still sign in.
  { "$CLAUDE_BIN" plugin marketplace update "$CLAUDE_MKT" \
      && "$CLAUDE_BIN" plugin update "$PLUGIN@$CLAUDE_MKT" --scope user; } \
    || say "warning: could not refresh $PLUGIN in Claude Code; re-run this installer, or: claude plugin update $PLUGIN@$CLAUDE_MKT --scope user" >&2
  "$CLAUDE_BIN" plugin enable "$PLUGIN@$CLAUDE_MKT" >/dev/null 2>&1 || true
  # Every MemHub copy registers the same hooks and an MCP server named
  # `memhub`; two enabled means every session is captured twice and every rule
  # fires twice. Disable (not uninstall) the others, so this is reversible.
  "$CLAUDE_BIN" plugin list --json | python3 -c '
import json, sys
target = sys.argv[1]
for p in json.load(sys.stdin):
    name = p["id"].split("@", 1)[0]
    if name in ("memhub", "memhub-staging") and p["id"] != target and p.get("enabled"):
        print(p["id"])
' "$PLUGIN@$CLAUDE_MKT" | while read -r dup; do
    if "$CLAUDE_BIN" plugin disable "$dup" >/dev/null 2>&1 \
       || "$CLAUDE_BIN" plugin disable "$dup" --scope user >/dev/null 2>&1; then
      say "disabled duplicate MemHub plugin: $dup (re-enable: claude plugin enable $dup)"
    else
      say "warning: could not disable duplicate $dup; disable it in /plugin" >&2
    fi
  done
  if [ -z "$PLUGIN_ROOT" ]; then
    PLUGIN_ROOT=$("$CLAUDE_BIN" plugin list --json | python3 -c '
import json, sys
# One row per installed scope; sign in from the user-scoped copy only.
for p in json.load(sys.stdin):
    if p["id"] == sys.argv[1] and p.get("scope") == "user":
        print(p["installPath"])
        break
' "$PLUGIN@$CLAUDE_MKT")
  fi
  LOGIN_HOST=${LOGIN_HOST:-claude-code}
  note "Claude Code: restart it (or /reload-plugins), then run /$PLUGIN:onboard in a repo"
fi

if [ -n "$CODEX_BIN" ]; then
  step "Codex"
  "$CODEX_BIN" plugin marketplace add "$CODEX_SRC"
  # `add` returns early for a known source without fetching; only `upgrade`
  # refreshes a Git snapshot. A local-directory source is always current.
  [ "$MODE" = staging ] || "$CODEX_BIN" plugin marketplace upgrade "$CODEX_MKT" \
    || say "warning: could not refresh the Codex marketplace; re-run this installer, or: codex plugin marketplace upgrade $CODEX_MKT" >&2
  CODEX_ROOT=$("$CODEX_BIN" plugin add "$PLUGIN@$CODEX_MKT" | sed -n 's/^Installed plugin root: //p')
  [ -n "$CODEX_ROOT" ] || fail "codex did not report where it installed $PLUGIN"
  say "installed: $CODEX_ROOT"
  "$CODEX_BIN" plugin list 2>/dev/null \
    | awk -v t="$PLUGIN@$CODEX_MKT" '$1 ~ /^memhub(-staging)?@/ && $1 != t && /enabled/ {print $1}' \
    | while read -r dup; do
        say "warning: $dup is also enabled in Codex; remove it with: codex plugin remove $dup" >&2
      done
  # Codex never dispatches a plugin manifest's own hooks; capture runs only
  # through this user-level bridge, and only after the person trusts it.
  python3 "$CODEX_ROOT/scripts/setup_codex_hooks.py" install
  [ -n "$PLUGIN_ROOT" ] || PLUGIN_ROOT="$CODEX_ROOT"
  LOGIN_HOST=${LOGIN_HOST:-codex}
  note "Codex: restart it, open /hooks and trust only the MemHub handlers — capture stays OFF until you do"
fi

if [ -n "$CURSOR_BIN" ]; then
  step "Cursor"
  "$CURSOR_BIN" plugin marketplace add "$CURSOR_SRC"
  # As with Codex, `add` does not re-index a known marketplace; `update` does.
  # Not fatal: the install itself is the manual Add step below.
  [ "$MODE" = staging ] || "$CURSOR_BIN" plugin marketplace update xtrace-plugins \
    || say "warning: could not refresh the Cursor marketplace; run: cursor-agent plugin marketplace update xtrace-plugins" >&2
  LOGIN_HOST=${LOGIN_HOST:-cursor}
  note "Cursor: open Customize, find MemHub and select Add (Cursor has no command for this step)"
fi

if [ "$LOGIN" = 1 ]; then
  step "Sign in (once for this machine)"
  if [ -z "$PLUGIN_ROOT" ]; then
    say "skipped: no installed copy to sign in from yet; after adding it in Cursor, ask the agent to log in to MemHub"
  elif python3 "$PLUGIN_ROOT/scripts/login.py" --status; then
    say "already signed in"
  else
    python3 "$PLUGIN_ROOT/scripts/login.py" --host "$LOGIN_HOST" \
      || fail "sign-in did not complete; run this installer again to retry"
  fi
fi

printf '\nDone. Left for you:%s\n' "$NEXT"
