#!/usr/bin/env python3
"""Local integration driver. Never uses saved credentials or production origins."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--org', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--expect', choices=['supported', 'blocked'], required=True)
    args = parser.parse_args()
    url = urlsplit(args.base)
    assert url.scheme == 'http' and url.hostname == '127.0.0.1', 'loopback fixture only'
    assert os.environ.get('MEMHUB_RULEBOOK_BASE'), 'isolated cache required'
    assert not os.environ.get('MEMHUB_RULEBOOK_HOOK_VERSION'), 'use actual bundled version'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts'))
    import mcp_http
    import rulebook_hook as hook

    class Transport:
        PluginUpgradeRequired = mcp_http.PluginUpgradeRequired

        @staticmethod
        def rest(url, bearer, method, **kwargs):
            headers = dict(kwargs.pop('headers', {}), **{'X-Org-Id': args.org})
            return mcp_http.rest(url, bearer, method, headers=headers, **kwargs)

    hook._api = lambda: (args.base, 'valid_test_token', Transport)
    hook.fetch_book(args.repo)
    blocked = hook.upgrade_status(args.repo) is not None
    assert blocked == (args.expect == 'blocked'), f'unexpected policy state: {blocked}'
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        hook.show_upgrade(args.repo, 'integration-session', 'SessionStart')
    if blocked:
        assert not hook.load_rules(args.repo)[0], 'cached rules must be suspended'
        assert hook.load_book(args.repo), 'retain cache bytes for rollback'
        notice = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        assert 'PLUGIN_UPGRADE_REQUIRED' in notice and 'restart' in notice
    else:
        assert not output.getvalue()
        assert hook.load_rules(args.repo)[0], 'supported fetch should restore rules'
    print(json.dumps({'ok': True, 'blocked': blocked, 'version': hook.hook_version()}))


if __name__ == '__main__':
    main()
