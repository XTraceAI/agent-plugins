"""Optional releases never gate operations; mandatory notices take precedence."""
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts'))
import plugin_updates as updates
import plugin_compatibility as compat
import plugin_version as version


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in [(updates, 'CACHE_DIR', Path(self.tmp.name) / 'releases'),
                                    (compat, 'STATE_DIR', Path(self.tmp.name) / 'status'),
                                    (updates, 'ACTIVE_PLUGIN_VERSION', '1.0.0')]:
            p = patch.object(target, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(updates, '_plugin_root', return_value=Path('/plugins/memhub'))
        p.start(); self.addCleanup(p.stop)

    def test_newer_is_informational_and_cached(self):
        with patch.object(updates, '_fetch', return_value={'version': '1.1.0'}) as fetch:
            for _ in range(2):
                message = updates.available_message('codex')
                self.assertIn('update available', message)
                self.assertIn('continues working', message)
                self.assertNotIn('PLUGIN_UPGRADE_REQUIRED', message)
            fetch.assert_called_once()

    def test_equal_older_and_invalid_are_silent(self):
        for v in ['1.0.0', '0.9.0', 'bad']:
            with self.subTest(v=v), patch.object(updates, '_fetch', return_value={'version': v}):
                for p in updates.CACHE_DIR.glob('*'): p.unlink()
                self.assertIsNone(updates.available_message('codex'))

    def test_claude_uses_published_pin_not_main_version(self):
        sha = 'a' * 40
        catalog = {'plugins': [{'name': 'memhub', 'source': {
            'url': 'https://github.com/XTraceAI/agent-plugins.git', 'sha': sha}}]}
        with patch.object(updates, '_fetch', side_effect=[catalog, {'version': '1.0.0'}]) as fetch:
            self.assertIsNone(updates.available_message('claude-code'))
            self.assertEqual(fetch.call_args.args[0], sha + '/plugins/memhub/.claude-plugin/plugin.json')

    def test_offline_is_bounded_and_does_not_block(self):
        with patch.object(updates, '_fetch', side_effect=OSError('offline')) as fetch:
            self.assertIsNone(updates.available_message('codex'))
            self.assertIsNone(updates.available_message('codex'))
            fetch.assert_called_once()

    def test_compatible_startup_announces_once_per_session(self):
        with patch('_memhub_auth.resolve_bearer', return_value=('https://example.test/mcp', 'key')), \
             patch.object(compat, 'check', return_value=None), \
             patch.object(updates, 'available_message', return_value='Update available') as notice:
            self.assertEqual(compat.startup_message('cursor', 'one'), 'Update available')
            self.assertIsNone(compat.startup_message('cursor', 'one'))
            self.assertEqual(compat.startup_message('cursor', 'two'), 'Update available')
            self.assertEqual(notice.call_count, 2)

    def test_required_notice_preempts_optional_notice(self):
        url = 'https://example.test/mcp'
        compat.record(url, 'key', '9.0.0')
        with patch('_memhub_auth.resolve_bearer', return_value=(url, 'key')), \
             patch.object(updates, 'available_message') as notice:
            message = compat.startup_message('cursor', 'one')
            self.assertIn('UPDATE REQUIRED', message)
            self.assertIn('paused', message)
            self.assertIn('restart', message)
            notice.assert_not_called()

    def test_required_hook_reaches_user_and_agent(self):
        import capture_health as health
        output = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO('{"session_id":"s"}')), \
             patch.object(sys, 'stdout', output), \
             patch.object(health, '_env_host', return_value='api.memhub.xtrace.ai'), \
             patch.object(health, '_token_problem', return_value=None), \
             patch.object(health, '_recent_failure', return_value=None), \
             patch.object(health, '_rulebook_problem', return_value=None), \
             patch.object(health, '_already_warned', return_value=False), \
             patch.object(compat, 'startup_message', return_value=version.upgrade_message('9.0.0')):
            health.main()
        result = json.loads(output.getvalue())
        self.assertTrue(result['systemMessage'].startswith('🚨'))
        self.assertIn('UPDATE REQUIRED', result['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
