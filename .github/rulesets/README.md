# ENG-1063 enforcement

The ruleset is **disabled during testing**. The existing `guard` check continues
to verify version/manifest parity and the full plugin suite on PRs and report
failures, but is not required for merging.

When testing is complete, explicitly re-enable `eng-1063-release-checks.json`.
It requires a PR and the GitHub Actions-published `guard` check on an up-to-date
main base, with no bypass actors. It also blocks force pushes and deletion.
No new human-approval count is introduced here.

The disabled template also includes `Production plugin readiness`. Its workflow
is advisory until explicitly configured and enabled: see
[production release checks](../../contracts/PRODUCTION-RELEASE.md). Keep both
the ruleset disabled and `MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED` unset during setup.
Enabling only one control does not establish enforcement. Do not label an advisory
workflow completion or the existing guard as proof of production compatibility.
