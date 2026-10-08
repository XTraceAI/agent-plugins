---
description: Use when someone wants MemHub working where they are — a first setup, or a check or repair later (e.g. "onboard", "get started", "set up memhub", "set up my brain", "connect this repo to MemHub", "capture stopped working", "my sessions aren't being saved", "verify capture", "check memhub is working", "remove memhub's hooks"). One command signs in, sets up this machine's hooks, checks capture health, creates or reuses the repo's brain with its docs, offers up to five universal safety rules with one answer, and on Codex asks for the one hook approval only the person can give. Every step is skipped when already done; --status only reports, --remove takes the machine hooks out.
argument-hint: "[--status | --remove]"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/login.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_claude_fork.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_codex_hooks.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture_health.py"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture_health.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" *), Bash(git rev-parse --show-toplevel), Bash(git ls-files), Read, Edit(//tmp/memhub-repo-overview.md), AskUserQuestion'
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code
exports it; Codex exports `PLUGIN_ROOT` instead. If it is unset, set it first —
from `$PLUGIN_ROOT` when that is set, otherwise (e.g. on Cursor) to this
plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Make MemHub work for this person, here. Run the steps in order; each is safe
to repeat and skipped when already done. Give each one status line, relaying
what its script printed, never something stronger. Do not call the memhub MCP
tools in this skill: on a first run they are not connected yet, and nothing
here needs them. `<host>` below is the host you are actually
running in: `claude-code`, `codex` or `cursor`.

Arguments: `$ARGUMENTS`

- `--status`: run only each step's read-only check (named in the step), then
  the summary. Change nothing, sign nobody in, ask no question.
- `--remove`: run the one line for this host, relay it, and stop. It does not
  sign out or touch any brain; Cursor has nothing to remove.
  - Codex: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_codex_hooks.py" remove`
  - Claude Code: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_claude_fork.py" remove`

## 1. Sign in

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/login.py" --status --host <host>
```

A `status` line reading `OK` → "✓ Signed in", go on. Otherwise (and not
`--status`) run the same command without `--status`. Before it finishes, tell
the person: "If a sign-in code appears, open the link, check the code matches,
and approve." If sign-in fails, give its one-line reason and stop; running
this command again picks up where it left off.

## 2. This machine's hooks

**Codex.** MemHub's handlers have to be in the user hook file, which a plugin
cannot write for itself:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_codex_hooks.py" install
```

It merges four MemHub handlers (session start, before and after a tool call,
turn end) into `~/.codex/hooks.json` (`$CODEX_HOME` when set), keeps every
unrelated hook, backs up a file it changes, and changes nothing when already
current. Say `installed` or `already current`. Read-only check:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_codex_hooks.py" status`. Install one MemHub plugin, production or
staging, never both.

**Claude Code.** Only matters when MemHub's rule drafting is on (the
default; `MEMHUB_HARNESS_EXTRACT=0` turns it off): a draft runs in a background fork of the
session, which the terminal has by default but the desktop app, the Agent SDK
and `claude -p` lack unless `CLAUDE_CODE_FORK_SUBAGENT=1` is set.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_claude_fork.py" install
```

It adds only `"CLAUDE_CODE_FORK_SUBAGENT": "1"` under `env` in
`~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR/settings.json` when set),
backs the file up first, and writes nothing when drafting is off or the key is
already there. Relay:

- `set`: tell the person to **restart Claude Code**; the setting is read when
  a session starts.
- `already set` / `not needed`: nothing to do, one line.
- `LEFT OFF`: they set it off themselves. Leave it; say rule drafts will not
  run outside the terminal until it is `1`.
- `ERROR`: the file could not be read and was left untouched. Give the one
  line to add under `env` by hand.

If a permission prompt or auto mode refuses the command, do not retry it or
write the file another way: give the same one line and report the fork agent
as not enabled. Read-only check:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_claude_fork.py" status`.

**Cursor.** Nothing to install; Cursor loads the plugin's hooks itself.

## 3. Health

```
echo '{}' | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture_health.py"
```

On Codex append `--host codex --plugin-root "${CLAUDE_PLUGIN_ROOT}"`, or it
judges the wrong host. Keep the `echo '{}' |`: without it the script waits on
the keyboard. No output
is healthy ("✓ Capture healthy"); otherwise relay the warning closely enough
that the person knows the remedy. This step is already read-only.

## 4. This repo

Run `git rev-parse --show-toplevel`. Outside a git repo say "This repo:
skipped (not a git repository)"; with `--status` say "This repo: not checked
by --status". Either way go to §5.

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard.py" run --host <host>
```

It prints `✓` lines as it goes; show them as they are. A `✗` line ends this
step: show it and go to §5.

**Overview.** Run `git ls-files` on its own and read the README (if any) with
the Read tool. Write an overview of at most 250 words: what the repo is, its
stack, its layout (top-level folders and what each holds), and how to run and
test it. Write only what you read. Save it in two steps, each exactly as shown
and on one line (a combined or line-broken command does not match this skill's
pre-approved calls, and a headless run has nobody to approve it):

1. Write the overview to `/tmp/memhub-repo-overview.md` with the Write tool.
2. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" --file /tmp/memhub-repo-overview.md --name "Repo overview" --type document --topic docs --tags overview`

If it fails, go on: the docs are already saved.

**The one question.** The script's last line starts `RULES:`. `RULES: none`
→ done. Otherwise ask once, with AskUserQuestion where you have it (otherwise
as a plain question), listing the rules the script printed:

- question: "Turn on these starter rules? They stop your agent from running these commands, for you only."
- option 1: "Turn them on (Recommended)"
- option 2: "Not now" — the rules wait in Studio, off.

Then run exactly one:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard.py" rules --activate
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard.py" rules --propose
```

## 5. Codex only: approve the hooks

Codex runs a user hook only once the person trusts it; no plugin can do that
for them. Run §2's read-only Codex check; `trust: confirmed …` means
MemHub's hooks have run since the install: say so and skip the rest.

Otherwise (and not `--status`) ask as a short plain-text question, since Codex
has no question UI:

"One step only you can do: restart Codex, choose **Review hooks** (or open
`/hooks`), and trust only the MemHub handlers whose source is
`~/.codex/hooks.json` and whose command contains `memhub_hook_bridge.py`.
Never choose **Trust all**. Done, or later?"

- done: MemHub confirms it the first time its hooks run (`--status` shows it).
- later: capture and team rules stay off on Codex until then; running this
  command again re-checks.

Handlers beyond MemHub's four are not from this plugin. Never call capture or
team rules active on Codex before trust is confirmed. In a Codex task with no
project, hooks may not see a command's working directory: run repo commands
as `cd <absolute repo path> && …`, or start the task in the repo.

## 6. Summary

One line per step: Signed in · Hooks · Health · This repo · Codex approval
(Codex only). When every step was already done, say plainly that nothing
changed. After a §4 that finished, add:

- What the brain holds now (the docs, plus the overview), and the rules that
  are on or waiting in Studio (the link the script printed).
- Claude Code only: "The animal above your prompt is the MemHub companion. It
  speaks up when a team rule fires or blocks a command, and when MemHub
  proposes a new rule for you to accept."
- "MemHub's tools are ready from your next session. Sessions from now on are
  saved to your memory."
- Next: share the brain with a teammate from Studio, and run
  `/memhub:start-rulebook` in a week or so to turn your own habits into rules.

Write each command the way this plugin names it: `/memhub-staging:…` when the
installed plugin is memhub-staging. Never mention `/mcp`, access keys, tokens,
or log files unless the person asks.
