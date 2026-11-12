<!-- create-rule step 3: gates and `given` facts. Read from SKILL.md. -->

**Advise, or stop the command?** A rule advises by default: its sentence is
shown and the call goes through. Pass `mode: "gate"` and the rule DENIES a
matching command before it runs — the person can still run that exact command
by prefixing `RULEBOOK_OVERRIDE='<why>'`, and their reason is recorded with the
fire. A blocked edit has no command to prefix: it goes through with a
`rulebook-override[<rule>]: <why>` marker in the content being written, naming
the rule (an unnamed marker excuses nothing), and the marker stays in the diff. Advice has the same channel, one call later: an agent that reads an
advisory and goes on without it says why on its next command as
`RULEBOOK_OVERRIDE='[<label>] <why>'`, naming the rule, and the reason lands on
that rule's fire. A rule with a `converted_rx` (a `matcher` key, matched
against the Bash commands the agent runs after the fire) also records "not
followed" on its own: a fire whose conversion has not been seen two turns later is closed
`converted=false`, so a rule nobody acts on shows it instead of showing
nothing. The hook only reports what it saw — the command that converted, the
override that set a rule aside, each turn ending — and the server decides the
outcome from those facts (the earliest one after the fire wins).

Ask for it when the user's own words ask for it — "block", "stop me", "don't
let me", "never let it happen again" — and never on your own initiative. Three
things bound it:

- **Only a call the hook sees BEFORE it runs can be stopped.** A `bash`
  matcher, an `edit` matcher on an Edit/Write call (matched against the content
  about to be written — a file a Bash command wrote is found only afterwards;
  there the same rule advises and QUARANTINES the file, below), a `read` matcher (the Read tool's call, or the Bash command that
  would print the file) or an `ordering` can block. `output` fires after the
  command already ran, a `prompt` matcher after the prompt was sent, and notes
  and anchors are advice by construction — the server refuses `gate` on those.
  A `reply` matcher is the one gate that stops no call: it blocks the agent's
  Stop, so the agent goes on to back its claim with the command or retract it,
  and the same turn is never blocked twice. A blocked Read has no prefix to carry
  a reason: the deny tells the agent to read narrower (`offset`/`limit`),
  delegate to a subagent, or run `RULEBOOK_OVERRIDE='<why>' cat <path>` in
  Bash, which records the override like any other.
- **A gate that matches too late quarantines instead.** A Bash command that
  writes a file the command line does not show (`python fix.py`, a
  `write_text()` in a heredoc, a generator) is found by the post-call scan of
  the working tree, after the write, when there is nothing left to refuse. A
  GATE-mode `edit` rule that matches it then opens a violation for that file
  in that session. Until the file no longer matches — fixed, reverted to
  HEAD, or deleted; re-checked whenever the agent writes it again and before
  every block — `git commit`, `git push` and `gh pr create` in that checkout
  are blocked naming the rule and the file (also under `git -C "<dir>"`, in
  `if …; then`, loops and `{ … }`; a `--dry-run` or `--help` is no door; a
  door run under a wrapper command such as `timeout 60 git push` or by a
  script is not seen), and the agent's Stop is blocked once per turn (never
  on the continuation the block itself causes) and advised after that. A
  turn is Claude Code's latest prompt in the transcript, or Codex's
  `turn_id`; a Stop with neither blocks at most once per violation per 10
  minutes. `RULEBOOK_OVERRIDE='<why>'` on the blocked command, or
  `RULEBOOK_OVERRIDE='[<rule>] <why>'` on any shell command, lifts it and is
  recorded like any other override. Nothing to author: every gate-mode edit
  rule does this, on hooks from 0.137.0 on (an older hook advises, as
  before). Advise-mode rules never quarantine. It works on Claude
  Code; on Codex the bridge relays the same Stop block, which is not yet
  tested in a live Codex session; Cursor runs the post-call scan but has no Stop block to
  quarantine with, so there a late match is never held back. Prove it with the verifier's quarantine sequences
  (verify-recipes.md).
- **It stops everyone the rule applies to, not just the author.** Say that
  before filing, in those words. A blocking rule with a loose `command_rx` is
  the worst failure this skill can ship: it stops work, and the person it stops
  did not write it.

Filing it is not arming it — see step 5. `mode` is the rule's own field, so a
blocking rule needs the same `command_not_rx` exemptions and the same
`--fires` / `--silent` proof as any other; if anything, prove it harder.

**`given` — facts the call must also satisfy.** A matcher rule may carry a
`given` block inside its `matcher`; the regex is checked first, then these,
and the hook answers them from read-only git and the local transcript, once
per call. A fact it cannot establish never satisfies a predicate, so the rule
stays silent rather than firing on a guess.

Examples:

- never push to main — on a `git push` matcher:
  `{"repo": {"branch_rx": "^(main|master)$"}}`
- a PR that changes source needs a test — on a `gh pr create` matcher:
  `{"repo": {"diff_paths_rx": "^src/", "diff_paths_none_rx": "(^|/)tests?/"}}`
- keep PRs under 500 lines: `{"repo": {"diff_lines_gt": 500}}`
- don't commit unless asked — on a `git commit` matcher:
  `{"user": {"not_said_rx": "\\b(commit|push|ship)\\b"}}`
- don't pull a whole big file into the main context — on a `read` matcher:
  `{"file": {"lines_gt": 350}, "agent": {"main": true}}` (a subagent's reads
  pass: delegation is the way past the rule)
