<!-- create-rule step 4: verifier case syntax per rule shape. Read from SKILL.md. -->

Every recipe below was run and exits 0. Write each JSON file with the Write
tool; return to SKILL.md step 4 for what the table means.

**Facts per case — the `--cases` file.** A case may be an object carrying the
facts the rule's `given` asks about: `branch`, `diff_paths`, `diff_lines`,
`dirty`, `user_said` (a list of what the person typed), `file_lines`,
`agent_main`, `cwd`. Prefer it to the global flags (`--branch`, `--diff-path`,
`--diff-lines`, `--dirty`, `--user-said`, `--file-lines`, `--agent-main`):
those apply to EVERY case, including the generated `grep` / `python -c` ones,
so `--branch main` makes them fire and the run fails. A rule with a `user`
fact needs `user_said` on its fires cases — with no transcript the fact is
unknown, and an unknown fact never fires. Generated cases get no facts at all,
so for a `given` rule they pass trivially: put your own mention cases in the
file, with the facts set.

**A `given` rule** — never push to main:

```json
{"title": "Never push to main",
 "statement": "Don't push to main or master; push a branch and open a PR.",
 "delivery": "agent_hook",
 "matcher": {"event": "bash", "command_rx": "\\bgit\\s+push\\b",
  "command_not_rx": "'[^';&|]*\\bgit\\s+push\\b[^']*'|\"[^\";&|]*\\bgit\\s+push\\b[^\"]*\"",
  "given": {"repo": {"branch_rx": "^(main|master)$"}}}}
```

```json
{"fires":  [{"case": "git push origin HEAD", "branch": "main"},
            {"case": "cd .. && git push", "branch": "master"}],
 "silent": [{"case": "git push -u origin feat/eng-1204", "branch": "feat/eng-1204"},
            {"case": "grep -rn \"git push\" docs/", "branch": "main"},
            {"case": "python3 -c 'print(\"git push\")'", "branch": "main"}]}
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --cases /tmp/cases.json
```

**A `read` rule** — a case is the Read tool (`read:<path>`, narrowed with
`@<offset>,<limit>`) or a shell command run through the hook's own parser
(`bash:<command>`, relative paths against `--cwd`); `file_lines` stands in for
the file's length, so no real file is needed. The delegate-big-reads rule:

```json
{"title": "Delegate big file reads to a subagent",
 "statement": "Reading a 300+ line file pulls it whole into the main context; read a range with offset/limit or hand it to a subagent.",
 "delivery": "agent_hook",
 "matcher": {"event": "read", "given": {"file": {"lines_gt": 300}, "agent": {"main": true}}}}
```

```json
{"fires":  [{"case": "read:/w/MemHub-Backend/app/mcp_server.py", "file_lines": 6200},
            {"case": "bash:cd app && cat mcp_server.py", "file_lines": 6200}],
 "silent": [{"case": "read:/w/MemHub-Backend/app/mcp_server.py@5500,120", "file_lines": 6200},
            {"case": "read:/w/MemHub-Backend/app/mcp_server.py", "file_lines": 6200, "agent_main": false},
            {"case": "read:/w/MemHub-Backend/app/config.py", "file_lines": 120},
            {"case": "bash:grep -n create_rule app/mcp_server.py", "file_lines": 6200}]}
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --cwd /w/MemHub-Backend --cases /tmp/cases.json
```

The `agent_main: false` case is the one that proves delegation is the way
past the rule.

**An `edit` rule** — a case is `path::content`. Feature gates go in the flag
control plane:

```json
{"title": "Feature gates go in the flag control plane",
 "statement": "A new *_ENABLED setting in app/core/config.py is a feature gate; add it to the flag control plane instead.",
 "delivery": "agent_hook",
 "matcher": {"event": "edit", "path_rx": "(^|/)app/core/config\\.py$",
  "content_rx": "^\\s*\\w+_ENABLED\\s*:\\s*bool\\b"}}
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'app/core/config.py::    BRAIN_TOPICS_ENABLED: bool = False' \
  --silent 'app/core/config.py::    BRAIN_TOPICS_BATCH_SIZE: int = 50' \
  --silent 'app/services/flags.py::    BRAIN_TOPICS_ENABLED: bool = False'
```

**An `output` rule** — a case is `command::output` (with no `::` the whole
case is the output and the command defaults to `pytest`). A rejected push
means fetch and rebase:

```json
{"title": "A refused push means fetch and rebase",
 "statement": "When a push is rejected as non-fast-forward, fetch and rebase onto the remote branch; never force it.",
 "delivery": "agent_hook",
 "matcher": {"event": "output", "command_rx": "\\bgit\\s+push\\b",
  "content_rx": "\\[rejected\\].*\\(non-fast-forward\\)|\\(fetch first\\)"}}
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'git push origin feat/x:: ! [rejected]        feat/x -> feat/x (non-fast-forward)' \
  --silent 'git push origin feat/x::   3f2c1a0..9b8d7e6  feat/x -> feat/x'
```

An `ordering` rule is verified as a sequence of steps joined by ` >> `
(`edit:<path>`, `ok:<cmd>` a green receipt, `red:<cmd>` a red one, `session`
the SessionStart arming, `prompt:<what the person typed>` the
UserPromptSubmit arming, and last `gate:<cmd>`); the case fires when that
final call is gated. Use the arming step your rule's `armed_by_events` names
— a case that never arms the rule can never fire:

```bash
# armed_by_events: ["edit", "write"]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'edit:src/a.py >> gate:git push' \
  --fires  'edit:src/a.py >> red:pytest tests >> gate:git push' \
  --silent 'edit:src/a.py >> ok:pytest tests >> gate:git push'

# armed_by_events: ["session"]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'session >> gate:git log origin/main' \
  --silent 'session >> ok:git fetch -q >> gate:git log origin/main'

# armed_by_events: ["prompt"] + armed_by_rx
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'prompt:is the staging brain 404ing? >> gate:gh pr comment 7 --body ok' \
  --silent 'prompt:how is production? >> gate:gh pr comment 7 --body ok' \
  --silent 'prompt:check staging >> ok:curl -s https://staging/health >> gate:gh pr comment 7 --body ok'
```

A `prompt:` whose text does not match `armed_by_rx` arms nothing, so it is
the natural `--silent` case: it proves the rule stays quiet when nobody
raised the subject.

An `event: "prompt"` matcher rule (not an ordering rule) takes the whole
prompt as its case, wrappers and all — no `prompt:` step, no `>>`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  '<task-notification><status>completed</status></task-notification>' \
  --silent 'what is the loop doing?'
```
