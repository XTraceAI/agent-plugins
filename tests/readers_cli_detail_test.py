"""Only the in-process root patch and diagnostic parsing needed by context tests."""
from contextlib import contextmanager
import json
from pathlib import Path
from unittest.mock import patch

from readers import codex


def codes_of(stderr):
    return [json.loads(line)['code'] for line in stderr.splitlines() if line.strip()]


@contextmanager
def rooted(home, host):
    if host != 'codex':
        raise ValueError('This synthetic context harness supports Codex only')
    home = Path(home).resolve(strict=True)
    with patch.multiple(codex, _SESSIONS=home / '.codex/sessions',
                        _SESSION_INDEX=home / '.codex/session_index.jsonl'):
        yield
