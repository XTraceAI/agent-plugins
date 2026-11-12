"""Claude Code adapter: before-prompt boundary, no-generation fork, interactive handoff.

The checkpoint is joined to its prompt by the UserPromptSubmit payload's
``prompt_id``, which Claude Code writes as ``promptId`` on that prompt's user
record after the synchronous hook returns. No prompt text is matched.

The fork writes the same truncated transcript the native ``--fork-session
--resume-session-at`` path writes: the source chain up to the selected
message's parent, native UUIDs kept, ``sessionId`` and ``cwd`` rebound to the
branch. The native path needs a headless model turn to fork (no approvals in
``-p``), so it cannot satisfy the core's fork-without-input contract. The
source transcript is only read. The first prompt is submitted by the
interactive ``claude --resume`` the human drives, with native approvals.
"""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from .state import Unavailable, client_id, digest

HARNESS = 'claude_code'
ADAPTER_VERSION = '1'
# The transcript layout, interactive resume and sandbox settings this adapter
# relies on were verified on this version. Older binaries fail closed.
MIN_VERSION = (2, 1, 322)
ALIASES = ('opus', 'sonnet', 'haiku')
# The CLI's --permission-mode choices. `default` is the hook's name for "no
# mode flag", and bypassPermissions is refused: the source checkout would be
# writable without a prompt.
PERMISSION_MODES = {'acceptEdits', 'auto', 'manual', 'dontAsk', 'plan'}
_PROJECT_NAME_MAX = 200


def claude_home():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def project_dir(cwd):
    """Claude Code's per-checkout transcript folder. Longer names are hashed
    natively in a way we do not reproduce, so they fail explicitly."""
    name = re.sub(r'[^A-Za-z0-9]', '-', str(Path(cwd).resolve()))
    if len(name) > _PROJECT_NAME_MAX:
        raise Unavailable('branch_path_unsupported')
    return claude_home() / 'projects' / name


def locate(session_id):
    """The source transcript, from the same store the launched CLI reads
    (CLAUDE_CONFIG_DIR is honored). One match or none; never a guess."""
    hits = list((claude_home() / 'projects').glob('*/' + str(uuid.UUID(session_id)) + '.jsonl'))
    if len(hits) > 1:
        raise Unavailable('native_session_ambiguous')
    return hits[0] if hits else None


def config_fingerprint(repo):
    home, repo = claude_home(), Path(repo)
    paths = [home / 'settings.json', home / 'CLAUDE.md', repo / '.claude' / 'settings.json',
             repo / '.claude' / 'settings.local.json', repo / '.mcp.json', repo / 'CLAUDE.local.md']
    paths += [p / 'CLAUDE.md' for p in (repo, *repo.parents)]
    return digest({str(p): digest(p.read_text(encoding='utf-8')) if p.is_file() else None for p in paths})


def local_config(payload, repo):
    """What the synchronous hook can know before the model runs. The model is
    read later from the turn's own assistant records."""
    return {'harness': HARNESS, 'fingerprint': config_fingerprint(repo),
            'transcript_path': payload['transcript_path'],
            'permission_mode': payload.get('permission_mode')}


def load(path):
    from readers import claude as reader
    return reader.load(path)


def genuine_user(record):
    if record.get('type') != 'user' or record.get('isMeta') or record.get('isSidechain'):
        return False
    content = record.get('message', {}).get('content')
    if isinstance(content, list) and any(isinstance(p, dict) and p.get('type') == 'tool_result'
                                         for p in content):
        return False
    from transcript_filter import is_command_wrapper
    return not is_command_wrapper(record)


def prompt_record(rows, prompt_id):
    """The first genuine user record of one prompt: later records sharing it
    are clarification or steering inside the old continuation."""
    for index, row in enumerate(rows):
        if row.get('promptId') == prompt_id and row.get('type') == 'user' and not row.get('isSidechain'):
            return index if genuine_user(row) else None
    return None


def turn_model(rows, index):
    prompt = rows[index].get('promptId')
    for row in rows[index + 1:]:
        if row.get('type') == 'user' and genuine_user(row) and row.get('promptId') != prompt:
            return None
        if row.get('type') == 'assistant' and not row.get('isSidechain'):
            model = row.get('message', {}).get('model')
            if model and model != '<synthetic>':
                return model
    return None


