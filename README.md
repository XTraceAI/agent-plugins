# XTrace Desktop reader-source snapshot

This repository is a reader-source snapshot for XTrace Desktop's local session
reader. It adds bounded Codex conversation sections while keeping the existing
ordinary export and whole-session reader paths. It is not an installable MemHub
plugin or a plugin release. It changes no upstream main branch, release process,
marketplace listing, or plugin promotion.

The public baseline is the reader tree bundled by
[XTrace Desktop at 4593b159](https://github.com/XTraceAI/xtrace-desktop/tree/4593b159cbc24a7a2788cc9373b36b9ec450d4d7/vendor/agent-plugins).
The baseline pin reports plugin version 0.58.3; this snapshot does not announce a
new plugin version. LICENSE and NOTICE are preserved byte for byte from that
public bundle.

The complete `plugins/memhub/scripts` tree is retained so Desktop can verify its
existing reader object checks. Only source files and synthetic tests are included.
There is no inherited repository history. The test helpers in this snapshot are
small local runners, not a copy of a broader upstream test suite.

`tests/codex_history_test.py` is the exact public Desktop
[`scripts/conformance/paginated-fixture.py`](https://github.com/XTraceAI/xtrace-desktop/blob/4593b159cbc24a7a2788cc9373b36b9ec450d4d7/scripts/conformance/paginated-fixture.py),
including its fixture functions. It supplies synthetic data only. This is borrowed
public fixture data, not an independent producer fixture or independent conversion
implementation. The context tests compare bounded reads with this snapshot's
existing ordinary/full-reader paths. They do not establish behavior for every
native session.

## Verify the snapshot

With Python 3.10 or newer on a POSIX host:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugins/memhub/scripts:tests python3 tests/readers_cli_context_test.py
```

The 31 tests check canonical record parity, paginated usage reconciliation, exact
anchors and navigation, forked sessions (parity, own usage, parent refusals and a
large parent), source changes, newline framing, malformed late records, resource
bounds and pre-retention ownership checks. All source data is synthetic;
no model is invoked. The test runner gives each CLI process its synthetic home,
limits stdout to 64 MiB and stderr to 1 MiB, and stops it after 35 seconds.
The bounded reader protocol and its limits are documented in
[the context contract](tests/readers_cli_context_contract.md).

Before a Desktop dependency update:

- Review this snapshot's source and synthetic test results.
- Verify the exact snapshot commit is available at the intended public source URL.
- Verify the scripts tree and reader object IDs against the reviewed source.
- Preserve LICENSE and NOTICE and record the public fixture's original location.
- Refresh Desktop's pin and bundled files with its existing vendoring helper.
- Run Desktop's required native/UI checks and verify the affected app behavior.

This snapshot alone does not publish a plugin, update Desktop's dependency pin,
or constitute a Desktop release.
