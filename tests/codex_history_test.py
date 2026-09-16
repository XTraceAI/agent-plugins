#!/usr/bin/env python3
"""Paginated native history counts physical work once, including rewound tails."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import readers_cli_test as cli
from readers import codex
import readers_cli

SID = cli.SID
RID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


def row(ordinal, kind, payload):
    return {'ordinal': ordinal, 'timestamp': cli.STAMP, 'type': kind, 'payload': payload}


def message(ordinal, role, text):
    return row(ordinal, 'response_item', {'type': 'message', 'role': role,
               'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]})


def tokens(ordinal, input_count, output_count):
    return row(ordinal, 'event_msg', {'type': 'token_count', 'info': {
        'total_token_usage': {'input_tokens': input_count, 'output_tokens': output_count}}})


def fixture(home):
    folder = home/'.codex/sessions/2026/01/01'
    meta = {'id': SID, 'timestamp': cli.STAMP, 'cwd': '/synthetic/project',
            'originator': 'codex_cli', 'history_mode': 'paginated'}
    parent_rows = [row(0, 'session_meta', meta), message(1, 'user', 'shared ask'),
                   message(2, 'assistant', 'shared reply'), tokens(3, 10, 2),
                   message(4, 'user', 'abandoned ask'), message(5, 'assistant', 'abandoned reply'),
                   tokens(6, 15, 3)]
    parent = cli.write_jsonl(folder/f'rollout-2026-01-01T00-00-00-{SID}.jsonl', parent_rows)
    cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:4]))
    child_meta = {**meta, 'timestamp': '2026-01-02T00:00:00Z',
                  'history_base': {'thread_id': SID, 'end_ordinal_exclusive': 4, 'end_byte_offset': cutoff}}
    child_rows = [row(4, 'session_meta', child_meta), message(5, 'user', 'retry ask'),
                  message(6, 'assistant', 'retry reply'), tokens(7, 17, 4)]
    child = cli.write_jsonl(folder/f'rollout-2026-01-02T00-00-00-{SID}_{RID}.jsonl', child_rows)
    return parent, child, parent_rows, child_rows


def test_counts_abandoned_work_once_and_preserves_replay_and_legacy_ids():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, _, _ = fixture(home)
        before = parent.read_bytes(), child.read_bytes()
        run, rows = cli.run(home, 'codex')
        assert run.returncode == 0, run.stderr
        assert len(rows) == 7
        assert len({r['uuid'] for r in rows[1:]}) == 6
        assert rows[0]['started_at'] == cli.STAMP
        total = sum(sum(r['message'].get('usage', {}).values()) for r in rows[1:])
        assert total == 27, total  # 18 physical parent + 9 new child; inherited 12 counted once.
        old, _ = codex.to_canonical(parent, strict=True)
        assert [r['uuid'] for r in rows[1:5]] == [r['uuid'] for r in old]
        for args in [(), ('--session', SID), ('--session', 'latest')]:
            replay, again = cli.run(home, 'codex', *args)
            assert replay.returncode == 0 and rows == again, replay.stderr
        metadata, headers = cli.run(home, 'codex', '--metadata-only')
        assert metadata.returncode == 0 and len(headers) == 1
        assert before == (parent.read_bytes(), child.read_bytes())


def test_siblings_follow_native_time_and_latest_continuation_title():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, original, continuation = fixture(home)
        # The later sibling sorts before the older one by UUID and filename.
        sibling = json.loads(json.dumps(continuation))
        sibling[0]['payload']['timestamp'] = '2026-01-02T02:00:00+01:00'
        sibling[1] = message(5, 'user', 'later ask')
        sibling[2] = message(6, 'assistant', 'later reply')
        continuation.append(row(8, 'event_msg', {'type':'thread_name_updated', 'thread_name':'Earlier title'}))
        sibling.append(row(8, 'event_msg', {'type':'thread_name_updated', 'thread_name':'Latest title'}))
        cli.write_jsonl(child, continuation)
        cli.write_jsonl(parent.parent/f'rollout-2026-01-01T00-00-00-{SID}_00000000-0000-4000-8000-000000000001.jsonl', sibling)
        result, rows = cli.run(home, 'codex')
        assert result.returncode == 0, result.stderr
        assert rows[0]['title'] == 'Latest title', rows[0]
        contents = [(r['message']['content'] if isinstance(r['message']['content'], str) else r['message']['content'][0]['text']) for r in rows[1:] if r['message']['content']]
        assert contents == ['shared ask', 'shared reply', 'abandoned ask', 'abandoned reply',
                            'retry ask', 'retry reply', 'later ask', 'later reply'], contents
        replay, again = cli.run(home, 'codex')
        assert replay.returncode == 0 and rows == again
        # A native title wins even over a malformed fallback index.
        (home/'.codex/session_index.jsonl').write_text('not json\n')
        result, again = cli.run(home, 'codex')
        assert result.returncode == 0 and rows == again, result.stderr


def test_group_title_uses_sidecar_only_without_native_updates():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        fixture(home)
        cli.write_jsonl(home/'.codex/session_index.jsonl', [{'id':SID, 'thread_name':'Sidecar title'}])
        result, rows = cli.run(home, 'codex')
        assert result.returncode == 0 and rows[0]['title'] == 'Sidecar title', (result.stderr, rows)


def test_explicit_paths_read_the_whole_group_inside_and_outside_native_root():
    for exported in [False, True]:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, child, _, _ = fixture(home)
            if exported:
                folder = home/'export'
                folder.mkdir()
                parent = parent.rename(folder/parent.name)
                child = child.rename(folder/child.name)
            else:
                folder = child.parent/'another-date'
                folder.mkdir()
                child = child.rename(folder/child.name)
            outputs = []
            for path in [parent, child]:
                result, records = cli.run(home, 'codex', '--session', str(path))
                assert result.returncode == 0 and len(records) == 7, (result.stderr, len(records))
                outputs.append(records)
            assert outputs[0] == outputs[1]
            parent.unlink()
            result, records = cli.run(home, 'codex', '--session', str(child))
            assert result.returncode == 2 and not records


def test_explicit_group_rejects_symlinked_continuations():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, _, _ = fixture(home)
        hidden = child.rename(home/'hidden.jsonl')
        child.symlink_to(hidden)
        result, records = cli.run(home, 'codex', '--session', str(parent))
        assert result.returncode == 2 and not records, result.stderr


def test_missing_or_invalid_references_never_emit_a_partial_group():
    for scenario in ('missing', 'byte-boundary', 'ordinal-boundary', 'duplicate', 'cycle'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, child, original, continuation = fixture(home)
            base = continuation[0]['payload']['history_base']
            if scenario == 'missing':
                parent.unlink()
            elif scenario == 'byte-boundary':
                base['end_byte_offset'] += 1
                cli.write_jsonl(child, continuation)
            elif scenario == 'ordinal-boundary':
                base['end_ordinal_exclusive'] += 1
                cli.write_jsonl(child, continuation)
            elif scenario == 'duplicate':
                cli.write_jsonl(parent.parent/'copy'/parent.name, original)
            else:
                original[0]['payload']['history_base'] = {
                    'thread_id': RID, 'end_ordinal_exclusive': 0, 'end_byte_offset': 0}
                cli.write_jsonl(parent, original)
            result, rows = cli.run(home, 'codex')
            assert result.returncode == 2 and not rows, (scenario, result.stderr)


def test_unknown_inherited_usage_does_not_invent_a_first_delta():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, original, continuation = fixture(home)
        original[3]['payload']['info']['total_token_usage'] = None
        cli.write_jsonl(parent, original)
        continuation[0]['payload']['history_base']['end_byte_offset'] = sum(
            map(len, parent.read_bytes().splitlines(keepends=True)[:4]))
        cli.write_jsonl(child, continuation)
        result, rows = cli.run(home, 'codex')
        assert result.returncode == 0, result.stderr
        assert 'usage' not in rows[-1]['message']


def test_parent_changes_after_snapshot_invalidate_the_entire_group():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, _, _ = fixture(home)
        real_snapshot = readers_cli.source_snapshot

        @contextlib.contextmanager
        def changed(path, host, revision=()):
            with real_snapshot(path, host, revision) as snapshot:
                yield snapshot
                if path == child.resolve():
                    with parent.open('ab') as output:
                        output.write(b'\n')

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(codex, '_SESSIONS', home/'.codex/sessions'), \
                patch.object(readers_cli, 'source_snapshot', changed), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = readers_cli.main(['--host', 'codex'])
        assert status == 2 and not output.getvalue(), output.getvalue()
        assert 'source_changed' in errors.getvalue(), errors.getvalue()


def ledger(ordinal, response, input_count, output_count):
    return row(ordinal, 'token_usage_record', {'thread_id': SID, 'response_id': response,
               'usage': {'input_tokens': input_count, 'output_tokens': output_count}})


def test_native_ledger_counts_meter_resets_and_deduplicates_responses():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, original, continuation = fixture(home)
        original = [original[0], row(1, 'turn_context', {'model': 'fixture-one'}),
                    message(2, 'user', 'shared ask'), message(3, 'assistant', 'shared reply'),
                    ledger(4, 'response-original', 10, 2), tokens(5, 10, 2),
                    message(6, 'user', 'abandoned ask'), message(7, 'assistant', 'abandoned reply'),
                    ledger(8, 'response-abandoned', 5, 1), tokens(9, 5, 1)]
        cli.write_jsonl(parent, original)
        cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:6]))
        meta = continuation[0]['payload']
        meta['history_base'] = {'thread_id': SID, 'end_ordinal_exclusive': 6, 'end_byte_offset': cutoff}
        continuation = [row(6, 'session_meta', meta), row(7, 'turn_context', {'model': 'fixture-two'}),
                        message(8, 'user', 'retry ask'), message(9, 'assistant', 'retry reply'),
                        ledger(10, 'response-retry', 7, 2), tokens(11, 7, 2),
                        ledger(12, 'response-original', 10, 2)]
        cli.write_jsonl(child, continuation)
        result, records = cli.run(home, 'codex')
        assert result.returncode == 0, result.stderr
        assert len(records) == 7
        assert sum(sum(r['message'].get('usage', {}).values()) for r in records[1:]) == 27
        legacy, _ = codex.to_canonical(parent, strict=True)
        assert [r['uuid'] for r in records[1:5]] == [r['uuid'] for r in legacy]
        assert {r['api_message_id'] for r in records[1:] if 'api_message_id' in r} == {
            'response-original', 'response-abandoned', 'response-retry'}
        assert records[-1]['message']['model'] == 'fixture-two'
        replay, again = cli.run(home, 'codex')
        assert replay.returncode == 0 and records == again


def test_ledger_preserves_legacy_prefix_and_usage_only_identities():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, original, _ = fixture(home)
        child.unlink()
        # A legacy-only request precedes the introduction of response ledgers.
        original = [original[0], message(1, 'assistant', 'legacy answer'), tokens(2, 2, 1),
                    message(3, 'assistant', 'modern answer'), ledger(4, 'modern-response', 10, 2),
                    tokens(5, 12, 3), ledger(6, 'empty-response', 4, 1), tokens(7, 16, 4)]
        cli.write_jsonl(parent, original)
        legacy, _ = codex.to_canonical(parent, strict=True)
        result, records = cli.run(home, 'codex')
        assert result.returncode == 0, result.stderr
        assert [r['uuid'] for r in records[1:]] == [r['uuid'] for r in legacy]
        assert sum(sum(r['message'].get('usage', {}).values()) for r in records[1:]) == 20
        assert records[-1]['api_message_id'] == 'empty-response'


def test_conflicting_or_invalid_native_ledgers_refuse_the_group():
    for invalid in ('identity', 'conflicting-response', 'negative'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, child, original, _ = fixture(home)
            child.unlink()
            original = [original[0], message(1, 'assistant', 'answer'), ledger(2, 'r', 4, 1), tokens(3, 4, 1)]
            if invalid == 'identity': original[2]['payload']['thread_id'] = RID
            elif invalid == 'negative': original[2]['payload']['usage']['input_tokens'] = -1
            else: original.append(ledger(4, 'r', 5, 1))
            cli.write_jsonl(parent, original)
            result, records = cli.run(home, 'codex')
            assert result.returncode == 2 and not records


def test_first_ledger_without_output_does_not_charge_an_older_thinking_block():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        parent, child, original, _ = fixture(home)
        child.unlink()
        original = [original[0], row(1, 'response_item', {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'legacy thought'}]}),
                    message(2, 'assistant', 'legacy answer'), tokens(3, 2, 1),
                    ledger(4, 'new-empty-response', 10, 2), tokens(5, 12, 3)]
        cli.write_jsonl(parent, original)
        legacy, _ = codex.to_canonical(parent, strict=True)
        result, records = cli.run(home, 'codex')
        assert result.returncode == 0, result.stderr
        assert [r['uuid'] for r in records[1:]] == [r['uuid'] for r in legacy]
        assert 'usage' not in records[1]['message']
        assert records[-1]['api_message_id'] == 'new-empty-response'
        assert sum(sum(r['message'].get('usage', {}).values()) for r in records[1:]) == 15


def test_subagent_context_is_metadata_and_own_ids_and_usage_stay_stable():
    for native_ledger in [False, True]:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, child, original, _ = fixture(home)
            child.unlink()
            meta = original[0]['payload']
            meta['subagent_history_start_ordinal'] = 6
            meta['parent_thread_id'] = RID
            inherited_meta = {**meta, 'id': RID}
            inherited_meta.pop('subagent_history_start_ordinal')
            rows = [row(0, 'session_meta', meta), row(1, 'session_meta', inherited_meta),
                    message(2, 'user', 'parent ask'), message(3, 'assistant', 'parent reply'),
                    row(4, 'event_msg', {'type':'thread_name_updated','thread_name':'Parent title'}),
                    tokens(5, 100, 20), message(6, 'user', 'child ask'),
                    message(7, 'assistant', 'child reply')]
            if native_ledger:
                rows.extend([ledger(8, 'child-response', 7, 2), tokens(9, 7, 2)])
            else:
                rows.append(tokens(8, 7, 2))
            cli.write_jsonl(parent, rows)
            before, _ = codex.to_canonical(parent, strict=True, title_index={})
            result, emitted = cli.run(home, 'codex')
            assert result.returncode == 0, result.stderr
            assert [r['uuid'] for r in emitted[1:]] == [r['uuid'] for r in before]
            assert [r.get('isMeta', False) for r in emitted[1:]] == [True, True, False, False]
            assert emitted[0]['title'] == 'child ask'
            assert all('usage' not in r['message'] for r in emitted[1:] if r.get('isMeta'))
            assert sum(sum(r['message'].get('usage', {}).values()) for r in emitted[1:] if not r.get('isMeta')) == 9
            again, replay = cli.run(home, 'codex')
            assert again.returncode == 0 and emitted == replay
            # Continuations retain the child's boundary but begin after it.
            # Their legacy baseline must exclude counters in copied context.
            start = len(rows)
            continuation_meta = {**meta, 'timestamp':'2026-01-03T00:00:00Z',
                'history_base':{'thread_id':SID,'end_ordinal_exclusive':start,'end_byte_offset':parent.stat().st_size}}
            tail = [row(start,'session_meta',continuation_meta), message(start+1,'user','next ask'),
                    message(start+2,'assistant','next reply')]
            if native_ledger:
                tail.extend([ledger(start+3,'next-response',3,1),tokens(start+4,10,3)])
            else:
                tail.append(tokens(start+3,10,3))
            cli.write_jsonl(child,tail)
            result, continued = cli.run(home,'codex')
            assert result.returncode == 0, result.stderr
            assert [r['uuid'] for r in continued[1:len(emitted)]] == [r['uuid'] for r in emitted[1:]]
            assert sum(sum(r['message'].get('usage',{}).values()) for r in continued[1:] if not r.get('isMeta')) == 13


def test_subagent_boundary_and_own_metadata_are_validated():
    for boundary in [-1, True, '4', 100, None, 1]:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, child, original, _ = fixture(home)
            child.unlink()
            meta = original[0]['payload'];meta['subagent_history_start_ordinal'] = boundary
            rows = [row(0,'session_meta',meta), row(1,'session_meta',{'id':RID}), message(2,'user','child')]
            cli.write_jsonl(parent, rows)
            result, emitted = cli.run(home,'codex')
            assert result.returncode == 2 and not emitted, boundary


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('PASS', name)
    print('ALL PASS')
