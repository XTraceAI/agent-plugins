# Reading `create_rule`'s reply

Step 5 of SKILL.md files the rule; this is what each answer means.

- `unchanged: true` → the rule is already in the book (a retried call);
  nothing new is filed, though differing `when` / `do` / `why` / `when_not`
  and `evidence` are written onto the existing row. On a retry, reuse the
  first filing's `source_ref` byte for byte (only an `@<sha>` and a `#…` tail
  are ignored), or name its `rule_id` in `supersedes_rule_id`.
- **Refused as a duplicate** (`This rulebook already has a rule with the same
  title|trigger: "<title>" (rule_id <id>, <status>)`) → nothing was written.
  If it is the same rule, file again with `supersedes_rule_id: <id>`; if it is
  a different rule, retitle it (same title) or tell the user (same trigger).
- `status: "proposed"` + `supersedes_rule_id` → filed as a replacement for
  the rule you named; it retires that rule when a reviewer activates it.
- `status: "proposed"` with no `supersedes_rule_id` → new, awaiting review.
- `category_invalid` / `evidence_invalid` → labels.md, *Refused*.
- A scope refusal (`This organisation's plan doesn't include workspaces…`,
  `That workspace is locked…`, `No workspace with that id…`, `That's the
  organisation's main workspace…`) → nothing was written. Tell the user in
  one line and file again with the scope step 0 rule 3 or 4 gives. `A replacement stays in the
  same scope…` → file it again without `scope`. `A personal rulebook only
  takes its owner's own rules…` → the rule you meant to replace is someone
  else's personal rule: tell the user, and file it only as a new rule (no
  `supersedes_rule_id`) if they want one.
