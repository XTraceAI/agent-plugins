"""Local checkpoint selection and durable creation, using the existing runner/API."""
from uuid import uuid4
from readers import codex as reader
from redact import redact_text
from .capture import prepare
from .checkpoints import find_checkpoint
from .client import Client
from .state import Unavailable, branch_path, digest, identifier, lock, read, root, write


def locate(session_id):
    """(harness, native session ID, transcript). Exactly one local store must
    hold the session; a `codex-` capture ID names Codex explicitly."""
    explicit = session_id.startswith('codex-')
    session_id = identifier(session_id.removeprefix('codex-'))
    found = []
    path, _ = reader.locate(session_id)
    if path is not None:
        found.append(('codex', session_id, path))
    if not explicit:
        from .claude import locate as locate_claude
        path = locate_claude(session_id)
        if path is not None:
            found.append(('claude_code', session_id, path))
    if not found:
        raise Unavailable('native_session_unavailable')
    if len(found) > 1:
        raise Unavailable('native_session_ambiguous')
    return found[0]


def choices(session_id):
    harness, session_id, path = locate(session_id)
    if read(branch_path(session_id)):
        raise Unavailable('comparison_source_unsupported')
    if harness == 'claude_code':
        from readers import claude as claude_reader
        from .claude_capture import annotate, fenced
        records = claude_reader.load(path)
        if fenced(next((r['cwd'] for r in records if isinstance(r.get('cwd'), str)), None)):
            raise Unavailable('comparison_source_unsupported')
        records = annotate(records, session_id, path, include_checkpoints=True)
    else:
        records, _, lineage = prepare(path, session_id, include_checkpoints=True)
        if lineage:
            raise Unavailable('comparison_source_unsupported')
    result = []
    for record in records:
        checkpoint = record.get('rewind_v1', {}).get('checkpoint')
        if not checkpoint:
            continue
        content = record.get('message', {}).get('content', '')
        text = content if isinstance(content, str) else '\n'.join(
            part.get('text', '') for part in content if part.get('type') == 'text')
        result.append({'event_id': record['uuid'], 'checkpoint_id': checkpoint['checkpoint_id'],
                       'turn_id': checkpoint['native_turn_id'], 'captured_at': checkpoint['captured_at'],
                       'preview': redact_text(text)[:240], '_checkpoint': checkpoint})
    return result


def start(session_id, event_id, org_id, intervention, criterion, baseline_mode='historical',
          *, adapter_factory=None, request_id=None):
    from .adapter import resolve
    session_id, org_id = identifier(session_id.removeprefix('codex-')), identifier(org_id)
    if not criterion.strip() or len(criterion) > 2000:
        raise Unavailable('criterion_invalid')
    if intervention.get('kind') not in ('instruction', 'model'):
        raise Unavailable('intervention_invalid')
    field = 'replacement_text' if intervention['kind'] == 'instruction' else 'model_id'
    if set(intervention) != {'kind', field} or not intervention[field].strip():
        raise Unavailable('intervention_invalid')
    if baseline_mode not in ('historical', 'fresh'):
        raise Unavailable('baseline_invalid')
    matches = [c for c in choices(session_id) if c['event_id'] == event_id]
    if len(matches) != 1:
        raise Unavailable('checkpoint_unavailable')
    checkpoint = matches[0]['_checkpoint']
    manifest = find_checkpoint(checkpoint['checkpoint_id'])
    # Preflight before creating a remote pending run. Never replace old snapshots
    # with the current filesystem or relax permissions to make a retry pass.
    config = dict(checkpoint['original_config'])
    if field == 'model_id':
        config['model_id'] = intervention[field]
    adapter = (adapter_factory or resolve)(config['harness'])
    try:
        adapter.preflight(manifest, config)
        adapter.resolve_boundary(manifest)
    finally:
        adapter.close()
    client = Client(org_id)
    body = {'source': client.source(session_id, config['harness']), 'boundary': {
        'native_user_message_id': event_id, 'checkpoint_id': checkpoint['checkpoint_id'],
        'client_id': checkpoint['client_id']}, 'intervention': intervention,
        'criterion': criterion, 'baseline_mode': baseline_mode}
    # Same command after a lost create ACK has the same idempotency key. Keep
    # completed creates too: retries resume, never silently duplicate inference.
    path = root() / 'requests' / (digest([client.origin, org_id, request_id or body]) + '.json')
    with lock(path.with_suffix('.lock')):
        request = read(path)
        if request is not None and request['body'] != body:
            raise Unavailable('idempotency_conflict')
        if request is None:
            request = {'key': str(uuid4()), 'body': body, 'org_id': org_id}
            write(path, request)
        if not request.get('comparison_id'):
            detail = client.create(request['body'], request['key'])
            request['comparison_id'] = identifier(detail['id'])
            write(path, request)
        return request['comparison_id']
