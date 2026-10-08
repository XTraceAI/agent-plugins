<!-- create-rule's two labels, `categories` and `evidence` (ENG-1158). Read from SKILL.md steps 1b and 3; step numbers refer to it. -->

### Categories — the chips the Rules card shows and filters by

Show them in step 5's preview as `Categories: <list>` (or `none`); the person
may change, add or drop any, among the six.

`categories` is a list: usually one, two only when the rule truly spans both,
and left out when nothing fits (on a new rule; a replacement sends `[]`, see
below). Never invent a seventh, and never send the
deprecated single `category`. Put it in the step-1b body too, so the replay
echoes it back as `rule_categories` (in the server's order; an older
`coding_standards` or `testing` reads as `code_quality`, and anything else is
warned about and dropped).

| category | the rule is about |
|---|---|
| `security` | secrets, credentials, permissions, hook tampering, what subagents may touch |
| `reliability` | the agent does not break things or leave them hanging: irreversible git, deletes, production writes, destructive infra commands, calls that hang or poll with sleep, releasing DB sessions before slow external calls |
| `token_efficiency` | the cost of agent work: keep big files, diffs and listings out of context; cheap verification instead of full suites, installs and dev servers |
| `code_quality` | what the shipped code is like: correctness, test hygiene (no skipping, slowing or snapshot-updating to get green), error handling |
| `consistency` | the way this team and repo do things: conventions, reuse before reinventing, commit / lockfile / migration discipline, repo facts |
| `agent_conduct` | how the agent works with the person driving it: checks before it assumes (asks which repo, names the environment), does what was asked (answers the question, plans when told to plan), says where its claims come from |

The sorting test for `agent_conduct`: if the rule would still matter with
nobody watching the session, it belongs elsewhere.

A rule filed with `supersedes_rule_id` and no `categories` inherits the whole
set of the rule it replaces. So on a replacement, send what the person
approved in the preview: when the confirmed set is empty (`Categories: none`),
send `categories: []` — leaving the field out would file the old rule's set.
Omit `categories` on a replacement only to keep the predecessor's set on
purpose.

### Evidence — "seen in N sessions"

Put the draft's `source_ref` in `/tmp/cand.json` before the replay and read
the row in `/tmp/mine/proposals.json` carrying it, because a built-in
hypothesis can share the draft's title. Its `rule_evidence`
(`{sessions_seen, sessions_scanned, window_days}`) is filed as the rule's
`evidence` in step 5, verbatim; never pass `measured_at`, the server stamps it.

A row with no `rule_evidence` was not measured, and the rule files without
evidence — never write one by hand. That is an anchor rule, a prompt-armed
ordering, a body whose count is only a ceiling (a `given` block,
`scope_paths` / `scope_exclude_paths`, an output rule's `command_rx` /
`command_not_rx` / `content_not_rx`, an ordering's `path_rx` — filters the
replay cannot apply), or a window outside 1–365 days (`--all`).

The replay checks the pattern only: a `given` rule over-counts, an output
rule replays `content_rx` alone, and a prompt or `anchor_recall` rule always
shows 0 — report those to the person as **not measurable**, not "no lift".

**The count belongs to the engine block it replayed:** when a later step
changes the matcher or ordering (the precision check, step 3's shape, step
4's fixes), re-run the replay on the final block before filing, or file with
no `evidence`.

### Refused

`category_invalid` or `evidence_invalid` means a hand-edited field the server
will not hold — and so does a tool-argument validation error on `categories`
(a non-list or a non-string entry is refused that way, before the server's own
check). Re-file once with only that field dropped — every other field,
`source_ref` and `author` included, exactly as sent; never retry with a
guessed value. A server that does not take `categories` yet usually ignores
it (the reply carries no `categories`); if one refuses it, re-file once
without it.

A harness draft sends neither label (`references/harness.md`).
