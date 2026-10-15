---
description: Use when the user wants to choose which repositories MemHub is active in, or asks why MemHub is quiet in one (e.g. "enable memhub for this repo", "turn memhub off in client repos", "which repos is memhub on for", "memhub says it is inactive here", "/memhub:repos add ."). Lists, adds or removes repositories on this machine's allowlist, or clears it so MemHub is active everywhere. Outside the listed repositories MemHub does nothing — no team rules, no session capture, no companion.
argument-hint: "[list | add [<org/name>|.] | remove [<org/name>|.] | all | status]"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" *)'
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

MemHub can be limited to the repositories a person chooses. The list lives on
this machine in `~/.config/memhub-plugin/repos.json`:

- **No file:** MemHub is active in every repository (how it behaves unless
  someone made a list).
- **A file:** MemHub is active only in the repositories it lists. Everywhere
  else the plugin is dormant: no team rules fire, no session or document is
  captured, and the companion stays hidden. A session started in such a
  repository is told so once, at its start. An **empty** list means dormant
  everywhere — the installer writes one when it cannot ask.

A repository is named as its `origin` remote spells it, `<org>/<name>`
(`XTraceAI/MemHub-Backend`); a checkout with no remote is named by its
directory. `.` means the current checkout, and the script derives the name, so
nobody has to spell it — from a worktree too, which carries its repository's
name, not its branch directory's.

The file may also hold **declined** repositories: in Claude Code, a session
started in a checkout the list leaves out is asked once, on the status strip
above the prompt (`MemHub · enable in <org/repo>?` — `1 Yes / 2 No / 3 Later`).
No records the repository as declined: as dormant as any unlisted one, and never
asked about again. `list` and `status` show declined repositories; `add` lists
one and clears its decline; `remove` of a declined repository clears the decline
only, so the strip asks again there.

Arguments: `$ARGUMENTS`

Run exactly one command and report what it prints:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" list                 # default: what is listed, and this checkout's state
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" add .                # list the current repository
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" add XTraceAI/xmem    # list one by name
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" remove .             # take the current repository off the list
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" all                  # delete the list: active everywhere
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/repos.py" status               # one line for this checkout
```

Three things to say when they apply:

- The **first** `add` creates the list, which turns MemHub off in every other
  repository on this machine. The script says so; repeat it, and name
  `/memhub:repos all` as the way back.
- A change reaches the Python hooks at the next tool call. The Claude Code mod
  (the in-process rules and the companion) reads the list at session start, so
  after `add` or `remove` the person restarts the session, or `/reload-plugins`.
- A repository shown as **declined** is one the person answered No to on the
  strip. If they now want MemHub there, `add .` is the way; say that `remove`
  would only make the strip ask again.

Do not edit `repos.json` by hand and do not suggest disabling the plugin for
one repository: disabling it stops capture and rules in every repository,
which is what this list exists to avoid.
