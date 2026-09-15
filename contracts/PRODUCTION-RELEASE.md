# Production plugin compatibility (ENG-1063)

This is **advisory by default**, while the internal staging repository and test
identities are being set up. No branch protection is activated by these files.
An advisory workflow completion is not a production compatibility certificate:
read its summary and the `ok` field in the probe's report.

`XTraceAI/agent-plugins-internal` is the intended staging/development repository;
`XTraceAI/agent-plugins` is production distribution. Plugin releases are independent
of the Wednesday backend cadence. The internal repository's existing cutover plan
still applies; this change does not switch development or publish a plugin.

## What runs

The public `production-compatibility.yml` runs offline probe failure tests on PRs
to main. Once live checks are enabled, it tests both the PR's merged main package
(Codex/Cursor) and the exact commit/tag selected by the Claude marketplace. A
missing tag, wrong SHA, or non-production source is a failure. Main changes and
Claude pin changes use the same workflow, with no path filter or label exemption.
Merge-group events are also supported. Fork PRs never receive production secrets;
a skipped live check is not a compatibility pass.

The internal promotion workflow uses an immutable revision of the public probe
against its exported production package before uploading the export. It records
the source commit and digest of the actual exported files, including executable
bits. Never rebuild or edit that package after testing. The existing full plugin
suites and export/credential scan still run. Opening promotion PRs automatically
still requires the GitHub App from the internal cutover plan.

The probe performs two read-only requests with the candidate's actual request
builder, HTTP transport, response parser and cache: cold 200, then conditional
304. It requires the exact fixture rule IDs/modes and an ETag. Empty rules, bad
credentials, 426 upgrade rejection, redirects, changed response shapes, outages,
and stale caches cannot produce `ok: true`. It never provisions or repairs fixtures,
uses saved credentials, or changes the production version floor. Auth and cache
locations are injected for isolation; this does not exercise the host login flow.

Reports include plugin version, source commit, package digest, fixture digest,
production origin and time; they exclude tokens, rule content and account IDs.
The check certifies the observed endpoint behavior at that time, **not the backend
deployment SHA or every serving replica**. It does not yet test MCP sessions,
capture, host hook delivery, or every plugin API. Those remain separate release
checks. A later backend deployment can invalidate earlier compatibility evidence;
rerun immediately before merging a release. Backend CI must still protect already
supported plugin versions.

## Setup, then opt in

In each repository that runs the live probe, configure the GitHub environment
`production-plugin-release` for trusted release code:

* Secret `MEMHUB_PROD_READ_TOKEN`: raw bearer token for a dedicated ordinary user
  with `memory:read`, belonging only to the production E2E org. No personal/admin
  account and no staging credential. The token is not a JSON access-key file.
* Environment variable `MEMHUB_PROD_FIXTURE_JSON`: the provisioned fixture:

  ```json
  {
    "schema_version": 1,
    "org_id": "<production test org UUID>",
    "repo": "memhub-production-compatibility",
    "rules": [{"id": "<bound active rule UUID>", "mode": "gate"}]
  }
  ```

Choose stable rules supported by both current main and the pinned Claude release.
The list must be nonempty and match all rules bound to this identity/repo. Include
unbound/out-of-repo rules in fixture provisioning to exercise filtering. Fixture
changes are maintained separately, never auto-repaired by a release test.

1. Leave repository variables unset during setup. Live checks do not run, and
   summaries say NOT VERIFIED. New checks do not block merges or internal export.
2. Set repository variable `MEMHUB_PLUGIN_PROD_CHECKS_ENABLED=true` to collect live
   results. Failures remain advisory: internal exports are marked UNVERIFIED and
   public checks are not required. No automatic publication is added.
3. After a real success and a deliberate failure test, explicitly set repository
   variable `MEMHUB_PLUGIN_RELEASE_GATE_ENFORCED=true` in both repos. Internal
   export then stops unless production verification succeeded. On public, also
   activate the checked-in main ruleset requiring `guard` and `Production plugin
   readiness`. The variable alone cannot prevent a public merge. Do not enable
   either enforcement control as part of initial setup.

Public live jobs run only reviewed same-repository candidates, with a read-only
GitHub token and no checkout credentials. Configure environment reviewers if the
repository's contributor trust model needs them. Do not use `pull_request_target`
to execute fork code with production secrets. Keep private host transcripts out of
public workflow artifacts.

## Local verification

Run `python3 tests/production_compatibility_test.py` (offline). To run the real
probe, supply the two configuration variables above through a secure environment:

```sh
python3 scripts/check-production-compatibility.py \
  --plugin-root plugins/memhub --source-sha "$(git rev-parse HEAD)" \
  --report /tmp/memhub-production-report.json
```

There is no arbitrary backend URL flag. Missing configuration fails the probe;
advisory behavior belongs to workflow orchestration, never a fake successful probe.
