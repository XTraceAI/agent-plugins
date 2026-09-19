---
description: Create, revise, check, or report on git-authored specs with owns frontmatter; bootstrap a repo and find weekly audit remediation PRs.
argument-hint: <init|revise|bootstrap|check|status> [file|topic]
---

Git is the only authored source of a spec. The repo brain holds read-only mirrors
and derived audit reports. Never upload a spec, maintain an artifact map, or apply
a generated audit diff locally. Humans review generated changes as GitHub PRs.

## init [file or topic]

1. Resolve the repo's spec directory from `MEMHUB_SPEC_DIR` or its configured
   Rulebook `given.repo.spec_dir` / repo spec setup. Use `docs/specs` only when no
   custom directory is configured. Honor an already agreed directory; if multiple
   configured directories make the intended owner ambiguous, ask which applies.
   Validate the directory with the bundled parser's `safe_spec_dir`. Read existing
   specs recursively with `load_specs_from_tree(repo_root, spec_dir)`, which skips
   retired specs, plus the bundled `references/feature_spec_template.md`. Reuse an
   existing owner when appropriate; otherwise write `<spec_dir>/<slug>.md`.
   Set the same `MEMHUB_SPEC_DIR` for post-edit reminders and automatic-capture
   exclusion when using a custom directory.
2. Set frontmatter `spec`, `owns` (repo-relative files or directories), and
   `last_verified_at: null`. Ask the user to confirm inferred ownership when it is
   ambiguous; use already-agreed ownership without asking again. `owns` entries
   are paths, not glob expressions. Do not invent verified behavior.
3. Stop with the local file ready for normal git review. Do not create a brain,
   upload an artifact, or write `.claude/artifact-map.json`.

## revise [file] [reason]

Read the spec and owned code, edit the spec in the same change as the implementation,
and use normal git commit/PR review. A mirror is never edited independently.

## bootstrap

Use the connected staging/production product consistently. An org admin configures
`code_insight` with `mode: feature_specs`, repo installation/full name, and a granted
workspace brain. With `domains` omitted, trigger pass 1 and show the partition
artifact. Ask which domains to confirm; never launch the expensive generation pass
without confirmed domains. Save the confirmed `{name, owns}` list and trigger pass 2
with `open_pr: true`. Link the resulting PR for human review and merge. Missing
GitHub write consent is actionable: show the run's reason and retain the candidates;
do not bypass it using the user's personal credentials. Policy operations require
a full user session; an MCP API key is not a substitute. If no supported authenticated
product surface is available, explain that configuration gap rather than inventing a tool.

## check [file]

Read the spec on disk. Compare the spec's last commit against git history for its
owned paths; report subsequent code changes as a reason to inspect consistency,
not proof of drift. Include working-tree changes. No commit yet means unverified.
Resolve the granted repo's bound brain through server repo resolution; do not mint
another brain based on a cached name. Search for `spec_audit` reports for this repo,
read the latest relevant report, and show this spec's verdict, evidence, and
remediation PR link. Never claim a truncated audit verified the omitted specs.

## status [topic]

Read local specs and search the bound repo brain for artifacts tagged `spec_mirror`.
Open pointer results before citing them. Compare the file paths and reported git
heads; explicitly say when a local file has no mirror yet. Read-only mirrors link
back to GitHub. Report retired mirrors as retired, not current governing specs.

The installed rulebook hook understands `given.repo.spec_untouched` and optional
`spec_dir` (default `docs/specs`). When owned code changes without its spec, an active
rule names the owning specs before push/PR creation. The post-edit reminder uses the
same parser. Set `MEMHUB_SPEC_DIR` for post-edit reminders in repos using a custom
directory. A rulebook admin activates the built-in proposed rule after installing a
supporting plugin; enabling spec drift does not silently activate an old rule.
