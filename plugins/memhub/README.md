# MemHub

MemHub gives coding agents shared team memory. This plugin connects Claude Code
to MemHub by XTrace. It provides MCP tools for searching and saving team
knowledge, and it captures your sessions into your personal MemHub memory
automatically. It also enforces your team's Rulebook rules on tool calls, and
adds skills for artifacts, specs, handoffs, PR linking and PR babysitting.

The plugin uploads data to MemHub in the background. The section
[What it runs, sends, and fetches](#what-it-runs-sends-and-fetches) lists
every hook, every network destination and every local file. Please read it
before you install.

## Install

XTrace publishes this plugin from two sources. Install it from one of them:

- **The Claude plugin directory.** Install `memhub` on claude.ai under
  **Customize > Plugins**, or in Claude Code with
  `/plugin install memhub@claude-plugins-official`. The directory's copy is
  built from `XTraceAI/xtrace-claude-plugin`, and Claude Code updates it once
  each new version is published to the directory. New versions can reach the
  directory a little later than the marketplace below.
- **The XTrace marketplace.** Add it by hand:

  ```text
  /plugin marketplace add XTraceAI/agent-plugins
  /plugin install memhub@memhub
  /reload-plugins
  ```

**Install it from one source only.** Both copies register the same hooks and
an MCP server named `memhub`, so with both enabled every session is captured
twice and every team rule fires twice. To switch, uninstall the copy you have
(`/plugin uninstall memhub@memhub` or
`/plugin uninstall memhub@claude-plugins-official`), restart Claude Code, then
install the other.

Then, from the repository you want to connect:

1. Run `/memhub:onboard`. It signs the plugin in (the same sign-in as
   `/memhub:login`, which background capture and the hooks need), sets up this
   machine's hooks, and creates or selects the repository's agent brain.

Requirements: `python3` 3.9 or newer and `git`. The hooks and the skills'
scripts use only the Python standard library, so nothing else is installed:
skills run them as `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/<script>.py"` or
`python3 "${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/<script>.py"`.

## Authentication

There are two credentials, and setting up one does not set up the other.

- **MCP tools (the `memhub` server).** The server is
  `https://api.memhub.xtrace.ai/mcp-server/mcp`. Its OAuth login uses the
  Auth0 tenant `memhub-prod.us.auth0.com`, with a browser redirect to
  `localhost:8765`. On Claude Code, the server's `headersHelper`
  (`scripts/mcp_headers.py`) sends the credential from `/memhub:login` (the
  stored access key, else the plugin's cached OAuth token) as the
  `Authorization` header. Before it does, it checks the credential against
  the server with one request per connect. So after `/memhub:login` the tools
  work without a separate `/mcp` login. If there is no credential, or the
  server refuses it, the helper prints no header, and **Authenticate** under
  `/mcp` applies.
- **Hooks (`/memhub:login`).** This opens the browser once for the same Auth0
  login. It then mints a personal access key (`mhk_…`). The key is scoped to
  `memory:read` and `memory:write`, expires after 90 days, and is labelled
  `claude-code-<hostname>`. It is stored at
  `~/.config/memhub-plugin/pak-<api-host>.json`. Hooks are background
  processes that cannot open a browser, so they rely on this key. When the
  login succeeds, the local callback page sends your browser on to MemHub's
  setup guide under `https://mem.xtrace.ai/plugin`.

**Optional `harnessDrafting` setting.** A boolean `userConfig` option, on by
default. Set it to false and the plugin drafts no team rule from your
corrections: the Stop hook judges no turn and asks for no drafting fork,
whatever `MEMHUB_HARNESS_EXTRACT` says. See
[Harness-tied rule drafting](#harness-tied-rule-drafting-on-by-default).

**Optional `memhub_token` setting.** The plugin declares a `userConfig`
option, `memhub_token`. It is a masked field, it is not required, and it has
no default. Claude Code keeps its value in the system credential store. Leave
it empty to use the key from `/memhub:login`. Hooks receive it as
`CLAUDE_PLUGIN_OPTION_MEMHUB_TOKEN`. Set it with
`/plugin configure memhub@<marketplace>`, or
`claude plugin install memhub@<marketplace> --config memhub_token=mhk_…`.

Hooks use the first credential they find, in this order:

1. the `memhub_token` option;
2. `$MEMHUB_TOKEN`;
3. the stored access key;
4. the cached OAuth token (`~/.config/memhub-plugin/tokens-<api-host>.json`),
   refreshed when stale.

Neither the option nor `$MEMHUB_TOKEN` reaches the MCP tools, because Claude
Code gives the `headersHelper` neither of them. Skill scripts that run through
the Bash tool don't see the option either.

## Skills

Each skill runs as `/memhub:<name>`, or when you ask for it in plain words.

| Skill | What it does, reads and sends |
| --- | --- |
| `login` | Signs the plugin in and stores its access key (see Authentication). |
| `onboard` | Signs the plugin in when it is not (as `login`), checks capture health, then creates or reuses the repository's agent brain and records it in `rooms.json`. Lists the repository's tracked Markdown files (`git ls-files`) and uploads the ones that look important to that brain without asking. It also saves a short repo overview it writes. With `MEMHUB_HARNESS_EXTRACT=1` on Claude Code it adds `CLAUDE_CODE_FORK_SUBAGENT=1` under `env` in `~/.claude/settings.json`. On Codex it installs the Codex hooks bridge (see [Codex and Cursor](#codex-and-cursor)) and asks you to approve it in Codex. `--status` only reports; `--remove` takes those machine settings out. |
| `save-artifact` | Uploads a file you name as an artifact, to the repository's brain when there is one, else to your personal memory. |
| `import-session` | Reads a past Claude Code, Codex or Cursor transcript from this machine and uploads it to your personal memory, in chunks when it is large. |
| `search-memory` | Read-only search of the brain and your memory through the MCP tools. |
| `handoff-session` | Writes a handoff brief (goal, state, decisions, next steps, gotchas) into the standing handoff brain for exactly you and the teammates you name, so they can search it from their own agent; creates and shares that brain when there is none, or a one-off brain with `--new`. The session itself is not shared. |
| `link-pr` | Links or unlinks a session and a pull request in MemHub. Asks the agent to run `gh pr view`. |
| `find-contributing-sessions` | Reads this machine's Claude Code, Codex and Cursor session history to find the sessions behind your pull requests. Its script runs `gh pr view` and `gh api` for each PR's files. It prints paths, branches and commit ids, never transcript content, and links only the sessions you approve. |
| `pr-babysit` | Polls a pull request's review bots and CI with `gh`. The agent fixes findings, commits, pushes and replies on review threads. Once the PR is clean, it saves a review record to the repository's brain. The `pr_babysit_trigger` hook asks the agent to start it after `gh pr create`. |
| `create-rule` | Drafts a team rule, replays it over this machine's sessions, and files it as `proposed` with `create_rule`. |
| `start-rulebook` | Fills starter rules in from a scan of the repository. If you choose, it also mines rules from your CLAUDE.md, the last 30 days of Claude Code, Codex and Cursor sessions on this machine, and Claude Code `/insights` facets. Working files go to a temp directory or `mine-out/` in the current directory. It files rules as `proposed` and never activates one. |
| `spec`, `spec-work`, `spec-check`, `spec-maintain` | Spec-driven work against Git specs or brain documents. A `--cloud` bootstrap runs on the MemHub backend. |
| `companion` | Checks, shows or hides the companion (see [Companion](#companion-claude-code-21287)). |

## What it runs, sends, and fetches

### Hooks

Every hook is the same command,
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_entry.py" <event> <name>`. Hooks
are declared in `hooks/claude-hooks.json`. `scripts/hook_entry.py` reads the
hook input once. It checks any payload filter (on the tool call's command or
tool name, never its output), then runs the named script with the same Python:
in the same process for the hooks Claude Code waits on, in a separate process
for the `async` ones. Every handler first passes `claude_hook_guard`. If the hook
input comes from Cursor or Codex instead of Claude Code, the guard stops the
handler. At a Cursor turn end, it starts the plugin's Cursor capture
(`cursor_flush.py`) instead.

Claude Code always sets `CLAUDE_PLUGIN_ROOT` for plugin hooks. A host that
loads `hooks/claude-hooks.json` without setting it gets the path
`/scripts/hook_entry.py`. Python then exits 2, and the host treats exit 2 on
PreToolUse, UserPromptSubmit and Stop as a block. Set `CLAUDE_PLUGIN_ROOT` to
the plugin folder when you run these hooks in another host.

| Event (matcher) | Name | What it does | Sends data? |
| --- | --- | --- | --- |
| PreToolUse (`Bash`, `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `Read`) | `rulebook_hook pre` | Checks the call against your cached team rules. It can add an advisory or **deny** the call when a gate rule matches. It refreshes the rule cache in a detached background process once the cache is a minute old. | Yes, see [Rulebook](#rulebook) |
| PreToolUse (`mcp__*__add_memory`) | `add_memory_gate` | Denies MemHub's `add_memory` while this plugin is already capturing the session, so a turn isn't stored twice. | No |
| PreToolUse (`mcp__*__create_rule`) | `create_rule_origin` | Adds this session's id and the current turn to MemHub's `create_rule` call (not to a harness draft, which carries its own), so the rule records the session it was filed from. Never approves or blocks the call. | Yes, the session id and turn, inside the `create_rule` call |
| PostToolUse (`Bash`) | `flush_session` | After a command that actually ran `git commit`, `gh pr create` or `gh pr merge`, uploads the transcript so far. Runs in the background. | Yes, the transcript |
| PostToolUse (`Edit`, `MultiEdit`, `Write`, `NotebookEdit`) | `artifact_sync_reminder` | If the edited file belongs to a spec in the repository (by spec frontmatter), reminds the agent once per session. | No |
| PostToolUse (`Bash`) | `pr_babysit_trigger` | After a successful `gh pr create`, tells the agent to start a `/memhub:pr-babysit` loop on the new PR. | No |
| PostToolUse (`Edit`, `MultiEdit`, `Write`, `Bash`) | `md_capture` | Records which Markdown files the session wrote, or for Bash the working directory, in a local state file, and touches a per-session activity marker. | No |
| PostToolUse (`Bash`, `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `Read`) | `rulebook_hook post` | Rule advisories on failed results and on files a Bash call wrote, plus tracking for ordering rules. | Yes, see [Rulebook](#rulebook) |
| PostToolUse (`Bash`, `mcp__*github*__*`) | `pr_link_trigger` | When a GitHub call's output names exactly one pull request, asks MemHub whether to link this session to it. It may then tell the agent to call the `link_pr` tool. | Yes, the PR URL |
| SessionStart | `capture_health` | Warns you when capture is unauthenticated or recently failed. Checks plugin compatibility with MemHub and whether a newer release exists. | Yes, see [Network destinations](#network-destinations) |
| SessionStart | `brain_brief brief` | Gives the agent a short map of the repository's agent brain from a local cache. Starts a detached process that refreshes its recall pointers. | The detached process does |
| SessionStart | `rulebook_hook session` | Loads your team rules into the session. Fetches them first if the cache is stale. | Yes, the repository name |
| SessionStart | `harness_stop session` | Unless `MEMHUB_HARNESS_EXTRACT` turns it off. Tells the agent the hand-off rule, unseen by you. See [Harness-tied rule drafting](#harness-tied-rule-drafting-on-by-default). | No |
| UserPromptSubmit | `brain_brief prompt` | Delivers brain pointers the session-start brief didn't have yet. | No |
| UserPromptSubmit | `rulebook_hook prompt` | Fires rules written for prompts. These only advise. | No. Fires are logged locally and uploaded at Stop |
| Stop | `flush_turn` | Uploads the transcript bytes written since the last successful upload. Runs in the background. | Yes, the transcript |
| Stop | `brain_brief refresh` | Refreshes the cached brain overview, at most every 6 hours. Runs in the background. | Yes, a brain id |
| Stop | `md_capture_flush` | Saves qualifying Markdown files as draft artifacts. Runs in the background. See [Markdown capture](#markdown-capture). | Yes, file contents |
| Stop | `rulebook_hook flush` | Uploads the rule-fire and rule-event logs. Once a day it also deletes stale local state. Runs in the background. | Yes, identifiers |
| Stop | `harness_stop stop` | Unless `MEMHUB_HARNESS_EXTRACT` turns it off. | Yes, when on |
| SessionEnd | `session_end` | Runs `flush_session.py` (re-sends the whole transcript as a backstop), then `rulebook_hook flush final`. Runs in the background. | Yes, the transcript |

### Session capture

Sessions upload automatically through the `import_conversation` MCP tool,
from the Stop, SessionEnd and commit/PR hooks above. They go to **your
personal memory, never into a brain**. Each upload also carries the session's
title, the repository name (the origin remote's basename) and any GitHub
pull-request URLs that `gh pr create` printed in the session. Slash-command
bookkeeping records are left out, and a tool result over 200,000 bytes is
replaced by a note giving its size. Before upload, the plugin removes MemHub
keys (`mhk_…`, `xtk_…`) from the records and the title. Nothing else is
redacted. The server de-duplicates what it already has, so re-sending is safe.
`MEMHUB_TURN_FLUSH=0` turns off per-turn capture. The commit/PR and SessionEnd
uploads still run.

### Markdown capture

At the end of each turn, `md_capture_flush.py` reads Markdown files the
session wrote and saves them to MemHub as draft artifacts. These files come
from Edit, Write or MultiEdit calls, or from `git status` in a directory where
the session ran Bash. A file qualifies when:

- it is a `.md` file of 6,000 to 2,000,000 bytes; or
- it is a `.md` file whose YAML frontmatter has the line `memhub: artifact`,
  which skips the 6,000-byte floor.

Files found through `git status` must also be modified or untracked, and newer
than the session's start.

A prefilter (`md_capture_prefilter.py`) skips the turn-end pass when nothing
is pending: no Edit/Write path is waiting, and the last `git status` pass
found nothing outstanding with no Bash, Edit, MultiEdit or Write call since.
A file changed by anything else (your editor, a background job) after such a
pass is picked up by the next turn that runs one of those calls, or within
five minutes.

A file never qualifies if its path contains `/.claude/`, `/scratchpad/`,
`/tmp/`, `/private/tmp/`, `/var/folders/`, `/node_modules/` or `/.git/`. The
same goes for files named `CLAUDE.md`, `AGENTS.md` or `MEMORY.md` and files in
the OS temp directory. The `git status` search also skips ignored files,
submodules, symlinks and paths outside the repository. At most five files are saved
per turn, largest first. Content is redacted the same way as transcripts.
Each save carries the `auto-captured` tag. It goes to the repository's agent
brain when one is known (`~/.config/memhub-plugin/rooms.json`). When none is
recorded, the plugin asks MemHub for a brain named after the origin remote's
`<org>/<name>` and records the answer; a miss is asked again after a day. It
never creates one. Without a brain, the file goes to your personal memory. A
file goes up again only when its content changes. There is no setting that
turns this off on its own. `MEMHUB_TURN_FLUSH=0` does not affect it. To keep a
file out, keep it below the size floor or in one of the excluded locations, or
disable the plugin.

### Rulebook

`rulebook_hook.py` sends:

- **fetch:** the repository name (the origin remote's basename, else the
  directory name).
- **fires and fire events:** rule id, session, repo, branch, tool, a hashed
  checkout id, timestamps and the judge's verdict. Events record what followed
  a fire: a command that satisfied it, a turn or session end, or a rule you
  set aside. A fire on a call you overrode, and an event for a rule you set
  aside, also carry the reason you gave (redacted, up to 2,000 characters).
  The matched excerpt stays in the local log.
- **rules tied to files or commands (anchor rules):** nothing. They are
  matched on this machine against the cached rules: a rule fires when one of
  its anchors appears in the command or the edited file's path as a whole
  identifier. Set `MEMHUB_RULEBOOK_RECALL=0` to turn this matching off.
- **judge:** when a rule fires on a call, it asks MemHub whether the rule fits
  the turn. The request carries your current message (up to 2,000
  characters), a stripped copy of the turn (the agent's text, one line per
  tool call, the first 300 characters of each result), the call, and the
  fired rule ids. It goes through the same denylist redaction, which can miss
  things. Set `MEMHUB_RULEBOOK_JUDGE=0` to turn this off.

Every rule that fires is shown to you as `📏 Rule fired: …`, or `⛔️` when a
gate blocked the call. The agent is told to repeat the same line. A blocked
Bash call can be overridden with `RULEBOOK_OVERRIDE='<why>' <command>`, and a
blocked edit with a `rulebook-override[<rule>]: <why>` line in the new
content.
`MEMHUB_RULEBOOK_FETCH=0` stops fetching rules, and the cached ones keep
applying.

On Claude Code with mods (2.1.287+), the plugin's mod (`mod/`) checks these
rules in-process and the command hooks step aside for the lanes it serves.
MemHub can switch that off remotely: the mod reads `mod_lanes` from
`GET /v1/plugin/compatibility` when the session starts (before it takes any
lane) and every five minutes after. When it reads `false`, the mod hands
every lane back to the command hooks for the rest of the session and the
status line says `MemHub rules: served by the command hooks (remote switch)`.
A later `true` does not take them back; the next session (or a reload of the
plugin) reads the switch afresh. A failed check changes nothing, and a server
that sends no `mod_lanes` leaves the mod serving.

### Brain brief and PR linking

`brain_brief.py` runs a detached `pointers` process. It searches the
repository's brain and your sessions with identifiers taken from the branch:
PR and ticket numbers, plus the basenames of files changed against the
default branch, left uncommitted, or changed in the last 20 commits. The
search goes through the `search_memory` tool. `MEMHUB_BRIEF_POINTERS=0` turns
this off.
`pr_link_trigger.py` sends the pull request URL to
`/v1/team/pr-links/check`. It caches a "not connected" answer for 30 minutes
(`MEMHUB_PRLINK_NEGATIVE_TTL_S`).

### Harness-tied rule drafting (on by default)

Unless `MEMHUB_HARNESS_EXTRACT` is set to something other than `1`, `on`,
`true` or `yes` (`0` turns it off; unset or blank is on), each Stop rebuilds the previous turn from the transcript. It redacts that
window (MemHub keys, home directories, e-mail addresses, command-line
credentials) and sends it to `/v1/team/rulebook/harness/classify`. When the
classifier signals a candidate rule, the agent is asked once to start a
background fork of itself. The fork files a **proposed** rule with
`create_rule`. Nothing is activated without a person. The local files, under
`~/.config/memhub-plugin/harness/`, are `stop.log` (one line per Stop, with no
prompt text), `offsets/` (where the next transcript read may start) and
`judged/` (one empty file per judged turn, so a machine with both the
`memhub` and `memhub-staging` installs judges each turn once).
`harnessDrafting: false` turns all of this off.

### Companion (Claude Code 2.1.287+)

`companion/register.ts` is part of the plugin's Claude Code mod (a
function-hook module: `hooks/hooks.json` loads `mod/register.ts`, which
registers the companion last). Mods are on by default from Claude Code 2.1.287
(the desktop Code tab from 2.1.286) and load with the plugin; there is
nothing to turn on, and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` is ignored.
`disableAllHooks`, `--safe-mode`, an organization policy or Anthropic
switching mods off remotely stops it. `/<animal> off` hides it. It writes nothing to `~/.claude/settings.json`. The
module draws an animal above the prompt and reacts to session, turn, prompt
and tool events, including rule fires. It saves its preferences in Claude
Code's plugin store. It reads the rules that fired from the mod's own
session state, and the classic hook answer only where the mod's Rulebook
lanes are off. It talks to MemHub itself, over Claude Code's HTTP client
with your access key (resolved once a session by
`python3 scripts/rulebook_mod_cli.py api-info`):

- `GET /v1/team/rulebook/rules?status=eq.proposed&author=eq.xtrace` when the
  session starts, every five minutes, and when a turn ends, to list rules
  proposed from this session;
- `PATCH /v1/team/rulebook/rules/<id>` when you press Activate or Reject.

The only processes it starts are `open` or `xdg-open`, to open a rule in
MemHub Studio (`https://mem.xtrace.ai`) when you click it.

### Codex and Cursor

This package also carries Codex and Cursor manifests (`.codex-plugin/`,
`.cursor-plugin/`, `plugin.json`, `mcp.json`) and hook configs
(`hooks/codex-hooks.json`, `hooks/cursor-hooks.json`,
`hooks/cursor_capture.cmd`). On those hosts the hooks run the same capture,
rulebook and brain-brief scripts through `scripts/codex_hook_bridge.py` and
`scripts/cursor_capture.py`, and Cursor capture also reads Cursor's session
store under `~/.cursor/`. `/memhub:onboard` merges MemHub's handlers into
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), keeps unrelated
handlers, backs up a file it changes, and copies a launcher to
`$CODEX_HOME/memhub_hook_bridge.py`. Codex asks you to review the hooks
before they run.

### Network destinations

- `https://api.memhub.xtrace.ai`: the MCP server (`/mcp-server/mcp`) and REST
  routes under `/v1/`. These cover rules, rule fires and fire events, the
  rule judge, pr-links, harness classify,
  `/v1/plugin/compatibility` and `/v1/developer/access-tokens` (used only by
  `/memhub:login`). Every request carries your credential, except the OAuth
  discovery requests `/memhub:login` sends before it has one. Plain `http` is
  refused. Requests through the shared transport (`scripts/mcp_http.py`)
  also carry an `X-MemHub-Plugin-Version` header and don't follow redirects.
  The access-key calls (`scripts/pak.py`) carry no version header and use
  Python's default URL opener, which follows a redirect on a GET.
- `https://mem.xtrace.ai`: the plugin sends nothing here. After
  `/memhub:login` succeeds, your browser is sent to the setup guide on this
  site.
- `https://memhub-prod.us.auth0.com`: OAuth login from `/memhub:login` and
  `/mcp`. Background hooks (capture flushes, the session brief, the rule
  judge) can also refresh a cached OAuth token here: a GET of the discovery
  document and a POST of the refresh token to its token endpoint. This
  happens only when no option, `$MEMHUB_TOKEN` or stored access key is set
  and the cached token is stale.
- `https://raw.githubusercontent.com/XTraceAI/agent-plugins/`: the release
  check at session start (`scripts/plugin_updates.py`). It reads the public
  marketplace manifest and plugin manifest, at most once an hour, with a
  0.75-second timeout. **No credentials or session data are sent.** The
  result is cached in `~/.config/memhub-plugin/releases/`.

The plugin itself makes no other network calls. In particular, it makes no
calls to the GitHub API of its own. GitHub traffic comes only from `gh` and
`git`: the commands the agent runs, for example in `/memhub:pr-babysit` or
`/memhub:link-pr`, and the `gh pr view` and `gh api` calls the
`find-contributing-sessions` script makes with your `gh` login.

### Commands it starts

- `python3` runs every hook and helper script, including the skills' scripts
  and the detached background refreshes (`rulebook_hook fetch`,
  `brain_brief pointers`).
- `git` runs read-only queries, such as `remote get-url`, `rev-parse`,
  `status`, `diff`, `log`, `ls-files`, `merge-base` and `worktree list`. These
  identify the repository, find changed Markdown files and evaluate rules.
- `open` or `xdg-open` (companion only) and your browser open pages. The
  browser opens for `/memhub:login`, and for the `save-artifact`, `onboard`
  and `import-session` scripts when they find no access key or usable cached
  login.
- The plugin's hooks never run `gh`. The `find-contributing-sessions` script
  runs `gh pr view` and `gh api`. The `pr-babysit`, `link-pr` and
  `find-contributing-sessions` skills ask the agent to run `gh`, and
  `pr-babysit` also has it commit, push and reply on review threads.

### Local state

Everything lives under `~/.config/memhub-plugin/` unless noted:

- `pak-<api-host>.json` and `tokens-<api-host>.json`: credentials, mode 0600;
- `turnflush/`, `codexflush/`, `cursorflush/`: capture cursors, locks and the
  last error;
- `mdcapture/`: Markdown capture bookkeeping;
- `rulebook/`: cached rule books, per-session state, the fire and event logs;
- `overview/`: brain-brief caches;
- `rooms.json`: which brain each repository maps to;
- `prlink/`: the PR-link negative cache;
- `compatibility/`, `releases/`: upgrade-check results;
- `harness/stop.log`, `harness/offsets/`, `harness/judged/`: unless `MEMHUB_HARNESS_EXTRACT` turns the harness off;
- `rules-from-sessions/`: what `/memhub:start-rulebook` noted about each
  session it read, so it reads a session once;
- `~/.claude/.memhub/directive_fired/`: what the brief already showed this
  session;
- the OS temp directory: `memhub-spec-reminder-<session>.json` for the spec
  reminder.

Once a day, `scripts/state_sweep.py` deletes stale capture, Markdown-capture
and brain-brief pointer files (after 7 days) and stale rulebook state (after
30).
`python3 scripts/state_sweep.py --dry-run` shows what it would delete.

### Strings that look like credential reads

A few files contain text that resembles a command reading a credential. None of
them reads one, and none of that text is ever run:

- The start-rulebook skill and its catalog hold the **"Never read secrets"**
  starter rule. Its test cases are, by design, the kinds of command it stops:
  ones that print the shell environment or open dotenv files. The rule is
  replayed against them as text before it is filed, and the session miner
  replays recorded commands the same way.
- The rulebook hook and the redaction module list credential patterns (API-key
  flags, authorization headers, GitHub, AWS and MemHub key prefixes) so they can
  strip matching values before anything is sent.
- The PR-link parser reads shell commands that address GitHub to find the pull
  request they name, skipping command wrappers such as the one that sets
  variables for a single command. It reads no environment variable, and the
  plugin's hooks never call GitHub (see Commands it starts).

## Configuration

| Setting | Effect |
| --- | --- |
| `memhub_token` (userConfig) | Credential for hooks, ahead of everything else |
| `MEMHUB_TOKEN` | Credential for hooks and scripts when the option is unset |
| `MEMHUB_TURN_FLUSH=0` | Turns off per-turn capture, and with it the session-start capture-health, compatibility and update notices |
| `MEMHUB_RULEBOOK_RECALL=0` | Turns off anchor rules (rules tied to files or commands, matched locally) |
| `MEMHUB_RULEBOOK_JUDGE=0` | Stops sending turns to the rule judge |
| `MEMHUB_RULEBOOK_FETCH=0` | Stops fetching rules (the cache keeps applying) |
| `MEMHUB_BRIEF_POINTERS=0` | Turns off brain-brief recall searches |
| `MEMHUB_BRIEF_TOKEN_BUDGET` | Session-start context budget (default 2,500 tokens) |
| `MEMHUB_HARNESS_EXTRACT` | `0` turns off harness-tied rule drafting (on by default) |
| `MEMHUB_MCP_BASE_URL`, `MEMHUB_MCP_SERVER_PATH` | Point hooks and scripts at another MemHub (MCP tools follow `.mcp.json`) |
| `MEMHUB_PRLINK_NEGATIVE_TTL_S` | PR-link negative cache lifetime; `0` turns it off |
| `MEMHUB_RULEBOOK_BASE`, `MEMHUB_STATE_DIR`, `MEMHUB_ROOMS_FILE`, `MEMHUB_HARNESS_DIR` | Move local state |
| `MEMHUB_SPEC_DIR` | Where the spec reminder and onboarding look for specs |
| `MEMHUB_PLUGIN_SCRIPTS` | Where the `find-contributing-sessions` and `start-rulebook` scripts load the plugin's scripts from, ahead of this plugin's own folder |
| `MEMHUB_RULEBOOK_BASE_BRANCH` | Base branch for rule diff checks |
| `MEMHUB_FLUSH_DEADLINE_S`, `MEMHUB_TURN_FLUSH_TIMEOUT_S`, `MEMHUB_RULEBOOK_TIMEOUT_S`, `MEMHUB_HARNESS_CLASSIFY_TIMEOUT`, `MEMHUB_OAUTH_TIMEOUT`, `MEMHUB_OAUTH_BIND_TIMEOUT` | Timeouts |
| `MEMHUB_PAK_LABEL` | Label for the access key `/memhub:login` mints |
| `MEMHUB_RULEBOOK_DEBUG`, `MEMHUB_HARNESS_DEBUG` | Print hook tracebacks to stderr |
| `MEMHUB_RULEBOOK_HOOK_VERSION` | Test override for the hook version that rules' minimum-version checks compare against |
| `MEMHUB_HARNESS_CHILD` | Marks a `claude` process started by XTrace's test tooling: it is not captured, `add_memory` is not gated, and harness drafting is off. The plugin never sets it |

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 XTrace Inc.
