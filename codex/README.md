# MemHub for OpenAI Codex

MemHub is a native Codex plugin. Installing it provides:

- the `memhub` MCP server for interactive memory tools;
- skills for login, onboarding, search, artifacts, imports, handoffs, specs,
  rules, and PR review;
- automatic session capture and directive recall through a three-hook
  compatibility bridge.

Do not configure MemHub as a separate global MCP server. The installed plugin
already supplies it.

## Fresh installation

Run these commands in a terminal:

```bash
codex plugin marketplace add XTraceAI/agent-plugins
codex plugin add memhub@xtrace-plugins
```

Then fully restart Codex so the installed skills and MCP server load.

Check the installed package:

```bash
codex plugin list --json
codex mcp list
```

The first command should list `memhub@xtrace-plugins` as installed and enabled.
The second should list one `memhub` server.

## 1. Connect the interactive memory tools

Codex normally starts the browser login while installing a plugin whose MCP
server requires authentication. If Codex still says `memhub` is not logged in,
run:

```bash
codex mcp login memhub --oauth-client-registration cimd
```

Open the printed URL and complete the browser approval. The authorization URL
should identify Codex with a client metadata document under
`https://chatgpt.com/oauth/codex/`; it should not contain MemHub's legacy static
OAuth client id.

This login enables interactive tools such as `search_memory`, `save_artifact`,
and `import_conversation`. It does not authenticate automatic capture.

## 2. Authenticate automatic capture

Start a new Codex task and ask:

```text
Log in to MemHub
```

The installed `login` skill opens a separate browser flow and provisions the
90-day personal access key used by background hooks. The connector login from
the previous step lives in Codex's credential store and is not available to
those hook processes, so both logins are required.

## 3. Install and approve the hooks bridge

Ask Codex:

```text
Set up MemHub
```

Codex 0.148 and 0.149 support user-level hooks but do not mount hooks bundled
inside an installed plugin. The `setup` skill therefore merges three MemHub
handlers into `~/.codex/hooks.json` while preserving unrelated hooks:

- `PreToolUse`: situated directive recall;
- `PostToolUse`: reactive recall, artifact reminders, and milestone capture;
- `Stop`: incremental session capture.

MemHub cannot approve its own command hooks. Restart Codex, choose **Review
hooks** or open `/hooks`, and trust only handlers with both of these traits:

- source: `User config - ~/.codex/hooks.json`;
- command contains: `memhub_hook_bridge.py`.

There should be one MemHub handler under each of `PreToolUse`, `PostToolUse`,
and `Stop`. Review any other waiting hooks separately instead of choosing
**Trust all**.

## 4. Connect the repository to its team brain

From the repository you want MemHub to remember, ask Codex:

```text
Onboard MemHub for this repo
```

The `onboard` skill creates or selects the repository's agent brain — and when it creates one, offers to share it with your org's default workspace, because a new brain is readable by its creator alone until it is shared —
stores the routing choice in the user's MemHub configuration, seeds the brain
from one substantive session, and verifies recall. Until a repository brain
exists, authenticated capture can still write to personal memory.

## Updating MemHub

Refresh the Git marketplace and reinstall the plugin package:

```bash
codex plugin marketplace upgrade xtrace-plugins
codex plugin remove memhub@xtrace-plugins
codex plugin add memhub@xtrace-plugins
```

Restart Codex after updating. The hooks bridge follows the installed plugin
version, so a normal update does not require rewriting `~/.codex/hooks.json`.
Asking Codex to `Set up MemHub` again is safe and verifies the bridge and
capture credential.

## Troubleshooting callback URL mismatch

The supported plugin login uses Codex's client metadata document (CIMD). A
manual global `memhub` MCP entry can shadow the plugin server and force the old
static OAuth client instead. The visible symptom is an Auth0 error like:

```text
unauthorized_client: Callback URL mismatch
```

This commonly follows a command that manually adds `memhub` with an OAuth
client id. Adding the reported random loopback port to Auth0 is not a fix; the
port changes between login attempts.

1. Fully exit Codex.
2. Clear credentials for the shadowing entry. If this reports that it was not
   logged in, continue:

   ```bash
   codex mcp logout memhub
   ```

3. Remove only the global MCP configuration. This does not remove the MCP
   server supplied by the installed plugin:

   ```bash
   codex mcp remove memhub
   ```

4. Confirm no manual section remains:

   ```bash
   grep -n '^\[mcp_servers\.memhub' ~/.codex/config.toml
   ```

   Expected: no output.

5. Confirm the plugin-provided server still appears, then use CIMD explicitly:

   ```bash
   codex mcp list
   codex mcp login memhub --oauth-client-registration cimd
   ```

The new authorization URL's decoded `client_id` should begin with
`https://chatgpt.com/oauth/codex/`. If it is a short Auth0 application id, a
manual configuration is still taking precedence.

## Troubleshooting a local or stale marketplace

Pulling a source checkout does not update an installed Codex plugin. Check the
configured marketplace source:

```bash
codex plugin marketplace list --json
```

The public `xtrace-plugins` marketplace should be Git-backed by
`https://github.com/XTraceAI/agent-plugins.git`. If it points at an old local
clone, replace the registration and reinstall:

```bash
codex plugin remove memhub@xtrace-plugins
codex plugin marketplace remove xtrace-plugins
codex plugin marketplace add XTraceAI/agent-plugins
codex plugin add memhub@xtrace-plugins
```

Removing a local marketplace registration does not delete its source checkout
or its branches.

## Developer reference

The installed plugin reads Codex rollouts through
`plugins/memhub/scripts/readers/codex.py` and routes imports through
`plugins/memhub/scripts/capture.py`. Files under this repository's `codex/`
directory other than this guide are compatibility shims for older workflows;
new users should use the installed skills and plugin commands above.

Focused Codex integration tests:

```bash
python3 tests/codex_capture_test.py
python3 tests/codex_hooks_setup_test.py
python3 tests/codex_hooks_parity_test.py
```
