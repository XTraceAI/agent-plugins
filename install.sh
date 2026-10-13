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
#   --no-auto-update leave Claude Code's marketplace auto-update as it is
#                    (default: turned on, so the plugin follows each release)
#   --no-fork-agent  leave Claude Code's fork agent setting as it is (default:
#                    CLAUDE_CODE_FORK_SUBAGENT=1 in settings.json, so rule
#                    drafts run in the desktop app too, not only the terminal)
#
# Safe to run again: every step checks what is already there. One sign-in
# covers every agent on the machine, because the capture hooks of all three
# read the same credential under ~/.config/memhub-plugin/.
set -eu

MODE=prod
SOURCE="${MEMHUB_PLUGINS_SOURCE:-$HOME/xtrace/agent-plugins-internal}"
AGENTS=""
LOGIN=1
AUTO_UPDATE=1
FORK_AGENT=1

while [ $# -gt 0 ]; do
  case "$1" in
    --staging) MODE=staging ;;
    --source) [ $# -ge 2 ] || { echo "--source needs a path" >&2; exit 2; }; SOURCE="$2"; shift ;;
    --agents) [ $# -ge 2 ] || { echo "--agents needs a list" >&2; exit 2; }; AGENTS="$2"; shift ;;
    --no-login) LOGIN=0 ;;
    --no-auto-update) AUTO_UPDATE=0 ;;
    --no-fork-agent) FORK_AGENT=0 ;;
    -h|--help) sed -n '2,25p' "$0" 2>/dev/null || true; exit 0 ;;
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
  CURSOR_SRC="$SOURCE";  CURSOR_MKT=memhub-internal
  BACKEND=staging
else
  PLUGIN=memhub
  CLAUDE_SRC=XTraceAI/agent-plugins;                 CLAUDE_MKT=memhub
  CODEX_SRC=XTraceAI/agent-plugins;                  CODEX_MKT=xtrace-plugins
  CURSOR_SRC=https://github.com/XTraceAI/agent-plugins; CURSOR_MKT=xtrace-plugins
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
  CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  # A machine that added MemHub's marketplace from its old home
  # (XTraceAI/memhub-claude-plugin) declares this same name with another
  # source, and `marketplace add` refuses to change it ("its source doesn't
  # match its extraKnownMarketplaces entry"). Re-point the entry — the fix that
  # error names — and the add below re-clones from the catalog, keeping the
  # installed plugin and its options (a remove would delete both).
  python3 -c '
import json, os, stat, sys, tempfile
from pathlib import Path
mkt, src, mode, home = sys.argv[1:5]
path = Path(home) / "settings.json"
try:
    settings = json.loads(path.read_text())
except (OSError, ValueError):
    sys.exit(0)
entry = (settings.get("extraKnownMarketplaces") or {}).get(mkt)
want = {"source": "github", "repo": src} if mode == "prod" else {"source": "directory", "path": src}
if not isinstance(entry, dict) or entry.get("source") == want:
    sys.exit(0)
print("re-pointing marketplace " + mkt + ": " + json.dumps(entry.get("source")) + " -> " + json.dumps(want))
entry["source"] = want
# Written beside the real file and swapped in by rename, so a settings.json
# that is a symlink (dotfile managers) stays one. The temp file is private
# from its first byte, then takes the mode the file already has (0600 on
# many machines) — the rename carries that mode onto settings.json.
real = path.resolve()
fd, tmp = tempfile.mkstemp(prefix=real.name + ".memhub-", dir=str(real.parent))  # unique, 0600
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(settings, indent=2) + "\n")
os.chmod(tmp, stat.S_IMODE(real.stat().st_mode))
os.replace(tmp, real)
' "$CLAUDE_MKT" "$CLAUDE_SRC" "$MODE" "$CLAUDE_HOME"
  # `marketplace add` rewrites the marketplace's settings entry on a re-run and
  # drops its autoUpdate. Remember whether it was on, so --no-auto-update keeps
  # it that way ("as it is") instead of silently turning it off.
  AUTO_UPDATE_WAS_ON=$(python3 -c '
import json, sys
from pathlib import Path
mkt, home = sys.argv[1:3]
try:
    entry = (json.loads((Path(home) / "settings.json").read_text()).get("extraKnownMarketplaces") or {}).get(mkt) or {}
except (OSError, ValueError, AttributeError):
    entry = {}
print("1" if isinstance(entry, dict) and entry.get("autoUpdate") is True else "0")
' "$CLAUDE_MKT" "$CLAUDE_HOME" 2>/dev/null || echo 0)
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
  if [ "$AUTO_UPDATE" = 1 ] || [ "$AUTO_UPDATE_WAS_ON" = 1 ]; then
    # Claude Code refreshes a marketplace and its installed plugins at startup
    # when the marketplace's entry in user settings says so — the toggle
    # /plugin offers, declared here so nobody has to find it. The CLI has no
    # flag for it. known_marketplaces.json is Claude's materialized copy of the
    # same entry; set it there too so it holds before the next settings sync.
    python3 -c '
import json, os, stat, sys, tempfile
from pathlib import Path
mkt, src, mode, home, asked = sys.argv[1:6]
want = {"source": "github", "repo": src} if mode == "prod" else {"source": "directory", "path": src}

def flip(path, entry_of):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return False
    entry = entry_of(data)
    if entry is None or entry.get("autoUpdate") is True:
        return False
    entry["autoUpdate"] = True
    # As above: beside the real file (a symlink stays one), private from the
    # first byte, then the mode the file already has — or 0600 for a file this
    # step creates, since settings can hold secrets.
    real = path.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(real.stat().st_mode) if real.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=real.name + ".memhub-", dir=str(real.parent))  # unique, 0600
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, real)
    return True

