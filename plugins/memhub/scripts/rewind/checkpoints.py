"""Git-ref checkpoints: never touch the user's index, HEAD or working files."""
from __future__ import annotations
import os
import hashlib
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
from .state import Unavailable, client_id, digest, lock, read, root, write

RETENTION = 30 * 86400
QUOTA = 5 * 1024 ** 3


def git(repo, *args, env=None, data=None):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-c', 'core.untrackedCache=false', '-c', 'commit.gpgsign=false', *args], cwd=repo,
                            env=env, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=20)
    if result.returncode:
        raise Unavailable('checkpoint_git_failed')
    return result.stdout


def repository(cwd):
    repo = Path(git(cwd, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    common = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    return repo, digest(common)


def inventory(repo):
    files = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    values = {}
    for raw in sorted(set(files) - {b''}):
        relative = os.fsdecode(raw)
        path = repo / relative
        if path.is_symlink():
            # Even internal symlinks may resolve differently after checkout relocation.
            raise Unavailable('symlink_coverage_unsupported')
        if path.is_dir():
            raise Unavailable('submodule_or_nested_repository')
        try:
            stat = path.stat()
            hashed = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    hashed.update(chunk)
            values[relative] = [stat.st_size, stat.st_mtime_ns, stat.st_mode, hashed.hexdigest()]
        except FileNotFoundError:
            values[relative] = None
    return values


class Store:
    def __init__(self, cwd, quota=QUOTA, retention=RETENTION):
        self.repo, self.repo_id = repository(cwd)
        self.folder = root() / 'repositories' / self.repo_id
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.quota, self.retention = quota, retention

    def collect(self, incoming=0):
        manifests = sorted([(read(p), p) for p in self.folder.glob('*.json')],
                           key=lambda pair: pair[0]['created'])
        total = sum(m['bytes'] for m, _ in manifests)
        for manifest, path in manifests:
            expired = manifest['created'] < time.time() - self.retention
            if not manifest.get('pins') and (expired or total + incoming > self.quota):
                git(self.repo, 'update-ref', '-d', manifest['ref'], manifest['commit'])
                path.unlink()
                total -= manifest['bytes']
        if total + incoming > self.quota:
            raise Unavailable('checkpoint_quota_exhausted')
        # Quota counts conservative logical snapshot bytes; Git GC owns object reclamation.

    def capture(self, native_session_id, native_turn_id, local_config):
        with lock(self.folder / 'store.lock'):
            if git(self.repo, 'ls-files', '--unmerged'):
                raise Unavailable('unmerged_index')
            before = inventory(self.repo)
            size = sum(v[0] for v in before.values() if v)
            self.collect(size)
            base = git(self.repo, 'rev-parse', 'HEAD').decode().strip()
            checkpoint_id = str(uuid4())
            with tempfile.TemporaryDirectory(prefix='memhub-index-') as directory:
                env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index'),
                       'GIT_AUTHOR_NAME': 'MemHub local checkpoint', 'GIT_AUTHOR_EMAIL': 'local@invalid',
                       'GIT_COMMITTER_NAME': 'MemHub local checkpoint', 'GIT_COMMITTER_EMAIL': 'local@invalid'}
                git(self.repo, 'read-tree', 'HEAD', env=env)
                # Git clean filters may run external code or alter bytes; reject them.
                attrs = git(self.repo, 'check-attr', '-z', '--stdin', 'filter',
                            data=b'\0'.join(os.fsencode(k) for k in before) + b'\0')
                if any(v not in (b'unspecified', b'unset') for v in attrs.split(b'\0')[2::3]):
                    raise Unavailable('git_filter_unsupported')
                git(self.repo, 'add', '-A', '--', '.', env=env)
                tree = git(self.repo, 'write-tree', env=env).decode().strip()
                if before != inventory(self.repo):
                    raise Unavailable('workspace_changed_during_snapshot')
                commit = git(self.repo, 'commit-tree', tree, '-p', base, env=env,
                             data=b'MemHub private rewind checkpoint\n').decode().strip()
            manifest = {'protocol_version': 1, 'checkpoint_id': checkpoint_id,
                        'repository_id': self.repo_id, 'repo': str(self.repo), 'base_commit': base,
                        'tree': tree, 'commit': commit, 'ref': 'refs/memhub/checkpoints/' + checkpoint_id,
                        'native_session_id': native_session_id, 'native_turn_id': native_turn_id,
                        'client_id': client_id(), 'created': time.time(), 'bytes': size,
                        'files': before, 'local_config': local_config, 'pins': []}
            manifest['manifest_digest'] = digest({k: v for k, v in manifest.items() if k != 'pins'})
            git(self.repo, 'update-ref', manifest['ref'], commit, '0' * len(commit))
            write(self.folder / (checkpoint_id + '.json'), manifest)
            return manifest

    def pin(self, manifest, run_id):
        with lock(self.folder / 'store.lock', timeout=30, busy_code='checkpoint_store_busy'):
            path = self.folder / (manifest['checkpoint_id'] + '.json')
            current = read(path)
            if current is None:
                raise Unavailable('checkpoint_expired')
            current['pins'] = sorted(set(current['pins'] + [run_id]))
            write(path, current)

    def release(self, manifest, run_id):
        with lock(self.folder / 'store.lock', timeout=30, busy_code='checkpoint_store_busy'):
            path = self.folder / (manifest['checkpoint_id'] + '.json')
            current = read(path)
            if current:
                current['pins'] = [pin for pin in current['pins'] if pin != run_id]
                write(path, current)

    def restore(self, manifest, target):
        target = Path(target)
        if target.exists():
            raise Unavailable('restore_destination_exists')
        if git(self.repo, 'rev-parse', manifest['ref']).decode().strip() != manifest['commit']:
            raise Unavailable('checkpoint_ref_changed')
        expected = manifest['manifest_digest']
        if digest({k: v for k, v in manifest.items() if k not in ('pins', 'manifest_digest')}) != expected:
            raise Unavailable('checkpoint_manifest_changed')
        git(self.repo, 'worktree', 'add', '--detach', str(target), manifest['commit'])
        restored = inventory(target)
        supported = {k: v for k, v in manifest['files'].items() if v is not None}
        if set(restored) != set(supported) or any(restored[k][3] != v[3] for k, v in supported.items()):
            raise Unavailable('checkpoint_restore_mismatch')
        return target


def find_checkpoint(checkpoint_id):
    from .state import identifier
    matches = list((root() / 'repositories').glob('*/' + identifier(checkpoint_id) + '.json'))
    if len(matches) != 1:
        raise Unavailable('checkpoint_unavailable')
    return read(matches[0])
