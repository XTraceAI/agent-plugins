"""Synchronous before-user snapshot, and later enrichment from native turn context."""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
from readers import codex as reader
from .checkpoints import Store
from .state import Unavailable, digest, lock, read, root, write


def config_fingerprint(repo):
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    paths = [home / 'config.toml', home / 'hooks.json', home / 'AGENTS.md']
    paths += [p / 'AGENTS.md' for p in Path(repo).parents]
    return digest({str(p): digest(p.read_text(encoding="utf-8")) if p.is_file() else None for p in paths})


def context_for(path, turn):
    rows = reader.load_rollout(Path(path))
    return next((r['payload'] for r in rows if r.get('type') == 'turn_context'
                 and r.get('payload', {}).get('turn_id') == turn), None)


def capture_prompt(payload, harness='codex'):
    """Called in the existing synchronous bridge, before any model request.

    Rewind is opt-in locally. A failed checkpoint never blocks normal user work;
    it records a visible unavailable reason rather than claiming later state.
    Claude Code's join key is the prompt ID its hook receives and later writes
    on that prompt's user record; Codex's is the hook's turn ID.
    """
    if not read(root() / 'settings.json', {}).get('enabled'):
        return
    from repo_allow import session_enabled
    if not session_enabled(payload.get('cwd', '')):
        return
    sid = payload.get('session_id')
    turn = payload.get('prompt_id') if harness == 'claude_code' else payload.get('turn_id')
    if not sid or not turn or not payload.get('transcript_path'):
        return
    key = root() / 'boundaries' / (digest([sid, turn]) + '.json')
    with lock(key.with_suffix('.lock')):
        if key.exists():
            return  # retry of the same prompt must not replace the before-turn state
        try:
            store = Store(payload['cwd'])
            if harness == 'claude_code':
                from .claude import local_config
                config = local_config(payload, store.repo)
            else:
                config = {'model': payload.get('model'), 'fingerprint': config_fingerprint(store.repo),
                          'transcript_path': payload['transcript_path']}
            manifest = store.capture(sid, turn, config)
            write(key, {'checkpoint_id': manifest['checkpoint_id'], 'repository_id': store.repo_id})
        except (Unavailable, OSError, ValueError, KeyError):
            write(key, {'unavailable': 'checkpoint_capture_failed'})


def metadata(manifest, native_event_id, version):
    context = context_for(manifest['local_config']['transcript_path'], manifest['native_turn_id'])
    if not context:
        raise Unavailable('turn_configuration_unavailable')
    config = {
        'harness': 'codex', 'harness_version': version or 'unknown', 'adapter_version': '1',
        'model_id': context['model'],
        'reasoning_effort': context.get('collaboration_mode', {}).get('settings', {}).get('reasoning_effort'),
        'permission_profile_digest': digest([context.get('approval_policy'), context.get('sandbox_policy'),
                                            context.get('permission_profile')]),
        'instruction_config_digest': manifest['local_config']['fingerprint'],
        'tool_config_digest': manifest['local_config']['fingerprint'],
        'provider_profile_id': digest(manifest['local_config']['fingerprint']), 'settings_known': True}
    return {'protocol_version': 1, 'checkpoint_id': manifest['checkpoint_id'],
            'client_id': manifest['client_id'], 'native_session_id': manifest['native_session_id'],
            'native_user_message_id': native_event_id, 'native_turn_id': manifest['native_turn_id'],
            'captured_at': datetime.datetime.fromtimestamp(manifest['created'], datetime.timezone.utc).isoformat(),
            'repository_id': manifest['repository_id'], 'base_commit': manifest['base_commit'],
            'manifest_digest': manifest['manifest_digest'],
            'coverage': {'status': 'supported', 'reasons': [],
                         'excluded_categories': ['ignored', 'secrets', 'process_state', 'remote_state']},
            'original_config': config}
