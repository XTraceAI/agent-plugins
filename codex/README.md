# MemHub for OpenAI Codex

> **Moved (multi-host refactor):** the transform now lives at
> `plugins/memhub/scripts/readers/codex.py`, and the import entry point is
> `plugins/memhub/scripts/capture.py import --host codex`. The commands below
> still work — `codex_to_claude.py` and `import_codex_session.py` are thin
> forwarding shims kept for one release.

Codex isn't a Claude Code plugin, so the marketplace at the repo root doesn't
apply to it. Instead MemHub reaches Codex two ways, and neither needs a new
repo or a backend change:

1. **Memory tools inside Codex** — the MemHub MCP server is plain MCP, and
   Codex speaks MCP. Add one block to `~/.codex/config.toml` and
   `search_memory` / `save_artifact` / `import_conversation` are available in
   Codex.
2. **Session capture** — `import_codex_session.py` reads a Codex *rollout*
   transcript, reshapes it into the Claude Code record shape, and hands it to
   the plugin's `import_session.py`. That reshape is the whole trick: MemHub's
   agentic (tool-aware, gist-composing) ingestion auto-detects by *structure*,
   not by a platform tag, so a faithful transform gets the full extraction with
   no server change.

## 1. Memory tools in Codex (MCP)

Add to `~/.codex/config.toml` (prod shown; swap the URL for staging if you're a
MemHub developer):

```toml
[mcp_servers.memhub]
url = "https://api.memhub.xtrace.ai/mcp-server/mcp"
# staging: "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"
```

Codex handles the OAuth browser flow on first use (same as its Notion server).
Verify with `codex mcp` / `codex doctor`.

## 2. Import a Codex session into team memory

```bash
# newest session:
uv run --with 'mcp<2' python codex/import_codex_session.py --session latest

# a specific session (rollout path, or the bare session id), into a shared room:
uv run --with 'mcp<2' python codex/import_codex_session.py \
    --session 019c6e48-b66c-7881-9301-99c87fc66cf6 \
    --agent-brain-id <room-id>
```

`--session` accepts a rollout path, a bare Codex session id (searched under
`~/.codex/sessions/`), or `latest`. The conversation id defaults to
`codex-<session-id>`, so re-imports are **incremental** — the server watermark
folds the session gist forward instead of duplicating.

Use `--dry-run` to see what would be sent (record count, tool calls, resolved
cwd/title) and write the transformed transcript without calling the server.
Auth is the same OAuth the MCP connector uses — no separate token to provision.

### What the transform does

Codex rollouts carry two parallel streams; capture reads the `response_item`
stream (the OpenAI Responses items actually exchanged with the model — the one
with tool I/O in order) and maps it 1:1 onto Claude Code records:

| Codex `response_item` | Claude Code record |
|---|---|
| `message` role=user | user text (Codex context injections — AGENTS.md, `<environment_context>`, IDE-setup wrappers — are stripped to the real ask) |
| `message` role=assistant | assistant text block |
| `reasoning` | assistant `thinking` block (summary only; `encrypted_content` dropped) |
| `function_call` / `custom_tool_call` | assistant `tool_use` block |
| `function_call_output` / `custom_tool_call_output` | user `tool_result` block |

Order is preserved (gpt-5.x emits `reasoning` before its `function_call`). A
leading provenance banner records the Codex origin, model, and cwd, since the
agentic path always tags the platform `claude`.

Run the tests: `python3 codex/test_codex_to_claude.py`.

## 3. Automatic capture and directive recall — the hooks bridge

After installing the plugin, ask Codex to `set up MemHub`. In the Codex CLI,
skills are model-invoked; `/memhub:setup` is not a slash command. The same
operation is available directly:

```bash
python3 plugins/memhub/scripts/setup_codex_hooks.py install
```

The installer preserves unrelated hooks, backs up a changed existing file, and
survives plugin upgrades. It folds all behavior into three handlers: one each
for `PreToolUse`, `PostToolUse`, and `Stop`. The bridge still provides directive
recall, reactive failure recall, artifact reminders, milestone capture, and
per-turn capture.

