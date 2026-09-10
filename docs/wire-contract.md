# Native reader stream

`python3 plugins/memhub/scripts/readers_cli.py --host codex|cursor` reads native
session files and emits JSON Lines on stdout. It uses the same canonical record
normalizers as capture hooks. It does not send an import, create a capture
receipt, or change plugin capture cursors. Python 3.10 or later is required; the
MCP SDK is not required.

Each session starts with one header, followed by its canonical records until
the next header or end of stream:

```json
{"type":"session","host":"codex","native_session_id":"example-session","conversation_id":"codex-example-session","source_surface":"codex_cli","started_at":"2026-01-01T00:00:00Z","cwd":"/synthetic/project","git_branch":"example-branch","title":null,"path":"/synthetic/rollout.jsonl","mtime":1767225600.0}
```

| Field | Meaning |
| --- | --- |
| `host` | `codex` or `cursor`. |
| `native_session_id` | Required native identity; absent identity makes the session unreadable. |
| `conversation_id` | `<host>-<native_session_id>`, matching hook imports. |
| `source_surface` | Raw Codex originator or Cursor store metadata when supplied. Recognized Cursor IDE transcript locations use `cursor-ide`. Legacy stores and arbitrary transcript paths without surface evidence remain `null`. Unknown nonempty values are preserved. |
| `started_at` | Native session start, retaining the original Codex timestamp precision; `null` when unavailable. A first message timestamp or file modification time is not substituted. |
| `cwd`, `git_branch` | Native structural metadata or `null`. |
| `title` | Existing reader title in full mode; always `null` in metadata-only mode. |
| `path` | Native source path on this machine. |
| `mtime` | Unix seconds of the most recent source observation: the native file, or an existing Cursor journal, metadata sidecar or saved hook-pin file. |

Canonical records retain their existing UUIDs, message blocks and usage rules.
Cursor records also restore saved hook usage and timestamp pins through the
existing read-only helper. Missing usage remains missing. Restored pins are
observations, not proof that this reader performed a hook capture.

`--metadata-only` emits headers without records or prompt-derived titles. The
headers still contain structural information such as local paths and branches.
`--since <timestamp-with-timezone>` includes sessions whose `mtime` is at least
that instant. It is a modification-time prefilter; consumers apply their own
event-time selection. `--session <native-id-or-path>` selects one session using
the existing host reader's locator instead of enumerating all sessions.

Exit code `0` means enumeration completed, including a legitimately empty
existing store or a filter matching no sessions. Exit code `2` means arguments
are invalid or coverage is incomplete. Missing roots, unreadable sessions,
unsupported metadata, and source changes during reading do not count as a
healthy empty result. Healthy peer sessions can still be emitted. Consumers
must inspect the exit code before reporting complete coverage. A missing
Python executable is a launcher error, not an empty reader result.

Runtime diagnostics use stderr JSON Lines with `type: "diagnostic"`, `host`,
`code`, and a nullable local `path`. Codes are `discovery_incomplete`,
`session_unavailable`, `session_unreadable`, and `source_changed`; diagnostics
do not include transcript excerpts or raw parser exceptions. Argument errors
use argparse's ordinary stderr help. Symlinked subdirectories are skipped and
reported as incomplete to avoid traversal cycles. A configured root symlink
remains supported. A session is checked for source changes and JSON encoding
errors before its header is emitted; stdout delivery errors still require
the consumer to discard its incomplete stream.

Full reads reject invalid UTF-8 and malformed complete JSON rows instead of
silently replacing or skipping content. An unfinished final JSON row can wait
for a later read. Enumeration excludes all candidates with duplicate native
identities before emitting any of them; an explicit path can select one.
Codex's title index participates in source revisions and `--since`, so a title
assignment or rename is observable even when its rollout has not changed.
The existing capture
readers retain their tolerant defaults. Cursor SQLite files and journals are
copied into a private, temporary snapshot before opening SQLite; the native
directory is never used for shared-memory files or journal recovery. A hot
rollback journal is recovered with writes allowed only in the private copy
before the existing read-only normalizer runs. Source
revisions are compared across the read, and changed sources are reported as
incomplete. Numeric overflow in native metadata rejects that session while
preserving healthy peers.
Existing malformed Cursor observation files or invalid saved timestamp/usage
values also report incomplete coverage. Missing files and explicit unmeasured
timestamp pins remain optional. Full reads validate the Codex title-index text
when that fallback is needed; ordinary capture retains its tolerant defaults.

## Verification

Run `python3 tests/readers_cli_test.py` for real CLI output against synthetic
Codex and Cursor fixtures. Goldens fix session headers and canonical records;
the same records are compared with the existing readers. Additional cases
cover raw/unknown surfaces, precise or absent native starts, metadata-only
content exclusion, saved pin restoration without state writes, WAL and pin
modification filtering, malformed peers, incomplete discovery, source changes,
encoding failures, and SQLite paths containing URI characters.

`bash scripts/check-plugin.sh` runs every discovered suite plus the flush shell
tests under isolated bare Python and MCP SDK environments. Existing reader,
Cursor usage and timestamp suites continue to cover cumulative usage,
deduplication and unobserved usage. No network or MCP import is needed by the
reader CLI; subprocess tests reject both.
