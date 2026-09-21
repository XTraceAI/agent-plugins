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
file. If asked to save it and no supported setting exists, explain that limitation.

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

The currently documented backend bootstrap produces Git-authored spec PRs.
For Brain-only bootstrap, offer local document drafts or require a discovered
supported cloud capability; do not send a Git generation request and pretend it
creates governing Brain documents.

Use the connected staging/production product consistently. An org admin configures
`code_insight` with `mode: feature_specs`, the correct repo installation/full name,
configured spec directory, and granted workspace brain. Discover and validate the
actual configuration schema before writing; don't assume field names from prose.
With `domains` omitted, trigger pass 1 and show the partition artifact. Obtain or
reuse confirmed `{name, owns}` choices, then trigger pass 2 with `open_pr: true`.
Link the returned PR and report actual run state and available usage evidence.

Policy operations require a full user session; an MCP API key is not a substitute.
Missing GitHub write consent is actionable: retain the candidates and report the
run's reason. Do not bypass cloud app permissions using the user's personal
credentials. If no supported authenticated surface is available, explain the
specific gap. Offer local generation but switch only on the user's choice.

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
second competing remediation PR.

## status [topic] / setup

Read configuration and readiness; `setup` diagnoses before proposing changes.
Report the source and directory/brain, agent/plugin connection, ownership rule
state, PR reviewer, audit schedule, and delivery mode only where evidence is
available. Identify conflicting settings with their origins. Separate configured,
observed working, missing, and unknown. Don't install or activate everything as
a side effect of a status request.

In Git mode, compare local paths/heads with `spec_mirror` artifacts in the resolved
repo brain. Open pointer results before citing them; report absent or retired
mirrors accurately. Read relevant `spec_audit` reports for verdicts, recorded
head, checked/omitted coverage, and remediation PR links. Brain mode reports
versions and retrieval access to the selected governing documents, not Git mirror
health. Do not change brain membership or create another brain to repair access.

The ownership rule uses `given.repo.spec_untouched` and optional `spec_dir`.
A proposed rule needs explicit activation through the product; enabling spec
drift doesn't silently activate it. Hooks, audits, and PR review remain separate
resources until a connected product exposes unified management. Link the actual
management surface if available rather than pretending this skill installed it.
