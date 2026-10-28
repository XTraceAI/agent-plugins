# Bounded Codex context, version 1

`readers_cli.py --host codex --exact-detail --session <native UUID>
--context-page '<selection JSON>'` reads complete canonical records from one flat
rollout or a single paginated rollout. That rollout is either an original root or
a fork whose history points into a rollout of the session it was forked from.
The existing discovery and history planner must prove the complete source set,
including the parent rollout a fork points to. From that parent, only the usage
counters of the referenced prefix are read, so the fork's first usage delta is its
own work; none of the parent's rows become page records. A fork whose parent was
not found and resolved is refused. Multi-file histories and inherited subagent
context are refused. Ordinary export and full exact-detail behavior are unchanged.

Selections are closed objects:

- `{"version":1,"kind":"first"}`
- `{"version":1,"kind":"before","position":200,"generation":"<digest>"}`
- `{"version":1,"kind":"after","position":200,"generation":"<digest>"}`
- `{"version":1,"kind":"anchor","record_uuid":"<saved UUID>","block":null,
  "expected":"message","generation":null}`

Anchor `expected` is `message`, `tool_call`, or `tool_block`; a tool target requires
its original numeric block index. A non-null generation must match the verified
source. An anchor retains up to 12 preceding records and subsequent records within
the page bounds. Smaller byte bounds can reduce either edge while keeping the
anchor. Pages are contiguous intervals in the shared converter/history/ledger's
final order, including usage-only records and removal of superseded legacy rows.

Success is the usual session header plus records. The header adds exactly
`context_page: {version:1,digest,start,end,total,target}`, where `[start,end)` and
target are global final-record positions. Target is null outside anchor selection.
The source path remains confined to the native reader protocol. No partial success
is written: late parse, identity, ledger, discovery, deadline or limit failure
returns exit 2 and no records. `context_changed`, `context_invalid_cursor`,
`context_target_missing`, `context_target_ambiguous`, `context_block_missing`, and
`context_record_too_large` are additional static refusal codes.

The read indexes native row offsets, then uses the same shared converter and
usage overlay twice: first with text removed to establish final IDs/order, then
with only the selected bodies retained. One descriptor and captured length serve
all passes. A fork's parent is opened once per pass through the identity
discovery recorded and read forward, buffered, up to its captured length: every
byte counts toward the same scan limit and the parent's bytes toward the source
limit. A parent line may be up to 64 MiB, because the parent can hold large tool
outputs such as screenshots; it is held one line at a time (reading and parsing
a line takes about three times its size), and the fork's own rows keep the 8 MiB
line limit. The prefix lines each pass reads, up to the fork point, count with
the fork's own rows toward the 200,000 row limit; the full read also counts the
parent's rows after the fork point, which this read never parses. A parent that
shrinks, grows or is replaced while it is read refuses the page. Descriptor, source name, configured root and complete discovered
source set are checked before output. No source snapshot or transcript cache is
written. Limits remain finite: 256 MiB source (the fork and its parent together), 2 GiB cumulative replay, 32 full
iterations, existing 8 MiB line/200,000 row/100,000 canonical record limits,
32 MiB encoded metadata, 200 retained bodies/4 MiB, 1 MiB output, and the caller's
existing exact-detail deadline. Limits refuse content rather than truncating a
record. Root/page digests are change detectors, not indexed-generation proofs.

Run `PYTHONPATH=plugins/memhub/scripts:tests python3 tests/readers_cli_context_test.py`
and the existing `readers_cli_detail_test.py`, `codex_history_test.py`, and
`readers_cli_test.py` suites. Their source transcripts are synthetic.

Retained metadata ownership includes the encoded canonical record metadata and
new response-key/counter entries held by usage reconciliation, under one 32 MiB
counter per normalization. A shared optional guard checks proposed final ledger
metadata before inserting a response, appending a record, or changing its target;
delayed legacy usage is checked through the same guard. Per-record high-water
charges are not refunded while reconciliation may still reference removed rows.
Selected record growth also preflights the existing 4 MiB body bound. This is an
encoded ownership bound, not an exact Python heap measurement.

The final complete-source check walks rollout paths directly and preserves the
initial path set, configured root and every original file identity. Selected
files must retain their full original and prepared revisions, descriptor and
body checks. For unrelated files, each initial bounded header probe records a
digest and length of the exact prefix it consumed, tied to its parsed native
session ID. Only unrelated files whose full revision changed are probed again:
the same anchored regular file must not shrink below either its initial or
probe-observed length, and that entire prefix must still match. Unused foreign
bodies may grow; they are not claimed immutable. A shrink directly observed
during preparation or the initial probe refuses even if the file later regrows.
Context checks retain the initial device/inode and a nondecreasing size floor
through the original snapshot, prepared revision, header open, saved final
revision, prefix open, post-prefix descriptor stat, and name reopen. Each saved
fact is checked before a fresher observation can supersede it: restoring the
original file or regrowing it cannot erase an observed replacement or decrease.
These are checks of the context revision/open observations, not an atomic
filesystem snapshot; shared helpers' intermediate identity-only checks are
unchanged. Replacement, header/prefix change, missing/new sources and incomplete
discovery still refuse all output.
The extra probes and bytes spend the same cumulative probe/deadline limits;
selected acquisition stamps are never replaced. The walk charges every entry
without constructing unused session-list rows again. Initial session discovery
and all ceilings are unchanged. Wide histories beyond the same cumulative
entry limit still refuse the entire page.
