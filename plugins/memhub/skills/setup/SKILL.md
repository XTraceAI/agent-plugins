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

Codex 0.148 and 0.149 support user-level hooks but do not load hooks bundled by
an installed plugin. Install MemHub's compatibility bridge:

```bash
python3 "$ROOT/scripts/setup_codex_hooks.py" install
```

The installer is idempotent. It merges three MemHub handlers into
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), preserves unrelated
events and handlers, backs up a changed existing file, and installs a stable
trampoline that follows plugin version upgrades. It enables:

- `PreToolUse`: situated directive recall before mutating shell and edit calls;
- `PostToolUse`: reactive recall on failures and artifact-link reminders;
- `PostToolUse` + `Stop`: incremental session capture.

After installation, report installation and trust as separate states. Codex
deliberately does not let a plugin approve command hooks, and the setup script
cannot inspect or change that approval. Do not claim capture or directive
recall is active until the user confirms the review is done.

Give these precise review instructions:

1. Restart Codex, then choose **Review hooks** at startup or open `/hooks`.
2. Review the single MemHub handler under each of `PreToolUse`, `PostToolUse`,
   and `Stop`.
3. Trust only handlers whose source is `User config - ~/.codex/hooks.json` and
   whose command contains `memhub_hook_bridge.py`.

If Codex reports more than three handlers awaiting review, the extras are not
from this installer. Warn the user not to choose **Trust all** in that case.
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

Then run the local health check:

```bash
echo '{}' | CLAUDE_PLUGIN_ROOT="$ROOT" python3 "$ROOT/scripts/capture_health.py"
```

(It reads a hook payload from stdin; without the pipe it blocks on the tty.)

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
