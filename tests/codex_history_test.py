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


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('PASS', name)
    print('ALL PASS')
