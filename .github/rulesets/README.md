# ENG-1063 enforcement

The ruleset is **active** on `main` as of 2026-09-15 (ENG-1077). It requires a
PR and, on an up-to-date `main` base, the GitHub Actions-published `guard` and
`Production plugin readiness` checks, with no bypass actors. It also blocks
force pushes and deletion. No human-approval count is introduced here, and
review-thread resolution is not required: the review bots file many threads per
PR and resolving them wholesale was a ritual, not a review.

The release owner's gate is the `production-plugin-release` environment, whose
required reviewer is the release owner. `Production plugin readiness` needs the
`production` job, and that job needs the environment approval, so no PR to
`main` can turn green until the release owner approves its run on the exact
bytes that will merge. Merging to `main` is the production release on the
unpinned channels (see [RELEASING.md](../../RELEASING.md)). The three real-agent
jobs share that environment but are advisory: they run when the release owner
approves, and never gate the merge.

`eng-1063-release-checks.json` is the definition of the live ruleset
(id 23353368). Change the file and the live ruleset together:

```bash
gh api -X PUT repos/XTraceAI/agent-plugins/rulesets/23353368 \
  --input .github/rulesets/eng-1063-release-checks.json
gh api repos/XTraceAI/agent-plugins/rules/branches/main   # verify the effective rules
```
