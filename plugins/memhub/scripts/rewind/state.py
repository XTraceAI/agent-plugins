"""Private, durable journals shared by hooks, runner and upload retries."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import UUID, uuid4
from atomic_write import publish
from repo_allow import config_dir

class Unavailable(RuntimeError):
    """Safe machine-readable failure (never include native error text)."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def root():
    path = Path(config_dir()) / 'rewind'
    return path


def identifier(value):
    return str(UUID(str(value)))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    publish(path, json.dumps(data, sort_keys=True))
    # publish supplies atomic rename + private modes; journals also need crash durability.
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(path, *, timeout=0, busy_code='local_run_busy'):
    if os.name == 'nt':
        raise Unavailable('platform_unsupported')
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unavailable(busy_code) from None
                time.sleep(min(0.05, remaining))
        yield
    finally:
        os.close(fd)


def client_id():
    path = root() / 'client.json'
    with lock(root() / 'client.lock', timeout=5):
        value = read(path)
        if value is None:
            value = {'client_id': str(uuid4())}
            write(path, value)
        return value['client_id']


def journal_path(run_id):
    return root() / 'runs' / (identifier(run_id) + '.json')


def branch_path(native_id):
    # Native IDs are opaque; hash instead of treating them as paths.
    return root() / 'branches' / (digest(native_id) + '.json')


def capabilities_path(harness):
    """The latest native model catalog, attached to the next source capture."""
    return root() / ('capabilities-' + harness + '.json')
