"""Exclusive native fork, with native terminal approvals and human continuation."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from _codex_rpc import AppServer
from .state import Unavailable, client_id, digest, root

class Codex:
    def __init__(self, binary=None, rpc=None):
        binary = binary or os.environ.get('MEMHUB_CODEX_BIN') or 'codex'
        self.binary = shutil.which(binary) or binary
        self.rpc = rpc
        self.version = subprocess.run([binary, '--version'], capture_output=True,
                                      text=True, check=True, timeout=10).stdout.strip()
        if self.rpc is None:
            root().mkdir(parents=True, exist_ok=True, mode=0o700)
            self.rpc = AppServer([binary, 'app-server'], cwd=Path.cwd(), env=os.environ.copy(),
                                 log=Path(os.devnull))
            self.rpc.request('initialize', {'clientInfo': {'name': 'memhub-rewind', 'version': '1'},
                                           'capabilities': {'experimentalApi': True}})
            self.rpc.notify('initialized')

    def capabilities(self):
        import datetime
        from .codex_safety import native_boundary_supported
        supported = native_boundary_supported(self.binary)
        models = self.rpc.request('model/list', {'limit': 100}).get('data', [])
        return {'protocol_version': 1, 'client_id': client_id(), 'harness': 'codex',
                'harness_version': self.version, 'adapter_version': '1',
                'reported_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'supports_before_user_turn': supported,
                'models': [{'model_id': m['model'], 'display_name': m.get('displayName', m['model']),
                            'supported_efforts': [r['reasoningEffort'] for r in m.get('supportedReasoningEfforts', [])],
                            'access': 'unknown'} for m in models]}

    def turns(self, thread_id):
        thread = self.rpc.request('thread/read', {'threadId': thread_id, 'includeTurns': True})['thread']
        if thread.get('historyMode') != 'paginated':
            return thread['turns']
        turns, cursor, seen = [], None, set()
        while True:
            page = self.rpc.request('thread/turns/list', {'threadId': thread_id, 'cursor': cursor,
                                                        'limit': 100, 'sortDirection': 'asc', 'itemsView': 'full'})
            turns.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return turns
            if cursor in seen:
                raise Unavailable('native_history_pagination_invalid')
            seen.add(cursor)

    def resolve_boundary(self, checkpoint):
        turns = self.turns(checkpoint['native_session_id'])
        matches = [i for i, t in enumerate(turns) if t['id'] == checkpoint['native_turn_id']]
        if len(matches) != 1:
            raise Unavailable('native_boundary_unavailable')
        i = matches[0]
        if any(t.get('status') == 'inProgress' for t in turns[:i + 1]):
            raise Unavailable('native_thread_busy')
        users = [item for item in turns[i]['items'] if item['type'] == 'userMessage']
        if not users:
            raise Unavailable('native_user_message_unavailable')
        # The snapshot and native fork are before the whole turn. Clarification
        # replies and steering later in that turn belong to the old continuation;
        # replay only the input that started it.
        return {'prefix': turns[:i], 'input': users[0]['content'], 'selected': users[0]['id']}

    def preflight(self, checkpoint, requested_config):
        from .hooks import config_fingerprint, context_for
        local = checkpoint['local_config']
        if local['fingerprint'] != config_fingerprint(checkpoint['repo']):
            raise Unavailable('local_configuration_changed')
        context = context_for(local['transcript_path'], checkpoint['native_turn_id'])
        if not context or context.get('model') != local['model']:
            raise Unavailable('effective_configuration_unknown')
        from .codex_safety import validate_context, validate_local_tools
        validate_context(context, checkpoint['repo'])
        local_tools = validate_local_tools(self.rpc, checkpoint['repo'])
        config = {**requested_config, '_context': context, '_local_tools': local_tools}
        caps = self.capabilities()
        if not caps['supports_before_user_turn']:
            raise Unavailable('native_boundary_api_unavailable')
        choices = {m['model_id']: m for m in caps['models']}
        model = requested_config['model_id']
        if model not in choices:
            raise Unavailable('model_not_in_native_catalog')
        effort = requested_config.get('reasoning_effort')
        if effort is not None and effort not in choices[model]['supported_efforts']:
            raise Unavailable('model_effort_incompatible')
        original_effort = context.get('collaboration_mode', {}).get('settings', {}).get('reasoning_effort')
        if effort != original_effort:
            raise Unavailable('reasoning_effort_changed')
        if self.version.split()[-1] != requested_config['harness_version'].split()[-1]:
            print(f"Codex version differs from checkpoint: {requested_config['harness_version']} -> "
                  f"{self.version}; required capabilities verified.", file=sys.stderr)
        config['harness_version'] = self.version
        return config

    def prepare(self, checkpoint, config, cwd):
        from .isolation import prepare, review
        config['_boundary'] = self.resolve_boundary(checkpoint)
        value = prepare(self.binary, checkpoint, config, cwd)
        self.rpc.close()
        self.rpc = AppServer([self.binary, 'app-server'], cwd=Path(cwd),
                             env={**os.environ, 'CODEX_HOME': value['home']}, log=Path(os.devnull))
        self.rpc.request('initialize', {'clientInfo': {'name': 'memhub-rewind', 'version': '1'},
                                       'capabilities': {'experimentalApi': True}})
        self.rpc.notify('initialized')
        review(self.rpc, cwd, value)
        config['_isolation'] = value

    def fork(self, checkpoint, config, cwd):
        boundary = config['_boundary'] if '_boundary' in config else self.resolve_boundary(checkpoint)
        context = config['_context']
        from .codex_safety import permission_options, verify_policy, runtime_roots
        roots = runtime_roots(context, cwd)
        result = self.rpc.request('thread/fork', {
            'threadId': checkpoint['native_session_id'], 'beforeTurnId': checkpoint['native_turn_id'],
            'cwd': cwd, 'runtimeWorkspaceRoots': roots, 'model': config['model_id'],
            'approvalPolicy': context['approval_policy'],
            'approvalsReviewer': context.get('approvals_reviewer', 'user'),
            **permission_options(context, cwd), 'excludeTurns': False, 'deferGoalContinuation': True})
        branch = result['thread']['id']
        if branch == checkpoint['native_session_id']:
            raise Unavailable('native_fork_identity_invalid')
        if self.turns(branch) != boundary['prefix']:
            raise Unavailable('native_fork_prefix_mismatch')
        if result.get('model') != config['model_id'] or result.get('cwd') != cwd:
            raise Unavailable('native_configuration_mismatch')
        if (result.get('approvalPolicy') != context['approval_policy'] or
                result.get('approvalsReviewer') != context.get('approvals_reviewer', 'user') or
                result.get('reasoningEffort') != config.get('reasoning_effort') or
                result.get('runtimeWorkspaceRoots') != roots):
            raise Unavailable('native_configuration_mismatch')
        verify_policy(result, context, cwd)
        return {'native_branch_id': branch, 'input': boundary['input'],
                'prefix_turn_ids': [t['id'] for t in boundary['prefix']]}

    def command(self, native_branch_id, cwd, config, input_parts=None):
        context = config['_context']
        from .codex_safety import permission_options
        permission = permission_options(context, cwd)
        command = [self.binary, 'resume', native_branch_id, '--cd', cwd, '--model', config['model_id'],
                   '--sandbox', permission['sandbox'], '--ask-for-approval', context['approval_policy']]
        for key, value in permission.get('config', {}).get('sandbox_workspace_write', {}).items():
            command += ['-c', 'sandbox_workspace_write.' + key + '=' + json.dumps(value)]
        if config.get('reasoning_effort') is not None:
            command += ['-c', 'model_reasoning_effort=' + json.dumps(config['reasoning_effort'])]
        command += ['-c', 'approvals_reviewer=' + json.dumps(context.get('approvals_reviewer', 'user'))]
        if input_parts is not None:
            texts = []
            for item in input_parts:
                if item['type'] == 'text':
                    texts.append(item['text'])
                elif item['type'] == 'image' and item['url'].startswith('data:image/'):
                    header, encoded = item['url'].split(',', 1)
                    if ';base64' not in header:
                        raise Unavailable('attachment_encoding_unsupported')
                    path = root() / 'attachments' / (digest(item['url']) + '.png')
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(base64.b64decode(encoded, validate=True))
                    command += ['--image', str(path)]
                else:
                    raise Unavailable('attachment_unavailable')
            command += ['--', '\n'.join(texts)]
        if config.get('_isolation'):
            from .isolation import verify_files
            verify_files(config['_isolation'])
            command = ['/usr/bin/env', 'CODEX_HOME=' + config['_isolation']['home'], *command]
        return command

    def close(self):
        self.rpc.close()
