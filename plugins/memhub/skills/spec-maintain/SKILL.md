---
name: spec-maintain
description: Use to bootstrap repository specs locally or in MemHub cloud, create or revise a spec, resolve drift into a reviewable change, or diagnose spec workflow readiness. Also handles /memhub:spec init, revise, bootstrap, resolve, status, and setup. Reads Git or explicitly selected Brain sources without treating generated mirrors as authoritative.
argument-hint: <init|revise|bootstrap|resolve|status|setup> [file|topic] [--local|--cloud]
---

# Maintain specs

Read [shared context](../../references/spec-workflow.md) first. Preserve the
user's command, configured source, directory, and decisions.

## bootstrap [--local|--cloud]

### Choose execution before discovery

If the user supplied a mode, use it. Otherwise reuse an explicit session choice
or a supported saved repository preference. With neither, ask once:
"Generate with your coding agent (recommended here; your agent usage), or in
MemHub cloud (backend usage; product credits may apply)?"

Don't start cloud discovery while waiting: discovery itself can cost money.
Ordinary local inventory needed to explain the choice is fine. Remember the
answer for this conversation; don't invent a persistence API or new configuration
file. If asked to save it, the saved choice is the bootstrap executor in Studio's
Repository → Specs, which the user sets there; this plugin cannot write it.

### Local generation

No MemHub backend generation call is needed. Use the local checkout and agent.

1. Inventory relevant code, entry points, tests, and existing specs. Respect
   ignored/private files and avoid reading secrets or generated/vendor trees.
   Show candidate domains with concrete `owns` paths, evidence paths, existing
   owners/overlaps, and gaps. Infer domain boundaries from behavior, not just
   directory names. A discovery map is not an authored specification.
2. Ask which domains and ownership to confirm before generating. If the user
   already provided an explicit domain/ownership list, reuse that authorization.
   Do not require a second confirmation for the same scope. Local generation
   still consumes the user's agent allowance; do not promise it is free.
3. Generate only confirmed domains. Reuse or revise existing specs rather than
   overwriting them or creating competing owners. In Git mode, write under the
   resolved spec directory using the template guidance below. In Brain mode,
   draft proposed governing documents for the selected brain; local Markdown
   drafts are not new canonical Git specs and do not receive Git ownership hooks.
4. Separate observed behavior from desired behavior and open decisions. Cite
   code/tests supporting observations. Do not bless current bugs as intended
   requirements or invent validation evidence. Keep `last_verified_at: null`
   for newly generated Git specs.
5. Re-read drafts and validate Git ownership with the bundled parser. Check
   path containment, overlaps, relevant skipped files, and unresolved claims.
   Return created/changed paths, confirmed scope, and uncertainties for normal
   review. A PR may be opened when requested using the user's normal Git flow;
   local mode does not require the MemHub GitHub app. Never merge automatically.

### Cloud generation

Cloud bootstrap runs in MemHub Studio, not from this agent. Repository → Specs
owns it: it configures the run, proposes domains, and takes the confirmed
`{name, owns}` choices. The plugin's personal access key
cannot call the policy API that bootstrap uses, and configuring a `code_insight`
policy or triggering passes by hand would fight the managed setup, so do none
of that. Instead send the user to Studio:

1. Connected sources → GitHub → this repository → **Repository specs**.
2. In Repository → Specs, choose Git as the source and cloud as the bootstrap
   executor, then **Open cloud bootstrap** and run **Bootstrap feature specs**.
   Review the proposed domains there before generation.

Each generated spec always lands in the repo brain as a `candidate_spec`
artifact (tags `candidate_spec`, `spec_feature`). A Git spec PR opens only when
the open-PR setting is on (off by default); without it there is no PR to look
for. A PR never overwrites a spec already in the repository: those destinations
are skipped and named in the PR body.

Name the repository and spec directory you resolved so the user can check them
against the Studio settings. If the **Repository specs** button is absent, the
feature is not enabled for their organization, or the repository has no GitHub
grant; say so rather than guessing a workaround. Cloud bootstrap produces
Git-authored spec PRs only: for a Brain source, offer local document drafts.

Afterwards, read what the run left rather than what was configured: link the
spec PR the user reports or that you can see on the repository and report its
state, or, with no PR, point at the `candidate_spec` artifacts in the repo
brain. Missing GitHub write consent is fixed in Studio, not with the user's
personal credentials. Offer local generation, but switch only on the user's
choice.

## init [file or topic]

Read related requirements and reuse an existing owner when appropriate. For a
new Git spec, read [the feature template](../spec/references/feature_spec_template.md)
and write `<spec_dir>/<slug>.md` with `spec`, concrete `owns`, and
`last_verified_at: null`. Scale sections to the feature; do not invent database,
API, security, or review details just to fill the template. Confirm ambiguous
ownership; reuse already agreed ownership. Keep unresolved decisions explicit.

