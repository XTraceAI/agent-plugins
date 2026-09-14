# ENG-1063 enforcement

The ruleset is **disabled during testing**. The existing `guard` check continues
to verify version/manifest parity and the full plugin suite on PRs and report
failures, but is not required for merging.

When testing is complete, explicitly re-enable `eng-1063-release-checks.json`.
It requires a PR and the GitHub Actions-published `guard` check on an up-to-date
main base, with no bypass actors. It also blocks force pushes and deletion.
No new human-approval count is introduced here.

This is not yet the live-production compatibility gate. Add that distinct check
only after its workflow, production test account and deployment evidence are
configured and a real run passes. Do not label the existing guard as proof of
production compatibility.
