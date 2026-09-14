# Backend compatibility (ENG-1063)

`rulebook.lock.json` pins the backend-owned fixture contract by commit and
SHA-256. The pinned commit must be available in the backend checkout; fetch it
before testing. Backend PRs that change the contract require a reviewed pin
update here. A matching pin is necessary but does not prove compatibility.

Run the actual implementation test from a backend checkout with its test
dependencies installed:

```sh
MEMHUB_CONTRACT_PLUGIN_ROOT=/absolute/path/to/agent-plugins \
  python -m pytest tests/test_plugin_rulebook_contract.py -q
```

The backend seeds fixtures and invokes `scripts/check-rulebook-contract.py`.
That probe runs this checkout's real hook request builder, HTTP transport,
parser, and cache against a loopback bridge to the real FastAPI app. Fixture
authentication and temporary state replace personal credentials/cache. The
probe requires its bundled version and rejects environment version overrides.
It fails on rejected queries, empty/malformed responses, wrong scope/modes,
or broken ETag revalidation. Its failure tests run in the normal plugin suite.

The script only permits loopback HTTP origins. Production smoke testing,
serving-revision evidence, required release checks and global version-floor
enforcement are subsequent ENG-1063 work. These tests do not certify historical
plugin binaries, production deployment, or host-to-agent upgrade messages.

The backend contract commit must be published before merging this pin. Do not
squash away the referenced commit without updating the pin to a reachable one.
