"""ENG-1106: normalized desktop payloads, host routing and a real hook fire."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/memhub/scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_hook_bridge as bridge
import claude_hook_guard as guard
import rulebook_hook as rb


def test_normalized_desktop_payload_and_explicit_context():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / 'repo with spaces'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git/HEAD').write_text('ref: refs/heads/main\n')
        payload = {'cwd': td, 'tool_name': 'Bash', 'session_id': 'fixture',
                   'tool_input': {'command': 'echo rulebook-probe'}}
        assert rb.repo_of_call(payload)[0] == ''  # do not guess from siblings
        command = 'cd ' + shlex.quote(str(repo)) + ' && echo rulebook-probe'
        payload['tool_input']['command'] = command
        normalized = json.loads(bridge._rulebook_payload(json.dumps(payload).encode()))
        assert rb.repo_of_call(normalized)[1] == str(repo)
        # Run the real hook engine, with an isolated fixture book and no network.
        rule = rb.to_hook_rule({'rule_id':'fixture-probe','title':'fixture-probe',
                'statement':'fixture rule fired','delivery':'agent_hook','status':'active',
                'version':1,'mode':'advise','scope_repos':[],
                'matcher':{'event':'bash','command_rx':'rulebook-probe'}})
        assert rule
        output = io.StringIO()
        with patch.object(rb, 'BASE', str(base / 'state')), \
             patch.object(rb, 'load_rules', return_value=([rule], '', rb._now(), {})), \
             patch.object(rb, 'maybe_refresh'), patch.object(rb, 'show_upgrade', return_value=False), \
             patch.object(rb, 'refresh_if_stale', side_effect=lambda repo,rules,at,sources:(rules,at,sources)), \
             patch.object(sys, 'argv', ['rulebook_hook.py','codex-pre','--host','codex']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(normalized))), \
             patch.dict(os.environ, {'MEMHUB_RULEBOOK_RECALL':'0','MEMHUB_RULEBOOK_FETCH':'0'}), \
             contextlib.redirect_stdout(output):
            assert rb.main() == 0
        assert 'fixture rule fired' in output.getvalue(), output.getvalue()
        rows = [json.loads(x) for x in (base/'state/ledger/fires.jsonl').read_text().splitlines()]
        assert len(rows) == 1 and rows[0]['host'] == 'codex'
        for cmd in ['echo x; '+command, 'cd '+shlex.quote(str(repo))+'; echo rulebook-probe',
                    'cd "$TARGET" && echo rulebook-probe']:
            payload['tool_input']['command'] = cmd
            assert rb.repo_of_call(payload)[0] == ''
        # The raw exec_command shape remains supported.
        payload.update(tool_name='exec_command', tool_input={'cmd':'echo probe','workdir':str(repo)})
        normalized=json.loads(bridge._rulebook_payload(json.dumps(payload).encode()))
        assert rb.repo_of_call(normalized)[1] == str(repo)


def test_claude_compatibility_guard_rejects_codex():
    for payload, env in [({}, {'CODEX_THREAD_ID':'fixture'}),
                         ({'transcript_path':'/home/u/.codex/sessions/2026/rollout.jsonl'}, {}),
                         ({'transcript_path':r'C:\Users\u\.codex\sessions\rollout.jsonl'}, {})]:
        assert not guard.route('capture','Stop',payload,b'{}',env)
        assert not guard.route('ignore','PreToolUse',payload,b'{}',env)
    assert guard.route('ignore','PreToolUse',{'transcript_path':'/u/.claude/projects/s.jsonl'},b'{}',{'CODEX_THREAD_ID':'parent'})
    assert guard.route('ignore','PreToolUse',{},b'{}',{'CLAUDE_PLUGIN_ROOT':'/p'})


def test_bundled_route_defers_only_for_installed_matching_user_handler():
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'CODEX_HOME':td}):
        home=Path(td); runner=home/'memhub_hook_bridge.py';runner.touch()
        doc={'hooks':{'PreToolUse':[{'matcher':'^Bash$', 'hooks':[{'type':'command',
             'command':f'python3 {runner} dispatch PreToolUse'}]}]}}
        (home/'hooks.json').write_text(json.dumps(doc))
        assert bridge._user_bridge_handles('PreToolUse',b'{"tool_name":"Bash"}')
        assert not bridge._user_bridge_handles('PreToolUse',b'{"tool_name":"Edit"}')
        assert not bridge._user_bridge_handles('Stop',b'{}')
        runner.unlink()
        assert not bridge._user_bridge_handles('PreToolUse',b'{"tool_name":"Bash"}')


def test_the_package_selects_codex_hooks_and_gives_a_projectless_notice():
    # Was asserted against plugins/memhub-staging until the staging build moved
    # to agent-plugins-internal; the shipped package makes the same promise.
    manifest=json.loads((ROOT/'plugins/memhub/.codex-plugin/plugin.json').read_text())
    assert manifest['hooks']=='./hooks/codex-hooks.json'
    assert manifest['name']=='memhub'
    with tempfile.TemporaryDirectory() as td, patch.object(sys,'argv',['hook','session','--host','codex']), \
         patch.object(sys,'stdin',io.StringIO(json.dumps({'cwd':td}))), \
         contextlib.redirect_stdout(io.StringIO()) as output:
        assert rb.main()==0
    assert 'cd <repo>' in json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']


if __name__ == '__main__':
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_'):
            fn();print('PASS',name)
