"""Private rewind configuration using the installed Codex sandbox, never a patched host."""
from __future__ import annotations
import copy
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from .state import Unavailable, digest, read, root, write


def listed_hooks(rpc, cwd):
    return [h for row in rpc.request('hooks/list', {'cwds': [str(cwd)]}).get('data', [])
            for h in row.get('hooks', [])]


def inspect(rpc, repo):
    """Do not silently drop disabled hooks, plugin hooks, or local integrations."""
    if sys.platform != 'darwin':
        raise Unavailable('process_isolation_platform_unsupported')
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    hooks = listed_hooks(rpc, repo)
    for hook in hooks:
        if hook.get('isManaged') or hook.get('handlerType') != 'command':
            raise Unavailable('hook_confinement_unsupported')
        path = Path(hook['sourcePath'])
        # Project/managed sources cannot be rewritten without changing the checkpoint.
        if not path.resolve().is_relative_to(home) or path.is_symlink():
            raise Unavailable('hook_source_not_isolated')
        if hook.get('enabled') and hook.get('trustStatus') != 'trusted':
            raise Unavailable('source_hook_not_trusted')
    config = rpc.request('config/read', {'cwd': str(repo), 'includeLayers': False}).get('config', {})
    if config.get('profile') or config.get('permissions') or config.get('default_permissions'):
        raise Unavailable('custom_permission_profile_unsupported')
    shell = os.environ.get('SHELL', '/bin/zsh')
    if not Path(shell).is_absolute() or Path(shell).name not in ('zsh', 'bash', 'sh'):
        raise Unavailable('hook_shell_unsupported')
    hook_files = {h['sourcePath']: digest(Path(h['sourcePath']).read_text(encoding='utf-8')) for h in hooks}
    current = listed_hooks(rpc, repo)
    if [(h['key'], h['currentHash'], h.get('enabled')) for h in hooks] != [(h['key'], h['currentHash'], h.get('enabled')) for h in current]:
        raise Unavailable('source_hooks_changed')
    return {'home': str(home), 'hooks': hooks, 'hook_files': hook_files, 'config': config, 'shell': shell}


def _toml(value):
    """Serialize the JSON-compatible config/read response without optional null fields."""
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + _toml(v)
                               for k, v in value.items() if v is not None) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(_toml(v) for v in value) + ']'
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    raise Unavailable('native_config_value_unsupported')


def write_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(''.join(json.dumps(k) + ' = ' + _toml(v) + '\n'
                            for k, v in config.items() if v is not None), encoding='utf-8')
    path.chmod(0o600)


def _copy(source, target):
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_dir():
        shutil.copytree(source, target, symlinks=False)
    else:
        shutil.copyfile(source, target)
        target.chmod(0o600)


def _mapped_key(key, old, new):
    return str(new) + key[len(str(old)):] if key.startswith(str(old) + '/') else key


