---
name: setup
description: Use when the user asks to set up, repair, or verify the installed MemHub host integration, especially Codex automatic capture, PreToolUse directive recall, hook installation, or capture health. Installs the Codex user-hooks compatibility bridge idempotently while preserving unrelated hooks, then checks plugin authentication and reports the one required trust step.
allowed-tools: Bash
---

Set up the installed MemHub plugin's host integration. This skill configures
the plugin install; `/memhub:onboard` separately connects the current repo to
its agent brain.

Resolve `ROOT` as the ancestor of this skill file containing `scripts/`. Codex
sets `PLUGIN_ROOT`; Claude Code sets `CLAUDE_PLUGIN_ROOT`. Prefer those values,
then derive it from the skill path if needed.

Arguments: `$ARGUMENTS`

## Codex

Codex hook support depends on the host version. Current desktop builds can load
bundled plugin hooks; older CLI releases need the user-level compatibility bridge.
Both builds have a Codex manifest pointing at `hooks/codex-hooks.json`. Claude
compatibility handlers must skip Codex payloads. When the user bridge is installed,
bundled Codex handlers defer to it for matching events; the user handlers still
need review and trust. Do not install both production and staging plugins.

A projectless task may receive shell hook payloads containing the session directory
instead of the command's explicit `workdir`. Use a shell-quoted absolute
`cd <repo> && ...` prefix for repository shell calls, or start the task in the
repository. The plugin cannot reconstruct an omitted working directory. This
workaround enables command rules; session-start rules still require starting in
the repository. Do not claim full parity from a successful directive recall.

Install the bridge:

```bash
python3 "$ROOT/scripts/setup_codex_hooks.py" install
```

The installer is idempotent. It merges four MemHub handlers into
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), preserves unrelated
events and handlers, backs up a changed existing file, and installs a stable
trampoline that follows plugin version upgrades. It enables:

- `SessionStart`: rulebook posture rules and any plugin-upgrade notice, the
  repo brain brief, and a capture-health warning — the same three scripts
  Claude Code runs at session start;
- `PreToolUse`: the rulebook hook (which can deny the call) and situated
  directive recall before mutating shell and edit calls;
- `PostToolUse`: the rulebook hook, reactive recall on failures,
  artifact-link reminders, and PR-link recording after GitHub-touching shell
  calls (GitHub MCP calls only through the bundled plugin hooks — the bridge's
  matcher stays narrow so upgrading it needs no re-trust);
- `PostToolUse` + `Stop`: incremental session capture.

After installation, report installation and trust as separate states. Codex
deliberately does not let a plugin approve command hooks, and the setup script
cannot inspect or change that approval. Do not claim capture or directive
recall is active until the user confirms the review is done.

Give these precise review instructions:

1. Restart Codex, then choose **Review hooks** at startup or open `/hooks`.
2. Review the single MemHub handler under each of `SessionStart`,
   `PreToolUse`, `PostToolUse`, and `Stop`.
3. Trust only handlers whose source is `User config - ~/.codex/hooks.json` and
   whose command contains `memhub_hook_bridge.py`.

If Codex reports more than four handlers awaiting review, the extras are not
from this installer. A user who trusted the earlier three-handler bridge is
asked once more after upgrading: the new `SessionStart` handler is the only
addition. Warn the user not to choose **Trust all** in that case.
Never describe this manual approval as automatable by MemHub.

For `$ARGUMENTS == --status`, run this and make no changes:

```bash
python3 "$ROOT/scripts/setup_codex_hooks.py" status
```

For `$ARGUMENTS == --remove`, run this and stop:

```bash
python3 "$ROOT/scripts/setup_codex_hooks.py" remove
```

## Authentication and health

After an install, check the hook credential without opening a browser:

```bash
CLAUDE_PLUGIN_ROOT="$ROOT" uv run --with 'mcp<2' python "$ROOT/scripts/login.py" --status
```

(`login.py` imports the `mcp` SDK, so a bare `python3` prints FAILED even when
the credential is fine.)

If it reports `NOT LOGGED IN`, explain that the hook credential is separate
from the MCP connector login and run `/memhub:login` before calling setup
complete. Do not silently start a browser login.

Then run the local health check. On Codex:

```bash
echo '{}' | python3 "$ROOT/scripts/capture_health.py" --host codex --plugin-root "$ROOT"
```

On Claude Code and Cursor:

```bash
echo '{}' | CLAUDE_PLUGIN_ROOT="$ROOT" python3 "$ROOT/scripts/capture_health.py"
```

(It reads a hook payload from stdin; without the pipe it blocks on the tty.
Without `--host codex` it judges Claude Code's capture state, which on Codex
reports the wrong host's health — the same flags the bridge passes.)

No output is healthy. Relay any warning exactly enough that the user knows the
remedy. Finish with a compact status: hook bridge installed/current, hook trust
unverified or user-confirmed, plugin credential healthy or missing, and whether
the current repo still needs MemHub onboarding.

## Other hosts

Claude Code and Cursor load their bundled MemHub hooks natively, so never write
Codex's bridge into their config. On those hosts, only run the authentication
and health checks above. Cursor Teams/Enterprise policy can block unofficial
marketplaces before this skill is available; that is an admin installation
policy, not something this plugin should attempt to bypass.
