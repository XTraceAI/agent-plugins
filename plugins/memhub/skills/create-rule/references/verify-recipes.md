<!-- create-rule step 4: verifier case syntax per rule shape. Read from SKILL.md. -->

Every recipe below was run and exits 0. Write each JSON file with the Write
tool; return to SKILL.md step 4 for what the table means.

**Facts per case — the `--cases` file.** A case may be an object carrying the
facts the rule's `given` asks about: `branch`, `diff_paths`, `diff_lines`,
`dirty`, `user_said` (a list of what the person typed), `file_lines`,
`agent_main`, `cwd`, and for a `session` rule `context_tokens`,
`idle_seconds`, `turn_tool_calls`, `repeat_count` (the count INCLUDING this
match). Prefer it to the global flags (`--branch`, `--diff-path`,
`--diff-lines`, `--dirty`, `--user-said`, `--file-lines`, `--agent-main`,
`--context-tokens`, `--idle-seconds`, `--turn-tool-calls`, `--repeat-count`):
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

**A `session` rule** — delegate searches once the context is large:

```json
{"title": "Delegate searches once the context is large",
 "statement": "The context is past 300k tokens; hand broad searches to a subagent instead of pulling more output into this one.",
 "delivery": "agent_hook",
 "matcher": {"event": "bash", "command_rx": "\\b(?:rg|grep\\s+-r)\\b",
  "given": {"session": {"context_tokens_gt": 300000}}}}
```

```json
{"fires":  [{"case": "rg -n create_rule app/", "context_tokens": 340000}],
 "silent": [{"case": "rg -n create_rule app/", "context_tokens": 90000},
            {"case": "rg -n create_rule app/"},
            {"case": "ls app/", "context_tokens": 340000}]}
```

The bare case with no `context_tokens` is a host that cannot say (Cursor):
it must stay silent. A repeat rule's cases give `repeat_count`:
`{"case": "pytest tests/test_api.py -x", "repeat_count": 3}` fires a
`{"repeat_count_gt": 2, "repeat_key": "command"}` rule, `"repeat_count": 2`
does not.

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

# armed_by_events: ["command"] + armed_by_command_rx
# with /tmp/cand.json's ordering block:
#   {"armed_by_events": ["command"], "armed_by_command_rx": "\\bgit\\s+(?:mv|rm)\\b",
#    "required_command_rx": "\\bpytest\\b", "gated_command_rx": "\\bgit\\s+push\\b"}
# `ok:` runs the arming command successfully (it arms), `red:` runs it and it
# fails (it arms nothing); a gate: step that runs the arming command BEFORE
# the gated one in the same call is gated too.
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'ok:git mv old.py new.py >> gate:git push' \
  --fires  'gate:git mv old.py new.py && git push' \
  --silent 'red:git mv old.py new.py >> gate:git push' \
  --silent 'ok:git mv old.py new.py >> ok:pytest -q >> gate:git push' \
  --fires  'ok:git rm -r old || true >> gate:git push' \
  --silent "ok:git commit -m 'git mv old.py new.py' >> gate:git push"
```

A command-armed rule is armed by ANY segment that runs the arming command in
a call that did not fail — `git rm -r old || true` and `git mv a b; git status`
arm, though their exit status is not the move's (a move that failed behind
`|| true` arms too: one extra required run, the safe direction). A call that
failed arms nothing. Quoted text, `#` comments and heredoc bodies never arm,
and a quoted mention of the gated command (`echo "git push"`) is not a run of
it. The required command still has to
PASS to clear it, in either mode. Mind the search tools: `grep -rn old .` and
`rg old` exit 1 when they find NOTHING — the clean outcome — and an exit of 1
is a failed receipt, so a clean search does not clear the rule (a search that
finds leftovers does). `cmd || true`, `cmd; true` and `! cmd` do not help: the
receipt must be the command itself. Name a required command that exits 0 on
a clean check, and say this limit in the statement when you cannot.

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

An `event: "reply"` matcher takes the agent's final reply as its case, after
the shell commands its turn ran, joined by ` >> ` — `ok:<cmd>` succeeded,
`red:<cmd>` failed, `ran:<cmd>` has no result yet. With no step the turn ran
no command. Write the three silent cases every claim rule needs: the claim
BACKED by a green command, the claim only QUOTED (a code span, a fence, a
`"…"`), and a reply that makes no claim; and the fires case the evidence
pattern is most likely to miss — a red run of the right command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  'Done: PR merged and tests pass.' \
  --fires  'red:pytest -q >> Fixed, tests pass now.' \
  --silent 'ok:gh pr view 12 --json state >> PR merged.' \
  --silent 'ok:cd api && pytest -q tests/ >> All tests pass.' \
  --silent 'I have not run `pytest`, so I cannot say "tests pass" yet.' \
  --silent 'I opened the PR; it is ready for review.'
```

The evidence pattern is matched the way an ordering receipt is: a quoted
mention (`echo "pytest"`), a comment or a heredoc body is not a run, an
unquoted one anywhere in the command is, and the match is case-sensitive.

A GATE-mode `edit` rule also gets its QUARANTINE verified — what happens when
a shell command writes the file in a way the command line does not show, so
the rule can only match it afterwards (gate-and-given.md). The case is a
sequence joined by ` >> ` over a throwaway checkout: `wrote:<path>::<content>`
a shell-written file, `fixed:<path>::<content>` the same file edited,
`deleted:<path>` its removal, and last either `door:<command>` (fires when the
command is blocked) or `stop` (fires when the agent's Stop would be). Give at
least one door that fires, the fixed form, and the override:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" --rule-file /tmp/cand.json \
  --fires  "wrote:app/keys.py::API_KEY = 'sk-1' >> door:git commit -am wip" \
  --fires  "wrote:app/keys.py::API_KEY = 'sk-1' >> stop" \
  --silent "wrote:app/keys.py::API_KEY = 'sk-1' >> fixed:app/keys.py::API_KEY = env() >> door:git push" \
  --silent "wrote:app/keys.py::API_KEY = 'sk-1' >> door:RULEBOOK_OVERRIDE='fixture' git commit -m x >> door:git push" \
  --silent "wrote:app/notes.md::API_KEY = 'sk-1' >> door:git commit -m x"
```

The doors are `git commit`, `git push` and `gh pr create` (with `-C <dir>`
and other git options, as a segment's command); their `--dry-run` and
`--help` forms, and push's `-n`, are not doors, and nothing else is blocked
while a file is quarantined. An advise-mode rule never quarantines,
and the verifier refuses the sequence for one.