- subagents may not push — on a `git push` matcher: `{"agent": {"main": false}}`
- once the context is large, delegate searches — on an `rg` matcher:
  `{"session": {"context_tokens_gt": 300000}}`
- the third identical test run in a session — on a `pytest` matcher:
  `{"session": {"repeat_count_gt": 2, "repeat_key": "command"}}`
- after a long pause the prompt cache is cold — on a `prompt` matcher:
  `{"session": {"idle_seconds_gt": 300}}`

`repo` keys: `branch_rx`, `branch_not_rx`, `diff_lines_gt`, `diff_files_gt`,
`diff_paths_rx`, `diff_paths_none_rx` (the branch's changes against its base,
working tree and untracked files included), `dirty`, and `spec_untouched`
(`true`: the diff changes files a spec under `spec_dir`, default `docs/specs`,
owns without changing that spec). `user` keys: `said_rx`,
`not_said_rx` (what the person typed this session — never a tool result or
injected context). `file` keys (read rules only): `lines_gt`, `bytes_gt` —
what the call would pull into the context, so a Read with `offset`/`limit`
or a `head -50` counts only those lines. `agent` keys (any event): `main`
(`true` = the main agent, `false` = a subagent).

`session` keys (any matcher event: bash, edit, read, output, prompt) describe the agent session, not the
repo:

- `context_tokens_gt` — tokens the model was sent on its latest response
  (input + cache-creation + cache-read, read off the transcript, each
  response counted once);
- `idle_seconds_gt` — the quiet before the latest response (at a prompt:
  since the last response). Past ~300 s a prompt cache has expired;
- `turn_tool_calls_gt` — tool calls since the person's last prompt, the call
  being checked included (0 at a prompt, so not for prompt rules);
- `repeat_count_gt` with `repeat_key` — this rule has matched (pattern and
  every other `given` fact) on more than N tool calls (or prompts) this
  session, this one included, counted per `rule` (default: any match),
  `command` (the same Bash command — `bash` rules) or `file` (the same path —
  `edit`/`read` rules); a prompt rule counts per `rule` only. One call counts
  once, even when a Bash write is seen both before and after it runs.
  Counted on this machine.

Inside a subagent, the context, idle and tool-call facts are the
subagent's own (read from its own transcript), never the main agent's; when
its transcript cannot be found they are unknown and the rule stays silent.
The repeat count is the whole session's, subagents included.

Where they can be answered: Claude Code — all of them. Codex —
`context_tokens_gt`, `turn_tool_calls_gt` and `repeat_count_gt`; idleness is
unknown. Cursor — `repeat_count_gt` only; its transcript carries no usage or
timestamps. An unknown fact never holds, so on those hosts the rule is
silent, not wrong. The server sets the rule's `min_hook_version` itself, and
a `given.session` rule is withheld from an older plugin entirely: unlike
the keys below, it never runs there as advice, since without the session
facts it would fire on every matching call. Numbers are whole and bounded
(context ≤ 10,000,000, idle ≤ 2,592,000 s, counts ≤ 100,000).

The verifier refuses an
unknown key (LOAD fails); a hook that meets one runs the rule as advice, and
a value of the wrong kind drops the rule.

**A rule on the agent's final reply — `event: "reply"`.** Some mistakes are a
sentence, not a command: "merged", "tests pass", "deployed", "fixed" said by
an agent that never ran anything to see it. A `reply` matcher is checked at
Stop against the turn's last assistant text:

```json
{"event": "reply",
 "reply_rx": "\\b(merged|tests? pass(?:es|ing)?|deployed|fixed)\\b",
 "evidence_command_rx": "\\bgh\\s+pr\\s+(view|merge)|pytest|npm\\s+test"}
```

- `reply_rx` (required) is the claim, case-insensitive. Text the agent QUOTED
  is never a claim: fenced and 4-space-indented code blocks, `inline code`,
  `> ` lines and `"…"` / `'…'` spans are blanked first, so "I did not run `pytest`" or a pasted log line
  fires nothing.
- `evidence_command_rx` (optional) is the shell command that would back it.
  The rule fires when no shell command of the SAME turn matched it and
  succeeded. A turn opens at the person's message, a slash command (each
  `/loop` tick) or a task notification the agent was idle for, so an earlier
  turn's run never backs a later claim. A red run (non-zero exit) is no
  evidence, a quoted mention
  (`echo "pytest"`) or a heredoc body is not a run, and the match is
  case-sensitive like an ordering receipt's. Without it, the claim alone fires
  — for wording the team never wants ("should work now").
- `mode: "gate"` blocks the Stop with the rule's statement as the reason, so
  the agent goes on to run the command or retract the claim; the same turn is
  never blocked again (a later unbacked claim in it advises). When the
  transcript does not show where the turn began, it advises instead. Write the
  statement as what to DO, since it is the whole instruction. `advise` shows
  the person at once and tells the agent at the next prompt. A gate blocks in
  every person turn that ends on an unbacked claim; an advisory fires once per
  session unless `warn_once_per: "turn"`.
- `given` takes `agent` and `repo` only; `scope_paths` is refused (a reply
  has no path). `min_hook_version` is set to 0.137.0 for you. Claude Code runs
  it; the Codex bridge runs it too, but Codex honouring the Stop block is not
  yet verified live; Cursor does not run it. The rule judge is NOT asked about
  a reply fire, so `when_not` does not narrow it: the patterns are the whole
  check. Keep `reply_rx` to claims, not words a hedge also uses.
