"""Small test runner using synthetic homes and the public Desktop fixture data."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from codex_history_test import SID, STAMP, write_jsonl

CLI = Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts/readers_cli.py'
OUTPUT_LIMITS = {'stdout': 64 * 1024 * 1024, 'stderr': 1024 * 1024}
RUN_SECONDS = 35


def run(home, host, *arguments):
    """Run the real CLI with a synthetic HOME, bounded pipes and a deadline."""
    home = Path(home).resolve(strict=True)
    environment = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home),
        'USERPROFILE': str(home), 'CODEX_HOME': str(home / '.codex'),
        'XDG_CONFIG_HOME': str(home / '.config'), 'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    command = [sys.executable, str(CLI), '--host', host, *arguments]
    output = {name: bytearray() for name in OUTPUT_LIMITS}
    deadline = time.monotonic() + RUN_SECONDS
    process = subprocess.Popen(command, cwd=home, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        with selectors.DefaultSelector() as selected:
            for name in OUTPUT_LIMITS:
                selected.register(getattr(process, name), selectors.EVENT_READ, name)
            while selected.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('Synthetic reader exceeded its test deadline')
                for key, _ in selected.select(min(remaining, 0.1)):
                    name = key.data
                    chunk = os.read(key.fd, min(65536, OUTPUT_LIMITS[name] - len(output[name]) + 1))
                    if not chunk:
                        selected.unregister(key.fileobj)
                    elif len(output[name]) + len(chunk) > OUTPUT_LIMITS[name]:
                        raise AssertionError('Synthetic reader exceeded its test output bound')
                    else:
                        output[name].extend(chunk)
        status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()
        process.stderr.close()
    result = subprocess.CompletedProcess(command, status,
        bytes(output['stdout']).decode('utf-8'), bytes(output['stderr']).decode('utf-8'))
    return result, [json.loads(line) for line in result.stdout.splitlines()]
