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
let me", "never let it happen again" — and never on your own initiative. Two
things bound it:

- **Only a call the hook sees BEFORE it runs can be stopped.** A `bash`
  matcher, an `edit` matcher on an Edit/Write call (matched against the content
  about to be written — a file a Bash command wrote is found only afterwards,
  so there the same rule advises), a `read` matcher (the Read tool's call, or the Bash command that
  would print the file) or an `ordering` can block. `output` fires after the
  command already ran, and notes and anchors are advice by construction —
  the server refuses `gate` on those. A blocked Read has no prefix to carry
  a reason: the deny tells the agent to read narrower (`offset`/`limit`),
  delegate to a subagent, or run `RULEBOOK_OVERRIDE='<why>' cat <path>` in
  Bash, which records the override like any other.
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

`repo` keys: `branch_rx`, `branch_not_rx`, `diff_lines_gt`, `diff_files_gt`,
`diff_paths_rx`, `diff_paths_none_rx` (the branch's changes against its base,
working tree and untracked files included), `dirty`, and `spec_untouched`
(`true`: the diff changes files a spec under `spec_dir`, default `docs/specs`,
owns without changing that spec). `user` keys: `said_rx`,
`not_said_rx` (what the person typed this session — never a tool result or
injected context). `file` keys (read rules only): `lines_gt`, `bytes_gt` —
what the call would pull into the context, so a Read with `offset`/`limit`
or a `head -50` counts only those lines. `agent` keys (any event): `main`
(`true` = the main agent, `false` = a subagent). The verifier refuses an
unknown key (LOAD fails); a hook that meets one runs the rule as advice, and
a value of the wrong kind drops the rule.
