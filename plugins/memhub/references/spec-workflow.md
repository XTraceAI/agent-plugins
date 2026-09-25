# Shared spec workflow context

Read this before any spec workflow. Resolve context once and reuse it across
work, check, and maintenance; do not create separate configuration for each skill.

## Repository, source, and access

1. Establish the actual Git root, current head, worktree changes, and canonical
   remote. Use the known project; do not assume the current shell directory is
   the user's repository. No Git checkout blocks local Git operations, not
   read-only work with an explicitly selected Brain document.
2. The repository's spec configuration has one owner: **Repository → Specs** in
   MemHub Studio (Connected sources → GitHub → the repository → **Repository
   specs**). It holds the spec source (Git or Brain), spec directory, bootstrap
   executor (local agent or cloud), developer reminders (the ownership rule),
   PR checks, the scheduled drift audit, and remediation PRs. The plugin has no
   tool that reads or writes it: when you cannot see it, say which settings are
   unconfirmed and send the user there to view or change them rather than
   editing review, audit, or Rulebook resources one by one. Report conflicts
   between what you observe and what the user says is configured.
3. Git mode: resolve `spec_dir` from the repo setup / Rulebook
   `given.repo.spec_dir`, the already agreed directory, or `MEMHUB_SPEC_DIR`.
   An explicit request can select a local scope; it does not change hosted
   configuration. If these disagree, show the sources and resolve the conflict
   before writes or a claim of repository-wide coverage. Use `docs/specs` only
   when no custom directory is configured. If hosted configuration cannot be
   read, label the selected local directory as local scope, not confirmed setup.
4. Brain mode: use the configured or explicitly selected governing brain and
   document identities, scoped to the correct organization/repository. A repo
   brain containing audit reports is not automatically a governing brain. If
   the source is unknown and Git ownership specs do not establish a local scope,
   ask Git or Brain before authoring. Do not infer Brain mode from failed Git
   discovery or from a similarly named search result.
5. Git + supplementary Brain guidance keeps Git authoritative for ownership
   specs. Cite the two sources separately and surface conflicts. Do not claim
   Git hook/audit/remediation parity for a Brain-only source.
6. Discover supported tools and their schemas before calling them. Resolve the
   granted repo's bound brain through server repo resolution: `recall_directives`
   with `repo=<org>/<name>` and a one-line `task` changes nothing and lists the
   resolved brain in `scope.brains`; otherwise match the exact brain name
   `Repo: <org>/<name>`. If `scope.brains` lists more than one brain, name
   each (id and name) and ask which one governs instead of picking one; a local
   room cache entry is not a tiebreaker.
   Do not create a brain from a cached name or widen sharing. Missing auth,
   source access, or revision support is a specific capability gap. Do not
   invent endpoints or substitute personal credentials for cloud permissions.

Keep production and staging separate. A local task does not require cloud login;
continue useful local work if hosted status or reports cannot be read. Explain
which evidence is unavailable without treating it as a pass or a failure.

## Git ownership and files

From a `skills/<skill>/` directory, `../..` is the plugin root. Use its bundled
`scripts/spec_owns.py` rather than a new parser. `safe_spec_dir(value)` validates
a directory; `load_specs_from_tree(repo_root, spec_dir)` loads active specs;
`owning_specs(changed_paths, specs)` resolves ownership. Read the implementation
or function signature before constructing a call.

- `owns` contains repository-relative files or directories, not glob patterns.
- Retired specs do not govern code. A parser-skipped, malformed, oversized, or
  unreadable document is a coverage gap; an empty parsed list is not proof that
  the repository has no requirements. Inspect relevant skipped files.
- Preserve user changes. Do not overwrite existing specs or make duplicate
  owners without reading the current documents and showing the overlap.
- Resolve filesystem targets inside the repository, including symlink parents,
  before writing. A lexically safe directory alone does not prove containment.
- Use the same chosen `MEMHUB_SPEC_DIR` for plugin reminder/capture processes
  you launch. A shell export does not reconfigure an already running host or
  its hooks. Report when the host must be configured/restarted; do not claim
  hosted reviews/audits were changed by a local environment variable.
- Git specs are authored in Git. Brain mirrors and generated audit reports are
  read-only derived evidence. Do not upload a Git spec as a new authoritative
  artifact or restore the retired `.claude/artifact-map.json` workflow.

## Brain documents

Read actual governing content, not just search abstracts. Use available memory
search/read/artifact tools scoped to the selected brain; follow pagination and
truncation until the relevant requirements are read, or mark the omitted scope.
Record stable document IDs, versions when provided, and cited sections. Do not
infer file ownership from tags, titles, or similarity scores.

The supported baseline is retrieval and drafting reviewable proposals. Use an
existing authenticated revision/review capability only if it is actually exposed
and the user requested publication. Otherwise deliver a local proposed revision
with the governing document ID/version and rationale. Never replace a canonical
Brain document or create a competing source silently. A draft is not published.

## Execution and truthful status

Local spec reasoning consumes the user's coding-agent allowance or API usage.
Cloud discovery/generation uses MemHub's configured backend model account and
may consume product credits. Do not quote a price or claim credits were charged
without returned usage/billing evidence. Local mode does not disable the host's
existing session capture and does not mean offline or zero network traffic.

Reuse an explicit execution choice and confirmed domain list from the session.
An explicit `--local` or `--cloud` wins over a remembered preference. The saved
preference is the bootstrap executor in Repository → Specs, which only the
user can change in Studio; otherwise remember the choice for this conversation
and say it is not saved across sessions.
Never silently fall back from local to cloud or cloud to local.

No skill invocation activates rules, creates schedules, merges PRs, or changes
sharing by implication. A user may explicitly request those actions; then use
supported product capabilities, preserve existing resource IDs, and report the
actual result. A proposed rule is not active, a configured check is not a passed
check, a neutral "MemHub / spec drift" check titled "Not run — …" is not a pass,
and a missing report is not evidence of no drift.
