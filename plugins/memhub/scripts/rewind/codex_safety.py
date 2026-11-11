"""Fail closed on unsupported native permissions, hooks and workspace configuration."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from .state import Unavailable


def native_boundary_supported(binary):
    if os.name == 'nt':
        return False
    with tempfile.TemporaryDirectory(prefix='memhub-codex-schema-') as directory:
        result = subprocess.run([binary, 'app-server', 'generate-json-schema', '--experimental',
                                 '--out', directory], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            return False
        path = Path(directory) / 'v2/ThreadForkParams.json'
        if not path.is_file():
            return False
        props = json.loads(path.read_text(encoding="utf-8")).get('properties', {})
        return all(p in props for p in ('beforeTurnId', 'runtimeWorkspaceRoots', 'deferGoalContinuation'))


def validate_local_tools(rpc, repo):
    import setup_codex_hooks as setup
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    if not setup.status(home)[0]:
        raise Unavailable('memhub_checkpoint_hook_unavailable')
    from .isolation import inspect
    info = inspect(rpc, repo)
    expected = setup._bridge_for_home(setup._load_json(setup.BRIDGE_SOURCE), home)
    actual = setup._load_json(home / 'hooks.json')
    for event, groups in expected.get('hooks', {}).items():
        if not all(group in actual.get('hooks', {}).get(event, []) for group in groups):
            raise Unavailable('memhub_checkpoint_hook_unavailable')
    if not any(h.get('eventName') == 'userPromptSubmit' and h.get('trustStatus') == 'trusted'
               and h.get('sourcePath') == str(home / 'hooks.json') for h in info['hooks']):
        raise Unavailable('checkpoint_hook_not_trusted')
    return info


def permission_options(context, cwd):
    policy = context.get('sandbox_policy', {'type': 'read-only'})
    mode = policy.get('type')
    if mode == 'read-only':
        return {'sandbox': mode}
    if mode != 'workspace-write':
        raise Unavailable('permission_policy_unsupported')
    return {'sandbox': mode, 'config': {'sandbox_workspace_write': {
        'writable_roots': runtime_roots(context, cwd),
        'network_access': bool(policy.get('network_access', False)),
        'exclude_tmpdir_env_var': bool(policy.get('exclude_tmpdir_env_var', False)),
        'exclude_slash_tmp': bool(policy.get('exclude_slash_tmp', False)),
    }}}


def _path(value):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise Unavailable('permission_path_invalid')
    return Path(value).resolve()


def _source(context):
    return _path(context.get('cwd') or context.get('workspace_roots', [''])[0])


def _roots(context):
    source = _source(context)
    roots = [source]
    for value in [*(context.get('workspace_roots') or []),
                  *context.get('sandbox_policy', {}).get('writable_roots', [])]:
        path = _path(value)
        if path not in roots:
            roots.append(path)
    return roots


def runtime_roots(context, cwd):
    """Rebind source-contained grants; retain disjoint explicitly granted paths."""
    source, target = _source(context), _path(cwd)
    return [str(target / path.relative_to(source)) if path.is_relative_to(source) else str(path)
            for path in _roots(context)]


def validate_context(context, repo):
    source = _path(repo)
    if _source(context) != source:
        raise Unavailable('workspace_root_mismatch')
    policy = context.get('sandbox_policy', {})
    if policy.get('type') == 'workspace-write':
        from .checkpoints import git
        common = _path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
        for grant in _roots(context)[1:]:
            if source.is_relative_to(grant) or common.is_relative_to(grant) or grant.is_relative_to(common):
                raise Unavailable('writable_root_exposes_source')
            # Explicit subdirectory grants must not override native protection
            # for repository control/config directories after rebinding.
            if grant.is_relative_to(source) and grant.relative_to(source).parts[0] in ('.git', '.agents', '.codex', '.aws'):
                raise Unavailable('writable_root_exposes_source')
        # Native implicit temporary directories must not contain the source.
        temp_roots = []
        if not policy.get('exclude_slash_tmp', False):
            temp_roots.append(Path('/tmp'))
        if not policy.get('exclude_tmpdir_env_var', False):
            temp_roots.append(Path(tempfile.gettempdir()))
        for temp in temp_roots:
            if Path(repo).resolve() == temp.resolve() or temp.resolve() in Path(repo).resolve().parents:
                raise Unavailable('source_inside_writable_temporary_root')
        allowed = {'type', 'writable_roots', 'network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp'}
        if set(policy) - allowed:
            raise Unavailable('permission_policy_unsupported')
    elif policy != {'type': 'read-only'}:
        raise Unavailable('permission_policy_unsupported')
    roots = context.get('workspace_roots')
    profile = context.get('permission_profile')
    if roots is None or profile is not None:
        # Newer native rollouts omit workspace_roots and record explicit grants.
        profile = context.get('permission_profile', {})
        fs = profile.get('file_system', {})
        if profile.get('type') != 'managed' or fs.get('type') != 'restricted':
            raise Unavailable('permission_profile_unavailable')
        allowed = set(_roots(context))
        special = set()
        if not policy.get('exclude_slash_tmp', False):
            allowed.add(Path('/tmp').resolve())
            special.add('slash_tmp')
        if not policy.get('exclude_tmpdir_env_var', False):
            allowed.add(Path(tempfile.gettempdir()).resolve())
            special.add('tmpdir')
        if profile.get('network', 'restricted') != ('enabled' if policy.get('network_access') else 'restricted'):
            raise Unavailable('permission_policy_unsupported')
        writes = set()
        for entry in fs.get('entries', []):
            grant = entry.get('path', {})
            kind = grant.get('value', {}).get('kind') if grant.get('type') == 'special' else None
            if entry.get('access') == 'write':
                if policy.get('type') != 'workspace-write':
                    raise Unavailable('permission_profile_mismatch')
                if not (kind in special or grant.get('type') == 'path' and _path(grant.get('path')) in allowed):
                    raise Unavailable('permission_profile_mismatch')
                if grant.get('type') == 'path':
                    writes.add(_path(grant['path']))
            elif entry.get('access') == 'read':
                if kind == 'root':
                    continue
                path = _path(grant.get('path'))
                if not any(path == root / name for root in _roots(context)
                           for name in ('.git', '.agents', '.codex', '.aws')):
                    raise Unavailable('custom_read_restriction_unsupported')
            else:
                raise Unavailable('permission_policy_unsupported')
        if policy.get('type') == 'workspace-write' and any(
                not any(root.is_relative_to(grant) for grant in writes) for root in _roots(context)):
            raise Unavailable('permission_profile_mismatch')
    if roots is not None and source not in [_path(p) for p in roots]:
        raise Unavailable('workspace_root_mismatch')
    if context.get('approval_policy') not in ('never', 'on-request', 'untrusted'):
        raise Unavailable('approval_policy_unsupported')


def verify_policy(result, context, cwd):
    expected = permission_options(context, cwd)
    actual = result.get('sandbox', {})
    if expected['sandbox'] == 'read-only':
        if actual.get('type') not in ('readOnly', 'read-only'):
            raise Unavailable('native_permission_mismatch')
        return
    if actual.get('type') not in ('workspaceWrite', 'workspace-write'):
        raise Unavailable('native_permission_mismatch')
    config = expected['config']['sandbox_workspace_write']
    for snake, camel in [('writable_roots','writableRoots'), ('network_access','networkAccess'),
                         ('exclude_tmpdir_env_var','excludeTmpdirEnvVar'), ('exclude_slash_tmp','excludeSlashTmp')]:
        value = actual.get(camel, actual.get(snake))
        if snake == 'writable_roots':
            implicit = result.get('runtimeWorkspaceRoots', [cwd])
            if isinstance(value, list) and set(value) | set(implicit) == set(config[snake]):
                continue  # separately verified runtime roots supply implicit writes
        if value != config[snake]:
            raise Unavailable('native_permission_mismatch')