Codex requires an explicit review because trust is per command hash. MemHub
cannot approve its own hooks. Restart Codex, choose **Review hooks** at startup
or open `/hooks`, and trust only the three handlers with both of these traits:

- source: `User config - ~/.codex/hooks.json`
- command contains: `memhub_hook_bridge.py`

If more than three handlers need review, the others are unrelated to MemHub's
installer. Do not choose **Trust all** unless you have separately reviewed
those too. Re-run `setup_codex_hooks.py status` to verify installation; Codex
does not expose trust approval to this setup script, so the script reports that
state separately instead of pretending the handlers are active.

**Why user-level and not the plugin's own `hooks/codex-hooks.json`.** Verified
live on codex-cli 0.148 and 0.149: plugin-bundled hooks are **not mounted**, via
either a manifest pointer or the default `hooks/hooks.json` path. User-level
`~/.codex/hooks.json` hooks do fire, and 0.149 delivers
`PreToolUse.hookSpecificOutput.additionalContext` to the model. The plugin keeps
its native hook declaration ready for the Codex release that mounts it; the
bridge is the working compatibility path today.

The installed trampoline resolves the highest naturally ordered plugin version
at run time instead of naming one. Natural ordering matters: lexical ordering
puts `0.9.0` above `0.10.0`, while mtime can be changed by merely touching a
directory. The version directory is the installer's actual upgrade boundary.

The marketplace namespace is **pinned** (`cache/xtrace-plugins/memhub/*/`),
only the version segment is a wildcard. The cache is shared by every
marketplace you have installed — `openai-bundled`, `openai-curated-remote`,
and so on all live beside ours — so a namespace wildcard would let any other
marketplace shipping a plugin named `memhub` supply the code this hook runs.
Pinning removes that without giving up upgrade-safety.

**Trust assumption.** The bridge runs scripts from the highest-versioned directory
under `~/.codex/plugins/cache/xtrace-plugins/memhub/`,
without verifying a signature or hash. That directory is inside your own home:
writing a higher-versioned sibling there requires the ability to write your home
directory, and anything with that ability can already rewrite `~/.codex/hooks.json`
(which *defines* this hook), edit the installed plugin in place, or alter your
shell startup — so the bridge is not a distinct escalation path, and a hash
stored in that same writable tree would verify nothing an attacker couldn't also
change. The assumption, stated plainly, is that `~/.codex/plugins/cache/` is
written only by the Codex plugin installer and by you. Pinning an exact version
instead would trade this for a concrete regression: capture silently stops on
every plugin upgrade until the pin is bumped.

Four implementation facts matter if you edit the bridge:

* every handler drains stdin;
* one dispatcher per event keeps the review surface to three command hashes;
* `"async": true` hooks are killed with `codex exec`, so capture detaches from
  the synchronous trampoline itself;
* Codex ignores plain text from `PreToolUse` and `PostToolUse`; model-visible
  bridge output uses `hookSpecificOutput.additionalContext`.

## 4. Auto-capture via `notify` (legacy, verify on your Codex version)

Predates the bridge and is strictly worse — it imports whole rollouts on a
debounce rather than flushing incrementally, and Codex allows only ONE `notify`
program, so this conflicts with anything else already using that slot (the
ChatGPT desktop client claims it). Prefer the bridge above.

Codex's `notify` config runs a program on session events. Point it at a wrapper
that imports the newest rollout when a session ends:

```toml
# ~/.codex/config.toml
notify = ["python3", "/absolute/path/to/codex/codex_notify.py"]
```

`codex_notify.py` is a thin filter: on a turn/session-completion event it runs
`import_codex_session.py` detached, with a debounce (one auto-import per ~2 min)
so a burst of turns doesn't re-send the whole rollout each time. It imports the
session named in the notify event when the payload identifies one; otherwise it
falls back to `--session latest` (the newest rollout by mtime), which is the
right session only when a **single** Codex session is active — with concurrent
sessions, prefer the manual import. Whether Codex emits a usable completion
event (and which id fields it carries) varies by version — confirm with your
build before relying on it; the manual import above always works.
