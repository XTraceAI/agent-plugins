"""Claude Code capture integration; flush_turn keeps upload, retries and ACKs.

Source sessions: every native event carries its ``rewind_v1`` cursor identity,
and the first genuine record of a checkpointed prompt carries the checkpoint.

Branch sessions: the fork kept the source's native UUIDs, so inherited records
are never uploaded at all and there is nothing to translate. Every batch
carries the authenticated lineage with an empty, completed prefix map. A
checkout fenced by a launch marker but not yet registered is held locally;
there is no fallback ordinary upload.
"""
from __future__ import annotations
from pathlib import Path
from .claude import genuine_user, load, metadata, turn_model
from .state import Unavailable, branch_path, capabilities_path, digest, journal_path, read, root, write

_BRANCH_KEYS = ('protocol_version', 'comparison_id', 'run_id', 'claim_id', 'native_branch_id', 'parent',
                'parent_boundary', 'continuation_start_event_id', 'checkpoint_manifest_digest')


def _checkpoint(session_id, prompt_id):
    boundary = read(root() / 'boundaries' / (digest([session_id, prompt_id]) + '.json'), {})
    if not boundary.get('checkpoint_id'):
        return None
    return read(root() / 'repositories' / boundary['repository_id'] / (boundary['checkpoint_id'] + '.json'))


def _firsts(rows):
    """Index of each prompt's first main-chain user record."""
    found = {}
    for index, row in enumerate(rows):
        prompt = row.get('promptId')
        if prompt and row.get('type') == 'user' and not row.get('isSidechain'):
            found.setdefault(prompt, index)
    return found


def annotate(records, session_id, transcript_path, *, include_checkpoints=False):
    """A same-length list, so flush_turn's byte-offset bookkeeping holds."""
    if not include_checkpoints and not read(root() / 'settings.json', {}).get('enabled') \
            and not read(branch_path(session_id)):
        return records
    rows = firsts = None
    out = []
    for record in records:
        if not isinstance(record, dict) or not record.get('uuid'):
            out.append(record)
            continue
        record = {**record, 'rewind_v1': {'native_event_id': record['uuid'], 'capture_version': 1}}
        prompt = record.get('promptId')
        manifest = _checkpoint(session_id, prompt) if prompt and genuine_user(record) else None
        if manifest:
            # Rare (once per checkpointed prompt): the turn's model and the
            # previous prompt may sit outside this slice.
            if rows is None:
                rows = load(transcript_path)
                firsts = _firsts(rows)
            index = firsts.get(prompt)
            model = turn_model(rows, index) if index is not None and rows[index].get('uuid') == record['uuid'] else None
            if model:
                earlier = [i for i in firsts.values() if i < index and genuine_user(rows[i])]
                previous = rows[max(earlier)] if earlier else {}
                record['rewind_v1'] = {
                    'checkpoint': metadata(manifest, record['uuid'], record.get('version'), model),
                    'capabilities': read(capabilities_path('claude_code')), 'capture_version': 1,
                    'native_event_id': record['uuid'],
                    'previous_native_message_id': previous.get('uuid'),
                    'previous_native_turn_id': previous.get('promptId')}
        out.append(record)
    return out


def fenced(cwd):
    return bool(cwd) and (root() / 'launches' / (digest(str(Path(cwd).resolve())) + '.json')).exists()


def lineage(session_id, transcript_path, cwd):
    """(comparison, inherited UUIDs). (None, empty) for an ordinary session."""
    branch = read(branch_path(session_id))
    if branch is None:
        if fenced(cwd):
            # A crash after the fork, before registration: never upload the
            # checkout's capture as an ordinary session.
            raise Unavailable('comparison_lineage_pending')
        return None, set()
    if branch.get('native_branch_id') != session_id or not branch.get('claim_id'):
        raise Unavailable('comparison_lineage_pending')
    journal = read(journal_path(branch['run_id']), {})
    prefix = journal.get('prefix_event_ids')
    if (journal.get('native_branch_id') != session_id or journal.get('claim_id') != branch['claim_id']
            or journal.get('comparison_id') != branch.get('comparison_id') or not isinstance(prefix, list)):
        raise Unavailable('native_lineage_unavailable')
    rows = load(transcript_path)
    if [r['uuid'] for r in rows if r.get('uuid')][:len(prefix)] != prefix:
        raise Unavailable('native_prefix_mapping_unavailable')
    inherited = set(prefix)
    own = [r for r in rows if r.get('uuid') and r['uuid'] not in inherited
           and r.get('type') in ('user', 'assistant') and not r.get('isSidechain')]
    if not own:
        raise Unavailable('comparison_continuation_pending')
    if not genuine_user(own[0]):
        raise Unavailable('comparison_continuation_changed')
    start = own[0]['uuid']
    if branch.get('continuation_start_event_id') not in (None, start):
        raise Unavailable('comparison_continuation_changed')
    branch['continuation_start_event_id'] = start
    models = [r.get('message', {}).get('model') for r in own if r['type'] == 'assistant']
    models = [m for m in models if m and m != '<synthetic>']
    if models:
        branch['actual_model_id'] = models[-1]
    branch['human_interventions'] = [r['uuid'] for r in own if genuine_user(r)][1:51]
    write(branch_path(session_id), branch)
    comparison = {key: branch[key] for key in _BRANCH_KEYS}
    comparison.update(prefix_mapping=[], prefix_map_offset=0, prefix_map_complete=True)
    return comparison, inherited


def prepare(session_id, transcript_path, records):
    """(annotated records, indices never to send, comparison or None).

    The inherited indices are consumed like command wrappers: skipped, never
    deferred, so the byte cursor still advances past them.
    """
    cwd = next((r['cwd'] for r in records if isinstance(r, dict) and isinstance(r.get('cwd'), str)), None)
    comparison, inherited = lineage(session_id, transcript_path, cwd)
    try:
        annotated = annotate(records, session_id, transcript_path)
    except (Unavailable, OSError, ValueError, KeyError, TypeError):
        # Identities are an enhancement of ordinary capture, which must not
        # stall on a malformed local manifest. The boundary just stays unusable.
        annotated = records
    skip = {i for i, r in enumerate(annotated) if isinstance(r, dict) and r.get('uuid') in inherited}
    return annotated, skip, comparison


def watermark(session_id, sent):
    """Advance the branch's final watermark after a confirmed import ACK."""
    events = [r['uuid'] for r in sent if isinstance(r, dict) and r.get('uuid')
              and r.get('type') in ('user', 'assistant')]
    branch = read(branch_path(session_id))
    if branch and events:
        branch['last_capture_watermark'] = {'event_id': events[-1], 'capture_version': 1}
        write(branch_path(session_id), branch)
