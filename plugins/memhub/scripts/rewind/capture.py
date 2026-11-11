"""Existing capture integration and durable lineage upload fence (no new transport)."""
from __future__ import annotations
from pathlib import Path
from readers import codex as reader, codex_usage
from .hooks import metadata
from .state import Unavailable, branch_path, digest, journal_path, read, root, write


def parsed(path):
    rows = reader.load_rollout(path)
    sources, usage_targets = {}, {}
    header = reader._session_meta(rows)
    records, meta = reader.rollout_to_claude_records(
        rows, record_sources=sources, usage_targets=usage_targets,
        # A fork may inherit cumulative counters without their earlier snapshots.
        # Without a response ledger, its first unanchored delta is unknown.
        usage_baseline_unknown=bool(header.get('forked_from_id')))
    cutoff = header.get('forked_from_ordinal_exclusive')
    usage_rows = [
        {'type': 'inherited_context'}
        if (row.get('type') == 'token_usage_record'
            and type(cutoff) is int and type(row.get('ordinal')) is int
            and row['ordinal'] < cutoff)
        else row for row in rows]
    records = codex_usage.apply(usage_rows, records, sources, usage_targets,
                               session_id=meta['session_id'], namespace=meta['session_id'], seen={})
    codex_usage.attach_reasoning(usage_rows, records)
    turns, current = {}, None
    for index, row in enumerate(rows):
        payload = row.get('payload', {})
        if row.get('type') == 'turn_context' or payload.get('type') == 'task_started':
            current = payload.get('turn_id', current)
        turns[index] = current
    return rows, sources, records, meta, turns


def prepare(path, sid, *, include_checkpoints=False):
    records, meta = reader.to_canonical(path)
    branch = read(branch_path(sid))
    # A crash after fork, before branch registry write is fenced by the checkout
    # marker written BEFORE the fork. It also survives TTY handoff/restarts.
    marker = root() / 'launches' / (digest(str(Path(meta.get('cwd') or '/').resolve())) + '.json')
    if marker.exists() and not branch:
        raise Unavailable('comparison_lineage_pending')
    if not branch and not include_checkpoints and not read(root() / 'settings.json', {}).get('enabled'):
        return records, meta, None
    rows, sources, records, meta, turns = parsed(path)
    previous_event, previous_turn = None, None
    for record in records:
        record['native_event_id'] = record['uuid']
        # Ingestion may split text blocks or give tool-only rows a storage
        # suffix. Keep the canonical identity on every event so the runner's
        # final watermark resolves independently of that persistence detail.
        record['rewind_v1'] = {'native_event_id': record['uuid'], 'capture_version': 1}
        turn = turns.get(sources[id(record)])
        content = record.get('message', {}).get('content')
        genuine = record.get('type') == 'user' and not record.get('isMeta') and not (
            isinstance(content, list) and any(p.get('type') == 'tool_result' for p in content))
        if not genuine or not turn:
            continue
        boundary = read(root() / 'boundaries' / (digest([sid, turn]) + '.json'), {})
        if boundary.get('checkpoint_id'):
            manifest = read(root() / 'repositories' / boundary['repository_id'] /
                            (boundary['checkpoint_id'] + '.json'))
            if manifest:
                record['rewind_v1'] = {'checkpoint': metadata(manifest, record['uuid'], meta.get('cli_version')),
                    'capabilities': read(root() / 'capabilities-codex.json'), 'capture_version': 1,
                    'native_event_id': record['uuid'], 'previous_native_message_id': previous_event,
                    'previous_native_turn_id': previous_turn}
        previous_event, previous_turn = record['uuid'], turn
    if branch is None:
        return records, meta, None
    if branch.get('native_branch_id') != sid or not branch.get('claim_id'):
        raise Unavailable('comparison_lineage_pending')
    header = rows[0].get('payload', {})
    cutoff = header.get('forked_from_ordinal_exclusive')
    if header.get('forked_from_id') != branch['source_native_session_id']:
        raise Unavailable('native_lineage_unavailable')
    if cutoff is None:
        # Native Codex can omit the ordinal boundary when the fork retained no
        # turns. The durable runner journal records the prefix verified against
        # the native fork response before any continuation was submitted.
        journal = read(journal_path(branch['run_id']), {})
        if (journal.get('native_branch_id') != sid or
                journal.get('comparison_id') != branch.get('comparison_id') or
                journal.get('claim_id') != branch.get('claim_id') or
                journal.get('prefix_turn_ids') != []):
            raise Unavailable('native_lineage_unavailable')
        cutoff = 0
    if not isinstance(cutoff, int):
        raise Unavailable('native_lineage_unavailable')
    parent_rows, parent_sources, parent_records, _, _ = parsed(Path(branch['source_transcript']))
    parent_ids = {parent_rows[parent_sources[id(r)]].get('ordinal'): r['uuid'] for r in parent_records}
    pairs, own = [], []
    for record in records:
        ordinal = rows[sources[id(record)]].get('ordinal')
        if ordinal is None:
            raise Unavailable('native_lineage_ordinal_unavailable')
        if ordinal < cutoff:
            if ordinal not in parent_ids:
                raise Unavailable('native_prefix_mapping_unavailable')
            pairs.append({'source_event_id': parent_ids[ordinal], 'branch_event_id': record['uuid']})
        else:
            own.append(record)
    if not own:
        raise Unavailable('comparison_continuation_pending')
    start = own[0]['uuid']
    if branch.get('continuation_start_event_id') not in (None, start):
        raise Unavailable('comparison_continuation_changed')
    branch['continuation_start_event_id'] = start
    branch['prefix_mapping'] = pairs
    models = [r['payload'].get('model') for r in rows if r.get('type') == 'turn_context']
    if models and models[-1]:
        branch['actual_model_id'] = models[-1]
    humans = [r['uuid'] for r in own if r.get('type') == 'user' and not r.get('isMeta')
              and not (isinstance(r.get('message', {}).get('content'), list) and
                       any(p.get('type') == 'tool_result' for p in r['message']['content']))]
    branch['human_interventions'] = humans[1:51]
    write(branch_path(sid), branch)
    comparison = {key: branch[key] for key in (
        'protocol_version', 'comparison_id', 'run_id', 'claim_id', 'native_branch_id', 'parent',
        'parent_boundary', 'continuation_start_event_id', 'checkpoint_manifest_digest')}
    # Keep prefix records in this authenticated import: backend verifies the map
    # and excludes them before persistence. They never go through an ordinary upload.
    comparison['prefix_mapping'] = pairs
    return records, meta, comparison


def chunks(comparison):
    pairs = comparison['prefix_mapping']
    offsets = range(0, len(pairs), 100) if pairs else [0]
    return [{**comparison, 'prefix_mapping': pairs[offset:offset + 100],
             'prefix_map_offset': offset, 'prefix_map_complete': offset + 100 >= len(pairs)}
            for offset in offsets]
