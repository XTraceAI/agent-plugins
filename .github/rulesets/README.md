# ENG-1063 enforcement

`eng-1063-release-checks.json` requires a PR and the GitHub Actions-published
`guard` check on an up-to-date main base, with no bypass actors. It also blocks
force pushes and deletion. The existing guard verifies version/manifest parity
and the full plugin suite; this makes it mandatory rather than optional.

This is not yet the live-production compatibility gate. Add that distinct check
only after its workflow, production test account and deployment evidence are
configured and a real run passes. Do not label the existing guard as proof of
production compatibility. No new human-approval count is introduced here.