def prepare(binary, checkpoint, config, cwd):
    """Create once; never overwrite an existing reviewed configuration on resume."""
    from .checkpoints import git
    from .isolation_assets import freeze_plugins, wrap_plugins, wrap_servers
    info = config['_local_tools']
    source_home = Path(info['home']).resolve()
    directory = (root() / 'clients' / digest(str(Path(cwd).resolve()))).resolve()
    marker = directory / 'isolation.json'
    binding = digest([checkpoint['manifest_digest'], info, str(Path(binary).resolve())])
    existing = read(marker)
    if existing:
        if existing['binding'] != binding:
            raise Unavailable('rewind_configuration_changed')
        verify_files(existing)
        freeze_plugins(Path(existing['home']) / 'plugins/cache')
        return existing
    if directory.exists():
        raise Unavailable('rewind_configuration_incomplete')
    home, guard = directory / 'client', directory / 'sandbox'
    transcript = Path(checkpoint['local_config']['transcript_path']).resolve()
    if not transcript.is_relative_to(source_home / 'sessions'):
        raise Unavailable('source_transcript_not_local')
    imported = home / transcript.relative_to(source_home)
    copied = ('auth.json', 'AGENTS.md', 'skills', 'plugins/cache', 'plugins/data', 'memhub_hook_bridge.py',
              'memhub_hook_bridge.version', 'models_cache.json')
    common = Path(git(checkpoint['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    protected = [str(Path(checkpoint['repo']).resolve()), str(common.resolve()), str(source_home), str(directory)]
    # Preserve explicitly granted output directories inside CODEX_HOME. They
    # must remain disjoint from the inputs controlling execution and history.
    controls = [source_home / name for name in (*copied, 'config.toml', 'hooks.json',
                'sessions', 'archived_sessions', 'plugins', 'rules', 'packages', 'bin')]
    controls.extend(Path(h['sourcePath']) for h in info['hooks'])
    # Root-level state files (including native databases) are never output roots.
    controls.extend(p for p in source_home.iterdir() if not p.is_dir())
    controls = [p.resolve() for p in controls]
    output_grants = {}
    # Reject inherited tool grants that could overwrite the private launch configuration.
    from .codex_safety import runtime_roots
    grants = runtime_roots(config['_context'], cwd)
    policy = config['_context']['sandbox_policy']
    if policy.get('type') == 'workspace-write':
        import tempfile
        if not policy.get('exclude_slash_tmp', False): grants.append('/tmp')
        if not policy.get('exclude_tmpdir_env_var', False): grants.append(tempfile.gettempdir())
        for grant in map(lambda p: Path(p).resolve(), grants):
            if any(Path(p).is_relative_to(grant) or grant.is_relative_to(Path(p))
                   for p in protected if Path(p) != source_home):
                raise Unavailable('writable_root_exposes_rewind_configuration')
            if source_home.is_relative_to(grant) or any(
                    p.is_relative_to(grant) or grant.is_relative_to(p) for p in controls):
                raise Unavailable('writable_root_exposes_rewind_configuration')
            if grant.is_relative_to(source_home):
                output_grants[str(grant)] = 'write'
    home.mkdir(parents=True, mode=0o700)
    guard.mkdir(mode=0o700)
    for name in copied:
        _copy(source_home / name, home / name)
    _copy(transcript, imported)
    write_config(guard / 'config.toml', {'default_permissions': 'rewind', 'permissions': {'rewind': {
        'filesystem': {'/': 'write', **{p: 'read' for p in protected}, **output_grants,
                       str(home / 'plugins/data'): 'write'}, 'network': {'enabled': True}}}})
    prefix = ['/usr/bin/env', 'CODEX_HOME=' + str(guard), binary, 'sandbox', '-P', 'rewind',
              '--include-managed-config', '--', '/usr/bin/env', 'CODEX_HOME=' + str(home)]
    launcher = guard / 'launch.py'
    _copy(Path(__file__).with_name('isolation_launcher.py'), launcher)
    files = {str(launcher): digest(launcher.read_text(encoding='utf-8'))}
    effective = copy.deepcopy(info['config'])
    states = effective.setdefault('hooks', {}).get('state', {})
    effective['hooks']['state'] = {_mapped_key(k, source_home, home): {
        k2: v for k2, v in value.items() if k2 not in ('trusted_hash', 'trusted_content_hash')}
        for k, value in states.items()}
    # Rewrite original files in the copied cache so native plugin environment, order,
    # matchers, timeout and async behavior remain owned by Codex.
    for original in sorted({h['sourcePath'] for h in info['hooks']}):
        src = Path(original)
        dest = home / src.relative_to(source_home)
        if dest.is_symlink() or not dest.resolve().is_relative_to(home):
            raise Unavailable('hook_source_not_isolated')
        contents = src.read_text(encoding='utf-8')
        if digest(contents) != info['hook_files'].get(original):
            raise Unavailable('source_hooks_changed')
        data = json.loads(contents)
        for groups in data.get('hooks', {}).values():
            for group in groups:
                for handler in group.get('hooks', []):
                    if handler.get('type') != 'command' or not isinstance(handler.get('command'), str):
                        raise Unavailable('hook_confinement_unsupported')
                    encoded = base64.b64encode(handler['command'].encode('utf-8')).decode('ascii')
                    handler['command'] = shlex.join(prefix + [sys.executable, str(launcher), info['shell'], encoded])
        write(dest, data)
        files[str(dest)] = digest(dest.read_text(encoding='utf-8'))
    wrap_plugins(home / 'plugins/cache', prefix, files)
    freeze_plugins(home / 'plugins/cache')
    wrap_servers(effective.get('mcp_servers') or {}, prefix)
    if effective.get('notify'):
        effective['notify'] = prefix + effective['notify']
    write_config(home / 'config.toml', effective)
    files[str(home / 'config.toml')] = digest((home / 'config.toml').read_text(encoding='utf-8'))
    files[str(guard / 'config.toml')] = digest((guard / 'config.toml').read_text(encoding='utf-8'))
    value = {'binding': binding, 'home': str(home), 'directory': str(directory),
             'source_home': str(source_home), 'files': files, 'prefix': prefix,
             'process_config': {k: effective.get(k) for k in ('notify', 'mcp_servers', 'plugins')},
             'hooks': [{**h, 'key': _mapped_key(h['key'], source_home, home),
                        'sourcePath': _mapped_key(h['sourcePath'], source_home, home)} for h in info['hooks']]}
    # Probe a disposable sentinel, not the user's checkout, before executing any hook.
    sentinel = guard / 'canary'
    sentinel.write_text('unchanged', encoding='utf-8')
    probe = ('from pathlib import Path; import sys\n'
             'try: Path(sys.argv[1]).write_text("escaped")\n'
             'except PermissionError: sys.exit(0)\n'
             'sys.exit(1)\n')
    result = subprocess.run(prefix + [sys.executable, '-c', probe, str(sentinel)],
                            cwd=cwd, capture_output=True, timeout=20)
    if result.returncode != 0 or sentinel.read_text(encoding='utf-8') != 'unchanged':
        raise Unavailable('process_sandbox_unavailable')
    write(marker, value)
    return value


def verify_files(value):
    for path, expected in value['files'].items():
        file = Path(path)
        if file.is_symlink() or not file.is_file() or digest(file.read_text(encoding='utf-8')) != expected:
            raise Unavailable('rewind_configuration_changed')


def review(rpc, cwd, value):
    """Only an explicit human review may authorize changed wrapper commands."""
    verify_files(value)
    actual_config = rpc.request('config/read', {'cwd': str(cwd), 'includeLayers': False}).get('config', {})
    if {k: actual_config.get(k) for k in value['process_config']} != value['process_config']:
        raise Unavailable('isolated_process_configuration_changed')
    hooks = listed_hooks(rpc, cwd)
    expected = {h['key']: h for h in value['hooks']}
    if {h['key'] for h in hooks} != set(expected):
        raise Unavailable('isolated_hook_inventory_changed')
    for h in hooks:
        prior = expected[h['key']]
        if (h.get('isManaged') or h.get('handlerType') != 'command' or
                h.get('enabled') != prior.get('enabled') or h['sourcePath'] not in value['files']):
            raise Unavailable('isolated_hook_inventory_changed')
    pending = [h for h in hooks if h.get('enabled') and h.get('trustStatus') != 'trusted']
    if pending:
        print('Rewind keeps these hooks inside the source-protection sandbox. Review the changed commands:')
        for h in pending:
            print(json.dumps({'event': h['eventName'], 'source': h['sourcePath'],
                              'originalCommand': expected[h['key']]['command'], 'command': h['command'], 'hash': h['currentHash']}, ensure_ascii=True))
        if not sys.stdin.isatty() or input('Trust these commands for this rewind only? [y/N] ').strip().lower() != 'y':
            raise Unavailable('rewind_hook_review_required')
        # Re-list after review; never authorize a command that changed during the prompt.
        verify_files(value)
        now = listed_hooks(rpc, cwd)
        if [(h['key'], h['currentHash']) for h in now] != [(h['key'], h['currentHash']) for h in hooks]:
            raise Unavailable('isolated_hook_inventory_changed')
        for h in pending:
            rpc.request('config/value/write', {'keyPath': 'hooks.state.' + json.dumps(h['key']) + '.trusted_hash',
                                               'value': h['currentHash'], 'mergeStrategy': 'upsert'})
        if any(h.get('enabled') and h.get('trustStatus') != 'trusted' for h in listed_hooks(rpc, cwd)):
            raise Unavailable('rewind_hook_review_required')

    config_path = Path(value['home']) / 'config.toml'
    value['files'][str(config_path)] = digest(config_path.read_text(encoding='utf-8'))
    write(Path(value['directory']) / 'isolation.json', value)
