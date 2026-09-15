# ENG-1063 enforcement

The ruleset is **active** on `main` (id 23353368, since 2026-09-15, ENG-1077).
It requires a PR and, on an up-to-date `main` base, three GitHub Actions
checks, with no bypass actors; it also blocks force pushes and deletion.
No human-approval count is introduced, and review-thread resolution is not
required: the review bots file many threads per PR and resolving them
wholesale was a ritual, not a review.

| Required check | Workflow | Runs |
| --- | --- | --- |
| `guard` | bump-guard.yml | every push: version bump, manifest parity, test registration |
| `Production plugin readiness` | production-compatibility.yml | every push: all suites, native install/upgrade and SessionStart delivery on the pinned host CLIs, the candidate bytes against live production |
| `Real agent evidence` | real-agent-evidence.yml | **on demand**: real host CLIs driving real models against production |

The release flow this produces: a developer prepares a PR into `main`; the
first two checks run on every push. When the PR is done being refined, the
developer runs the real-agent workflow on the branch:

```bash
gh workflow run real-agent-evidence.yml --ref <branch>
```

Until that run exists for the PR's head commit the third check is absent
("expected") and the merge is blocked; when it passes, the developer merges.
A further push makes a new head with no evidence, so the run is repeated. The
`production-plugin-release` environment holds the production test credentials
and has no required reviewers: CI, not a person, is the release gate. Merging
to `main` is the production release on the unpinned channels
(see [RELEASING.md](../../RELEASING.md)); promoting the Claude marketplace pin
is a separate, deliberate PR.

`eng-1063-release-checks.json` is the definition of the live ruleset. Change
the file and the live ruleset together:

```bash
gh api -X PUT repos/XTraceAI/agent-plugins/rulesets/23353368 \
  --input .github/rulesets/eng-1063-release-checks.json
gh api repos/XTraceAI/agent-plugins/rules/branches/main   # verify the effective rules
```

Bootstrapping note: a `workflow_dispatch` workflow can only be run once it
exists on the default branch, so `Real agent evidence` is added to the live
ruleset after the PR that introduces real-agent-evidence.yml has merged.
