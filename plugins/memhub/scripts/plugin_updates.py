"""Best-effort notices for releases on the public marketplace; never a gate."""
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

import atomic_write
from _memhub_auth import _plugin_root
from plugin_version import ACTIVE_PLUGIN_VERSION, update_message

CACHE_DIR = Path.home() / '.config/memhub-plugin/releases'
RAW = 'https://raw.githubusercontent.com/XTraceAI/agent-plugins/'


def _version(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}', value):
        return tuple(map(int, value.split('.')))
    return None


def _fetch(path):
    # No MemHub credentials go to the public release endpoint.
    with urlopen(Request(RAW + path, headers={'Accept': 'application/json'}), timeout=0.75) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('release metadata too large')
    return json.loads(raw)


def available_message(host):
    # Staging is a locally copied marketplace, not a public release channel.
    if 'memhub-staging' in _plugin_root().parts or host not in {'claude-code', 'codex', 'cursor'}:
        return None
    cache = CACHE_DIR / (host + '.json')
    try:
        data = json.loads(cache.read_text(encoding='utf-8'))
        fresh = 0 <= time.time() - float(data['checked_at']) < 3600
    except (OSError, ValueError, KeyError, TypeError):
        data, fresh = {}, False
    if not fresh:
        try:
            ref = 'main'
            if host == 'claude-code':
                catalog = _fetch('main/.claude-plugin/marketplace.json')
                source = next(p['source'] for p in catalog['plugins'] if p['name'] == 'memhub')
                if source.get('url') != 'https://github.com/XTraceAI/agent-plugins.git':
                    return None
                ref = source['sha']
                if not re.fullmatch(r'[0-9a-f]{40}', ref):
                    return None
            latest = _fetch(ref + '/plugins/memhub/.claude-plugin/plugin.json')['version']
            if not _version(latest):
                return None
            data = {'version': latest, 'checked_at': time.time()}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            # Cache failures too: startup must not repeatedly retry offline.
            data = {'checked_at': time.time()}
        try:
            atomic_write.publish(cache, json.dumps(data))
        except OSError:
            pass
    latest = data.get('version')
    active = _version(ACTIVE_PLUGIN_VERSION)
    if active and _version(latest) and _version(latest) > active:
        return update_message(latest, host=host)
    return None
