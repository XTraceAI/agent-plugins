"""Compute-budget rejection must remain distinct from auth and hook timeouts."""
import asyncio
import io
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts'))
import mcp_http
import capture_health


def reply(text, error=True):
    return SimpleNamespace(isError=error, content=[SimpleNamespace(text=text)])


class BudgetTest(unittest.TestCase):
    def test_billing_rejection(self):
        message = 'Error executing tool import_conversation: Not saved: your organization has used its monthly compute budget, so MemHub cannot extract memory.'
        self.assertTrue(mcp_http.is_compute_budget_rejection(reply(message)))
        self.assertFalse(mcp_http.is_compute_budget_rejection(reply(message, False)))

    def test_other_failures(self):
        for message in ['capture ran out of time', '401 unauthorized', 'Agent brain not found', 'budget_exhausted']:
            self.assertFalse(mcp_http.is_compute_budget_rejection(reply(message)))

    def test_actionable_health(self):
        message = capture_health._message('api.memhub.xtrace.ai', None, ('compute_budget_exhausted', time.time()))
        self.assertIn('upload was not saved', message)
        self.assertIn('Settings → Billing', message)
        self.assertNotIn('/memhub:login', message)
        timeout = capture_health._message('api.memhub.xtrace.ai', None, ('budget_exhausted', time.time()))
        self.assertIn('ran out of time', timeout)


class CaptureIntegrationTest(unittest.TestCase):
    def test_session_backstop_stops_and_records_budget_cause(self):
        import flush_session
        result = reply("Not saved: your organization has used its monthly compute budget")
        session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
        with patch.object(flush_session, '_breadcrumb') as crumb, patch.object(flush_session, '_log'):
            ok, _ = asyncio.run(flush_session._send(session, {'conversation_id': 'test'}, None, None, None, 0, 1))
        self.assertFalse(ok)
        self.assertEqual(crumb.call_args.args[1], 'compute_budget_exhausted')

    def test_codex_preserves_watermark_on_budget_rejection(self):
        import codex_flush as flush
        result = reply("Not saved: your organization has used its monthly compute budget")
        record = {'type': 'user', 'uuid': 'test', 'message': {'role': 'user', 'content': 'hello'}}
        with patch.object(flush.codex_reader, 'to_canonical', return_value=([record], {'cwd': None})), \
             patch.object(flush, 'resolve_bearer', return_value=('https://example.invalid/mcp', 'test')), \
             patch.object(flush.mcp_http, 'Session', return_value=SimpleNamespace(call_tool=AsyncMock(return_value=result))), \
             patch.object(flush, '_read_state', return_value={'rollout_size': 10}), \
             patch.object(flush, '_save_state') as save, patch.object(flush, '_log'):
            asyncio.run(flush._flush('test', Path('/tmp/not-read.jsonl'), 100))
        self.assertTrue(any(c.kwargs.get('last_error') == 'compute_budget_exhausted' for c in save.call_args_list))
        self.assertFalse(any('rollout_size' in c.kwargs for c in save.call_args_list))

    def test_cursor_delivers_health_to_user_and_agent_without_blocking(self):
        import cursor_capture as capture
        output = io.StringIO()
        with patch.object(sys, 'argv', ['cursor_capture.py', 'beforeShellExecution']), \
             patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"conversation_id":"test"}'))), \
             patch.object(sys, 'stdout', output), patch.object(capture, 'spawn_cursor_flush'), \
             patch.object(capture, 'upgrade_context', return_value=None), \
             patch.object(capture, 'capture_context', return_value='Compute credits exhausted'):
            capture.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['permission'], 'allow')
        self.assertEqual(result['user_message'], 'Compute credits exhausted')
        self.assertEqual(result['agent_message'], result['user_message'])


if __name__ == '__main__':
    unittest.main()
