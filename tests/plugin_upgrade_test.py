"""Policy errors survive cached rules and reach each supported hook adapter."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/memhub/scripts'))
import mcp_http
import rulebook_hook as hook
import codex_hook_bridge as codex
import cursor_capture as cursor


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(hook, 'BOOK_DIR', self.temp.name),
                        patch.object(hook, 'BASE', self.temp.name)]
        for p in self.patches:
            p.start()
        self.status = 426
        self.payload = {'code': 426, 'msg': 'x' * 500,
                        'data': {'error_code': 'PLUGIN_UPGRADE_REQUIRED', 'minimum_version': '999.0.0'}}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(owner.status)
                self.end_headers()
                self.wfile.write(json.dumps(owner.payload).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.api = patch.object(hook, '_api', return_value=(self.base, 'synthetic', mcp_http))
        self.api.start()
        hook._atomic_json(hook.book_path('repo'), {'rules': [], 'etag': 'old', 'fetched_at': hook._now()})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.api.stop()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_structured_error_is_parsed_beyond_old_truncation(self):
        with self.assertRaises(mcp_http.PluginUpgradeRequired) as caught:
            mcp_http.rest(self.base, 'synthetic')
        self.assertEqual(caught.exception.minimum_version, '999.0.0')
        self.assertNotIn('x' * 100, str(caught.exception))

    def test_transient_failure_preserves_notice_and_cache(self):
        original = Path(hook.book_path('repo')).read_bytes()
        hook.fetch_book('repo')
        self.assertIsNotNone(hook.upgrade_status('repo'))
        self.status = 503
        hook.fetch_book('repo')
        self.assertIsNotNone(hook.upgrade_status('repo'))
        self.assertEqual(Path(hook.book_path('repo')).read_bytes(), original)
        self.assertEqual(hook.load_rules('repo')[0], [])
        self.status = 200
        self.payload = {'code': 0, 'data': {'rules': []}}
        hook.fetch_book('repo')
        self.assertIsNone(hook.upgrade_status('repo'))

    def test_bad_426_is_not_an_upgrade_instruction(self):
        self.payload['data']['minimum_version'] = 'run arbitrary-command'
        with self.assertRaises(mcp_http.McpError) as caught:
            mcp_http.rest(self.base, 'synthetic')
        self.assertNotIsInstance(caught.exception, mcp_http.PluginUpgradeRequired)
        hook.fetch_book('repo')
        self.assertIsNone(hook.upgrade_status('repo'))

    def test_notice_is_scoped_to_backend_and_credential(self):
        hook.fetch_book('repo')
        for api in [(self.base, 'other', mcp_http), ('https://other.invalid', 'synthetic', mcp_http)]:
            with patch.object(hook, '_api', return_value=api):
                self.assertIsNone(hook.upgrade_status('repo'))

    def test_claude_notice_is_visible_once_without_denying_tools(self):
        hook.fetch_book('repo')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(hook.show_upgrade('repo', 'session', 'PreToolUse'))
            self.assertTrue(hook.show_upgrade('repo', 'session', 'PreToolUse'))
        value = json.loads(output.getvalue())
        self.assertIn('PLUGIN_UPGRADE_REQUIRED', value['hookSpecificOutput']['additionalContext'])
        self.assertIn('restart', value['systemMessage'])
        self.assertNotIn('permissionDecision', value['hookSpecificOutput'])

    def test_codex_merges_notice_with_existing_context(self):
        notice = {'hookSpecificOutput': {'additionalContext': 'PLUGIN_UPGRADE_REQUIRED'}}
        result = subprocess.CompletedProcess([], 0, json.dumps(notice).encode(), b'')
        other = subprocess.CompletedProcess([], 0, json.dumps({'hookSpecificOutput': {'additionalContext': 'existing'}}).encode(), b'')
        with patch.object(codex, '_run', return_value=result), patch.object(codex, '_directive_result', return_value=other):
            output = io.StringIO()
            with redirect_stdout(output):
                codex._dispatch(ROOT / 'plugins/memhub', b'{"cwd":"/tmp"}', 'PreToolUse')
        value = json.loads(output.getvalue())
        self.assertEqual(value['hookSpecificOutput']['additionalContext'], 'PLUGIN_UPGRADE_REQUIRED\n\nexisting')

    def test_cursor_adapter_extracts_notice(self):
        notice = {'hookSpecificOutput': {'additionalContext': 'PLUGIN_UPGRADE_REQUIRED'}}
        result = subprocess.CompletedProcess([], 0, json.dumps(notice).encode(), b'')
        with patch.object(cursor.subprocess, 'run', return_value=result) as run:
            self.assertEqual(cursor.upgrade_context(b'{"workspace_roots":["/tmp/repo"],"conversation_id":"session"}'), 'PLUGIN_UPGRADE_REQUIRED')
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['cwd'], '/tmp/repo')
        self.assertEqual(payload['session_id'], 'session')


if __name__ == '__main__':
    unittest.main()