For Brain, read the current governing documents and draft a new document or a
revision proposal with source identity and rationale. Publication follows the
shared Brain rules; a local draft does not silently establish a second authority.

## revise [file or document] [reason]

Read the current spec, relevant code, and the reason for changing intended
behavior. Preserve unrelated user edits. Update the existing Git spec in the same
change as implementation, or prepare a Brain revision against the exact document
identity/version. Re-read the latest Brain version before an authorized publication
so a concurrent revision is not silently overwritten. Missing revision support
leaves a reviewable proposal, not a claimed successful write.

Explain which requirements changed and why. Keep generated mirrors read-only.
Clear `last_verified_at` to `null` on a changed Git spec because previous verification
no longer covers the new text. A local check does not certify a merged commit.

## resolve [finding]

Read the cited requirement, implementation, and finding; reproduce or freshly
check the discrepancy using [Check specs](../spec-check/SKILL.md). An audit may
be stale, incomplete, or wrong.

Determine the intended behavior from explicit user decisions and authoritative
requirements. If unclear, present the concrete alternatives and ask whether to
fix code or revise the requirement before making the dependent change. Don't
rewrite specs simply to make a drift check green. When a decision is already
clear, implement it without asking again. Run relevant checks and present the
code/spec diff or Brain proposal for review. Cloud-generated remediation stays
in its existing PR; do not blindly apply the audit diff locally or create a
second competing remediation PR. A `dismissed` remediation PR was closed
unmerged by a reviewer: treat that as their decision, not as drift to re-raise,
unless the user asks to revisit it.

## status [topic] / setup

Read configuration and readiness; `setup` diagnoses before proposing changes.
Start with `get_spec_workflow(repo="<owner>/<name>")`, taking the name from the
`origin` remote. It returns what Repository → Specs holds: `settings` (source,
directory or brain, reminders, PR checks, audit schedule, `only_stale`, remediation
PRs, bootstrap executor), each resource with its status and teamspace, `managed`
(false means nobody has saved the workflow and the settings are inferred),
`report_brain_id`, `last_audit`, `reminder_rule`, `warnings` and `conflicts`.
If the tool is unavailable (an older server) or refuses, say so and fall back to
`list_rules(repo=…)` for the ownership rule; everything else is then unknown.
Report the source and directory/brain, agent/plugin connection, ownership rule
state, PR reviewer, audit schedule, and delivery mode only where evidence is
available. Identify conflicting settings with their origins. Separate configured,
observed working, missing, and unknown. Don't install or activate everything as
a side effect of a status request.

- A non-empty `reminder_rule.behind_builtin` means the stored rule predates the
  current built-in (for example, its text still shows a literal `<spec>`). Say so,
  and say that an org admin re-saving Repository → Specs refreshes it.
- A `last_audit` with status `failed` and `not_audited` > 0 is an incomplete run
  the next one resumes, not a verdict. Report its counts and `error` as such.
- In Git mode, list every `*.md` directly under the spec directory that has no
  `owns:` frontmatter, by name. No check, audit or reminder reads those files.
  Don't leave this to chance: compare the directory listing with the parsed specs.

In Git mode, compare local paths/heads with the mirrors in the resolved repo
brain: artifacts tagged `spec_mirror` (retired ones also `spec_retired`), named
`Spec: <name>`, with rationale `Mirror <repo>@<sha12>` giving the mirrored head.
MCP does not return their metadata, so read the head from the rationale and the
spec's frontmatter from its content. Open pointer results before citing them;
report absent or retired mirrors accurately. Read relevant `spec_audit` reports for verdicts, recorded
head, checked/omitted coverage, and remediation PR links. They are in
`report_brain_id` from `get_spec_workflow`, which is often NOT the repo brain:
search that brain (`search_memory(agent_brain_id=<report_brain_id>, memory_type="artifacts")`),
or open `last_audit.artifact_id` directly with `get_artifact`. Finding none in the
repo brain does not mean there are none. Brain mode reports
versions and retrieval access to the selected governing documents, not Git mirror
health. Do not change brain membership or create another brain to repair access.

The ownership rule uses `given.repo.spec_untouched` and optional `spec_dir`.
Repository → Specs in Studio (Connected sources → GitHub → the repository →
**Repository specs**) is the single place that configures the spec source and
directory, bootstrap executor, developer reminders (this rule), PR checks, the
scheduled drift audit, and remediation PRs. Saving those settings needs an org
admin. `setup` changes go there: tell the user what to set and where (and that
an admin must save them), and don't create or edit rules, policies, or
routines one by one to reproduce it. This skill installs and activates nothing.