declared = flip(Path(home) / "settings.json",
                lambda d: d.setdefault("extraKnownMarketplaces", {}).setdefault(mkt, {"source": want}))
flip(Path(home) / "plugins" / "known_marketplaces.json",
     lambda d: d.get(mkt) if isinstance(d.get(mkt), dict) else None)
if asked == "1":
    print("auto-update for " + mkt + ": " + ("turned on" if declared else "already on"))
else:
    print("auto-update for " + mkt + ": kept on (it was on before this run; --no-auto-update leaves it as it is)")
' "$CLAUDE_MKT" "$CLAUDE_SRC" "$MODE" "$CLAUDE_HOME" "$AUTO_UPDATE" \
      || say "warning: could not turn on auto-update for $CLAUDE_MKT; toggle it in /plugin" >&2
  fi
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
  # Rule drafts run in a background fork of the session. A terminal session
  # has the fork agent by default; the desktop app, the Agent SDK and
  # `claude -p` need CLAUDE_CODE_FORK_SUBAGENT=1 in settings.json, or the draft
  # fails after the person was told it started. /memhub:onboard sets it too;
  # doing it here covers a desktop-only person from their first session. The
  # script backs settings.json up, keeps a key set off by hand (exit 1, says
  # so), and writes nothing when rule drafting is off.
  if [ "$FORK_AGENT" = 1 ] && [ -f "$PLUGIN_ROOT/scripts/setup_claude_fork.py" ]; then
    rc=0; python3 "$PLUGIN_ROOT/scripts/setup_claude_fork.py" install --claude-home "$CLAUDE_HOME" || rc=$?
    [ "$rc" = 2 ] && say "warning: could not set Claude Code's fork agent; add \"CLAUDE_CODE_FORK_SUBAGENT\": \"1\" under env in $CLAUDE_HOME/settings.json" >&2
  fi
  LOGIN_HOST=${LOGIN_HOST:-claude-code}
  note "Claude Code: restart it (or /reload-plugins), then run /$PLUGIN:onboard in a repo"
fi

if [ -n "$CODEX_BIN" ]; then
  step "Codex"
  "$CODEX_BIN" plugin marketplace add "$CODEX_SRC"
  # `add` returns early for a known source without fetching; only `upgrade`
  # refreshes a Git snapshot. A local-directory source is always current.
  if [ "$MODE" != staging ] && ! "$CODEX_BIN" plugin marketplace upgrade "$CODEX_MKT"; then
    # `plugin add` would reinstall from the stale snapshot and can downgrade a
    # newer installed MemHub; leave whatever Codex has untouched instead.
    say "warning: could not refresh the Codex marketplace, so Codex was left as it is; re-run this installer" >&2
    note "Codex: not installed or updated (marketplace refresh failed) — re-run this installer"
  else
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
fi

if [ -n "$CURSOR_BIN" ]; then
  step "Cursor"
  "$CURSOR_BIN" plugin marketplace add "$CURSOR_SRC"
  # As with Codex, `add` does not re-index a known marketplace; `update` does.
  # Not fatal: the install itself is the manual Add step below.
  "$CURSOR_BIN" plugin marketplace update "$CURSOR_MKT" \
    || say "warning: could not refresh the Cursor marketplace; run: cursor-agent plugin marketplace update $CURSOR_MKT" >&2
  LOGIN_HOST=${LOGIN_HOST:-cursor}
  # Cursor has no command to install or update a plugin, and re-indexing the
  # marketplace does not touch an installed copy's files.
  note "Cursor: open Customize, find MemHub and select Add — or, if it is already installed, Update (or remove and Add again) to get the new version. Cursor has no command for this step"
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