def metadata(manifest, native_event_id, version, model):
    local = manifest['local_config']
    config = {
        'harness': HARNESS, 'harness_version': version or 'unknown', 'adapter_version': ADAPTER_VERSION,
        'model_id': model,
        # Claude Code records no per-turn effort. The launch reuses the same
        # fingerprinted settings and changes no effort, so none is claimed.
        'reasoning_effort': None,
        'permission_profile_digest': digest([local.get('permission_mode'), local['fingerprint']]),
        'instruction_config_digest': local['fingerprint'], 'tool_config_digest': local['fingerprint'],
        'provider_profile_id': digest(local['fingerprint']), 'settings_known': True}
    return {'protocol_version': 1, 'checkpoint_id': manifest['checkpoint_id'],
            'client_id': manifest['client_id'], 'native_session_id': manifest['native_session_id'],
            'native_user_message_id': native_event_id, 'native_turn_id': manifest['native_turn_id'],
            'captured_at': datetime.datetime.fromtimestamp(manifest['created'], datetime.timezone.utc).isoformat(),
            'repository_id': manifest['repository_id'], 'base_commit': manifest['base_commit'],
            'manifest_digest': manifest['manifest_digest'],
            'coverage': {'status': 'supported', 'reasons': [],
                         'excluded_categories': ['ignored', 'secrets', 'process_state', 'remote_state']},
            'original_config': config}


def parse_version(text):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    return tuple(int(part) for part in match.groups()) if match else None


