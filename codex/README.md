# MemHub for OpenAI Codex

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
uv run --with mcp python codex/import_codex_session.py --session latest

# a specific session (rollout path, or the bare session id), into a shared room:
uv run --with mcp python codex/import_codex_session.py \
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

## 3. Client-side encrypted artifacts from Codex

The same SDK-backed artifact scripts work when Codex is the client. Configure
the private `~/.config/memhub-plugin/.env` described in the root README, then
have Codex run the file-to-file flow without reading or echoing that file:

```bash
uv run --with 'mcp<2' --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/save_artifact.py \
  --file private-notes.md --name "Private notes" --encrypt \
  --agent-brain-id "<brain-id>"

uv run --with 'mcp<2' --with xtrace-ai-sdk==0.1.1 \
  python plugins/memhub/scripts/load_encrypted_artifact.py \
  --artifact-id "<artifact-id>" --agent-brain-id "<brain-id>" \
  --output /tmp/private-notes.decrypted
```

Codex can read the locally decrypted output, while MemHub receives and returns
only the SDK ciphertext. Names, tags, and other artifact metadata remain
visible. This is intentionally different from the session importer in section
2: normal session imports are plaintext so the server can perform agentic
extraction; encrypted artifacts are opaque and must be loaded directly by id.

## 4. Auto-capture (optional, verify on your Codex version)

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
```