def observed_models(limit=50, tail=256 * 1024):
    """Models this machine's Claude Code actually ran recently. Claude Code has
    no catalog command, so these plus the native aliases are the catalog."""
    folder = claude_home() / 'projects'
    files = sorted(folder.glob('*/*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    found = set()
    for path in files:
        try:
            with path.open('rb') as handle:
                handle.seek(max(0, path.stat().st_size - tail))
                data = handle.read().decode('utf-8', errors='replace')
        except OSError:
            continue
        found.update(m for m in re.findall(r'"model":\s*"(claude-[A-Za-z0-9.\-]+)"', data))
    return sorted(found)


def sandbox_ready():
    """The native sandbox's own prerequisites (Seatbelt ships with macOS)."""
    if sys.platform == 'darwin':
        return True
    return sys.platform == 'linux' and bool(shutil.which('bwrap') and shutil.which('socat'))


def protected_paths(repo):
    from .checkpoints import git
    common = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    return sorted({str(Path(repo).resolve()), str(Path(common).resolve())})


def launch_settings(protected):
    """A --settings overlay that keeps the branch off the source checkout.

    Bash runs in the native sandbox, which fails closed and has no unsandboxed
    retry. Bash still asks as it did before, because the sandbox would
    otherwise auto-approve. Edit/Write are permission rules, not sandboxed.
    The shared Git storage stays read-only, as on Codex.
    """
    return {'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
                        'autoAllowBashIfSandboxed': False,
                        'filesystem': {'denyWrite': list(protected)}},
            'permissions': {'deny': ['Edit(/' + path + '/**)' for path in protected]}}


def exposed_configuration(protected, cwd):
    """Hooks and MCP servers run outside the sandbox. Refuse any that name the
    source checkout or its Git storage instead of running them into it."""
    home = claude_home()
    state = Path.home() / '.claude.json' if 'CLAUDE_CONFIG_DIR' not in os.environ else home / '.claude.json'
    cwd = str(Path(cwd).resolve())
    sources = [home / 'settings.json', state, Path(cwd) / '.claude' / 'settings.json',
               Path(cwd) / '.claude' / 'settings.local.json', Path(cwd) / '.mcp.json']
    for path in sources:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        sections = [value.get('hooks'), value.get('mcpServers'), value.get('statusLine')]
        if path.name == '.claude.json':
            # Per-project MCP servers are keyed by checkout path; only the
            # branch checkout's own entry, if any, applies to the launch.
            sections = [value.get('mcpServers'),
                        (value.get('projects') or {}).get(str(cwd), {}).get('mcpServers')]
        text = json.dumps(sections)
        if any(path_text in text for path_text in protected):
            raise Unavailable('configuration_targets_source')


class Claude:
    def __init__(self, binary=None):
        binary = binary or os.environ.get('MEMHUB_CLAUDE_BIN') or 'claude'
        self.binary = shutil.which(binary) or binary
        try:
            self.version = subprocess.run([self.binary, '--version'], capture_output=True, text=True,
                                          check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise Unavailable('native_binary_unavailable') from None

    def supported(self):
        version = parse_version(self.version)
        return bool(version and version >= MIN_VERSION and sys.platform in ('darwin', 'linux'))

    def capabilities(self):
        observed = observed_models()
        models = [{'model_id': m, 'display_name': m, 'supported_efforts': [],
                   'access': 'previously_succeeded'} for m in observed]
        models += [{'model_id': a, 'display_name': a, 'supported_efforts': [], 'access': 'unknown'}
                   for a in ALIASES]
        return {'protocol_version': 1, 'client_id': client_id(), 'harness': HARNESS,
                'harness_version': self.version, 'adapter_version': ADAPTER_VERSION,
                'reported_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'supports_before_user_turn': self.supported(), 'models': models[:100]}

    def resolve_boundary(self, checkpoint):
        rows = load(checkpoint['local_config']['transcript_path'])
        index = prompt_record(rows, checkpoint['native_turn_id'])
        if index is None:
            raise Unavailable('native_user_message_unavailable')
        selected = rows[index]
        if selected.get('sessionId') != checkpoint['native_session_id']:
            raise Unavailable('native_boundary_unavailable')
        content = selected.get('message', {}).get('content')
        parts = ([{'type': 'text', 'text': content}] if isinstance(content, str) else
                 [{'type': 'text', 'text': p.get('text', '')} if p.get('type') == 'text' else p
                  for p in content or []])
        if not parts or any(p.get('type') != 'text' for p in parts):
            # The interactive CLI takes the first prompt as text only. Images
            # block rather than disappearing from the replay.
            raise Unavailable('attachment_unavailable')
        by_id = {}
        for row in rows[:index]:
            if row.get('uuid') and not row.get('isSidechain'):
                by_id.setdefault(row['uuid'], row)
        chain, cursor = [], selected.get('parentUuid')
        while cursor:
            if cursor not in by_id or cursor in chain:
                raise Unavailable('native_prefix_unavailable')
            chain.append(cursor)
            cursor = by_id[cursor].get('parentUuid')
        keep = set(chain)
        prefix = [row['uuid'] for row in rows[:index] if row.get('uuid') in keep
                  and by_id[row['uuid']] is row]
        used, answered = set(), set()
        for uid in prefix:
            content = by_id[uid].get('message', {}).get('content')
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get('type') == 'tool_use':
                    used.add(part.get('id'))
                elif isinstance(part, dict) and part.get('type') == 'tool_result':
                    answered.add(part.get('tool_use_id'))
        if used - answered:
            raise Unavailable('native_thread_busy')
        return {'prefix': prefix, 'input': parts, 'selected': selected['uuid']}

    def preflight(self, checkpoint, requested_config):
        local = checkpoint['local_config']
        if local.get('harness') != HARNESS or requested_config.get('harness') != HARNESS:
            raise Unavailable('harness_mismatch')
        if not self.supported():
            raise Unavailable('native_boundary_api_unavailable')
        if local['fingerprint'] != config_fingerprint(checkpoint['repo']):
            raise Unavailable('local_configuration_changed')
        mode = local.get('permission_mode') or 'default'
        if mode != 'default' and mode not in PERMISSION_MODES:
            raise Unavailable('permission_mode_unsupported')
        if requested_config.get('reasoning_effort') is not None:
            raise Unavailable('reasoning_effort_changed')
        model = requested_config['model_id']
        if model not in {m['model_id'] for m in self.capabilities()['models']}:
            raise Unavailable('model_not_in_native_catalog')
        if not sandbox_ready():
            raise Unavailable('sandbox_unavailable')
        self.resolve_boundary(checkpoint)
        if parse_version(self.version) != parse_version(requested_config['harness_version']):
            print(f"Claude Code version differs from checkpoint: {requested_config['harness_version']} -> "
                  f"{self.version}; required capabilities verified.", file=sys.stderr)
        return {**requested_config, 'harness_version': self.version, '_permission_mode': mode,
                '_protected': protected_paths(checkpoint['repo'])}

    def prepare(self, checkpoint, config, cwd):
        exposed_configuration(config['_protected'], cwd)
        config['_boundary'] = self.resolve_boundary(checkpoint)

    def branch_file(self, cwd, native_branch_id):
        return project_dir(cwd) / (str(uuid.UUID(native_branch_id)) + '.jsonl')

    def fork(self, checkpoint, config, cwd):
        boundary = config['_boundary'] if '_boundary' in config else self.resolve_boundary(checkpoint)
        folder = project_dir(cwd)
        if folder.is_dir() and any(folder.glob('*.jsonl')):
            # A branch already exists for this checkout: reconcile, never fork twice.
            raise Unavailable('native_branch_exists')
        branch, cwd = str(uuid.uuid4()), str(Path(cwd).resolve())
        keep, lines, seen = set(boundary['prefix']), [], set()
        for row in load(checkpoint['local_config']['transcript_path']):
            uid = row.get('uuid')
            if uid in keep and uid not in seen and not row.get('isSidechain'):
                seen.add(uid)
                lines.append(json.dumps({**row, 'sessionId': branch, 'cwd': cwd}, ensure_ascii=False))
        if [json.loads(line)['uuid'] for line in lines] != boundary['prefix']:
            raise Unavailable('native_fork_prefix_mismatch')
        if lines:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / (branch + '.jsonl')
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write('\n'.join(lines) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            self.verify_branch(cwd, branch, boundary['prefix'])
        return {'native_branch_id': branch, 'input': boundary['input'], 'prefix_event_ids': boundary['prefix']}

    def verify_branch(self, cwd, native_branch_id, prefix):
        path = self.branch_file(cwd, native_branch_id)
        rows = load(path) if path.is_file() else []
        ids = [row.get('uuid') for row in rows if row.get('uuid')]
        if ids[:len(prefix)] != prefix or any(row.get('sessionId') not in (None, native_branch_id)
                                              for row in rows):
            raise Unavailable('native_fork_prefix_mismatch')

    def reconcile(self, checkpoint, journal, native_branch_id=None):
        """Attach the one branch a crashed fork may have written; never fork."""
        boundary = self.resolve_boundary(checkpoint)
        folder = project_dir(journal['cwd'])
        found = sorted(p.stem for p in folder.glob('*.jsonl')) if folder.is_dir() else []
        if native_branch_id is None:
            if len(found) != 1:
                raise Unavailable('reconciliation_requires_native_branch')
            native_branch_id = found[0]
        if native_branch_id not in found:
            raise Unavailable('native_branch_unavailable')
        self.verify_branch(journal['cwd'], native_branch_id, boundary['prefix'])
        return {'native_branch_id': native_branch_id, 'input': boundary['input'],
                'prefix_event_ids': boundary['prefix']}

    def command(self, native_branch_id, cwd, config, input_parts=None):
        argv = [self.binary]
        if self.branch_file(cwd, native_branch_id).is_file():
            argv += ['--resume', native_branch_id]
        elif input_parts is not None:
            # Selected the session's first prompt: nothing to inherit.
            argv += ['--session-id', native_branch_id]
        else:
            raise Unavailable('native_branch_unavailable')
        argv += ['--model', config['model_id'], '--settings', json.dumps(launch_settings(config['_protected']))]
        if config['_permission_mode'] != 'default':
            argv += ['--permission-mode', config['_permission_mode']]
        if input_parts is not None:
            if any(part.get('type') != 'text' for part in input_parts):
                raise Unavailable('attachment_unavailable')
            argv += ['--', '\n'.join(part['text'] for part in input_parts)]
        return argv

    def close(self):
        pass

