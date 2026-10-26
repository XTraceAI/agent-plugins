"""Bounded Codex pages keep full-reader identities, order, blocks and usage."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import readers_cli_test as cli
import readers_cli_detail_test as detail
import codex_history_test as history
import codex_context as context
import readers_cli

SID = cli.SID


def ledger(n, response, inputs=10, outputs=2):
    return history.row(n, 'token_usage_record', {'thread_id': SID, 'response_id': response,
        'usage': {'input_tokens': inputs, 'output_tokens': outputs}})


def fixture(home, count=35):
    rows = [history.row(0, 'session_meta', {'id': SID, 'timestamp': cli.STAMP,
        'cwd': '/synthetic/project', 'originator': 'codex_cli', 'history_mode': 'paginated'})]
    for n in range(count):
        rows += [history.message(len(rows), 'user', f'question {n}'),
                 history.message(len(rows) + 1, 'assistant', f'answer {n}')]
        rows.append(ledger(len(rows), f'response-{n}'))
    path = cli.write_jsonl(home / f'.codex/sessions/rollout-2026-01-01T00-00-00-{SID}.jsonl', rows)
    return path, rows


def wide_fixture(home, unrelated):
    target, rows = fixture(home, 2)
    other = []
    for n in range(unrelated):
        sid = f'22222222-3333-4444-8555-{n:012x}'
        path = target.parent / f'rollout-2026-01-01T00-00-00-{sid}.jsonl'
        cli.write_jsonl(path, [history.row(0, 'session_meta', {'id': sid,
            'timestamp': cli.STAMP, 'cwd': '/synthetic/project', 'originator': 'codex_cli'})])
        other.append(path)
    return target, rows, other


def run(home, request=None):
    request = request or {'version': 1, 'kind': 'first'}
    result, rows = cli.run(home, 'codex', '--exact-detail', '--session', SID,
                           '--context-page', json.dumps(request))
    return result, rows


def here(home, request=None):
    out, err = io.StringIO(), io.StringIO()
    with detail.rooted(home, 'codex'), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = readers_cli.main(['--host', 'codex', '--exact-detail', '--session', SID,
            '--context-page', json.dumps(request or {'version': 1, 'kind': 'first'})])
    return status, [json.loads(line) for line in out.getvalue().splitlines()], detail.codes_of(err.getvalue())


def anchor(record, block=None, expected='message', generation=None):
    return {'version': 1, 'kind': 'anchor', 'record_uuid': record['uuid'], 'block': block,
            'expected': expected, 'generation': generation}


def test_paginated_ledger_and_wrapper_records_match_full_exact_detail():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path, rows = fixture(home, 0)
        more = [history.message(0, 'user', '<recommended_plugins>hidden</recommended_plugins>'),
                history.message(0, 'user', '# AGENTS.md instructions\n\n<INSTRUCTIONS>hidden</INSTRUCTIONS>\nreal ask'),
                history.row(0, 'response_item', {'type': 'reasoning', 'summary': [{'text': 'thinking'}]}),
                history.tokens(0, 2, 1),  # protected legacy prefix
                ledger(0, 'before-output'),
                history.tokens(0, 4, 2),  # reuse this usage-only record
                history.row(0, 'turn_context', {'model': 'synthetic-model'}),
                history.row(0, 'response_item', {'type': 'function_call', 'call_id': 'call-1',
                            'name': 'read_file', 'arguments': '{"path":"synthetic.txt"}'}),
                history.row(0, 'response_item', {'type': 'function_call_output', 'call_id': 'call-1',
                            'output': 'useful tool text'}),
                ledger(0, 'tool-response', 0, 0),
                ledger(0, 'only-ledger'), ledger(0, 'only-ledger')]
        for row in more:
            row['ordinal'] = len(rows)
            rows.append(row)
        cli.write_jsonl(path, rows)
        full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        result, page = run(home)
        assert full.returncode == result.returncode == 0, (full.stderr, result.stderr)
        assert page[1:] == canonical[1:]
        assert any(r.get('api_message_id') == 'only-ledger' for r in page[1:])
        assert any(r['message'].get('usage', {}).get('input_tokens') == 0 for r in page[1:])
        for record in page[1:]:
            block = record['message']['content']
            expected = 'tool_call' if isinstance(block, list) and block and block[0]['type'] == 'tool_use' else 'message'
            selected, again = run(home, anchor(record, 0 if expected == 'tool_call' else None, expected))
            assert selected.returncode == 0, selected.stderr
            at = again[0]['context_page']['target'] - again[0]['context_page']['start']
            assert again[1:][at] == record


def test_oversized_single_root_supports_pages_late_anchor_and_context():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path, rows = fixture(home, 70)
        padding = 'synthetic padding ' * 12000
        for row in rows:
            if row['type'] == 'response_item':
                row['payload']['content'][0]['text'] += padding
        cli.write_jsonl(path, rows)
        assert path.stat().st_size > readers_cli.DETAIL_MAX_SOURCE_BYTES
        full, none = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        assert full.returncode == 2 and not none and 'detail_limit_source_bytes' in full.stderr
        # An unlimited ordinary export is the independently existing parity oracle.
        full, canonical = cli.run(home, 'codex')
        assert full.returncode == 0, full.stderr
        result, first = run(home)
        assert result.returncode == 0, result.stderr
        seen = []
        page = first
        while True:
            meta = page[0]['context_page']
            assert page[1:] == canonical[1:][meta['start']:meta['end']]
            seen += page[1:]
            if meta['end'] == meta['total']:
                break
            result, page = run(home, {'version': 1, 'kind': 'after', 'position': meta['end'],
                                     'generation': meta['digest']})
            assert result.returncode == 0, result.stderr
        assert seen == canonical[1:]
        late = canonical[-4]
        result, page = run(home, anchor(late))
        assert result.returncode == 0, result.stderr
        meta = page[0]['context_page']
        assert page[1:] == canonical[1:][meta['start']:meta['end']]
        assert page[1:][meta['target'] - meta['start']] == late
        result, before = run(home, {'version': 1, 'kind': 'before', 'position': meta['start'],
                                   'generation': meta['digest']})
        assert result.returncode == 0 and before[0]['context_page']['end'] == meta['start'], result.stderr


def test_context_retains_twelve_before_and_seven_after():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        fixture(home)
        full, records = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        result, page = run(home, anchor(records[35]))
        assert result.returncode == full.returncode == 0, result.stderr
        meta = page[0]['context_page']
        assert meta['target'] - meta['start'] >= 12 and meta['end'] - meta['target'] - 1 >= 7


def test_invalid_late_rows_and_unsupported_history_return_no_page():
    for mutation in ('ordinal', 'wrong-thread', 'conflict', 'context', 'fork', 'extra-file'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path, rows = fixture(home)
            if mutation == 'ordinal':
                rows[-1]['ordinal'] += 1
            elif mutation == 'wrong-thread':
                rows[-1]['payload']['thread_id'] = 'another-thread'
            elif mutation == 'conflict':
                rows.append(ledger(len(rows), 'response-0', 999))
            elif mutation == 'context':
                rows[0]['payload']['subagent_history_start_ordinal'] = 9
            elif mutation == 'fork':
                rows[0]['payload']['forked_from_id'] = history.RID
            else:
                cli.write_jsonl(path.parent / 'duplicate' / path.name, rows)
            cli.write_jsonl(path, rows)
            result, page = run(home)
            assert result.returncode == 2 and not page, (mutation, result.stderr)


def test_changed_sources_and_generation_are_refused():
    for mutation in ('append', 'rewrite', 'replace', 'continuation'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path, rows = fixture(home)
            status, page, codes = here(home)
            assert status == 0 and not codes
            request = {'version': 1, 'kind': 'after', 'position': 1,
                       'generation': page[0]['context_page']['digest']}
            old = context.Rows.__getitem__
            fired = False
            def changed(self, index):
                nonlocal fired
                result = old(self, index)
                if not fired:
                    fired = True
                    if mutation == 'append':
                        with path.open('a') as handle:
                            handle.write(json.dumps(history.message(len(rows), 'user', 'new')) + '\n')
                    elif mutation == 'rewrite':
                        path.write_text(path.read_text().replace('question', 'QUESTION'))
                    elif mutation == 'replace':
                        saved = path.rename(path.with_suffix('.saved'))
                        path.write_bytes(saved.read_bytes())
                    else:
                        cli.write_jsonl(path.with_name(path.stem + '_' + history.RID + '.jsonl'), rows)
                return result
            with patch.object(context.Rows, '__getitem__', changed):
                status, page, codes = here(home)
            assert status == 2 and not page, (mutation, codes)
            status, page, codes = here(home, request)
            assert status == 2 and not page, (mutation, codes)


def test_declared_bounds_and_deadline_refuse_without_content():
    for bound in ('MAX_SOURCE_BYTES', 'MAX_SCAN_BYTES', 'MAX_METADATA_BYTES', 'MAX_PASSES', 'MAX_OUTPUT_BYTES'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fixture(home)
            with patch.object(context, bound, 1):
                status, page, codes = here(home)
            assert status == 2 and not page, (bound, codes)
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        fixture(home)
        with patch.object(readers_cli, 'DETAIL_MAX_LINE_BYTES', 10):
            status, page, codes = here(home)
        assert status == 2 and not page
        result, page = cli.run(home, 'codex', '--exact-detail', '--session', SID,
            '--context-page', json.dumps({'version': 1, 'kind': 'first'}), '--deadline-seconds', '0.000001')
        assert result.returncode == 2 and not page


def test_universal_newline_pages_match_full_exact_detail():
    for delimiter in (b'\n', b'\r\n', b'\r'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path, rows = fixture(home, 2)
            path.write_bytes(delimiter.join(path.read_bytes().splitlines()) + delimiter)
            full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
            result, page = run(home)
            assert full.returncode == result.returncode == 0, (delimiter, full.stderr, result.stderr)
            assert page[1:] == canonical[1:]
            result, selected = run(home, anchor(canonical[-1]))
            assert result.returncode == 0 and selected[-1] == canonical[-1], result.stderr


def test_required_oversized_record_never_returns_an_empty_or_skipped_page():
    for size in (5*1024*1024, 1200*1024):
        for backwards in (False, True):
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                path, rows = fixture(home, 0)
                texts = ['tail', 'x'*size] if backwards else ['x'*size, 'tail']
                rows.extend(history.message(i+1, 'user' if i==0 else 'assistant', text)
                            for i, text in enumerate(texts))
                cli.write_jsonl(path, rows)
                full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
                assert full.returncode == 0, full.stderr
                # Digest is source-only, so obtain it from the small opposite edge.
                opposite = canonical[1] if backwards else canonical[-1]
                result, page = run(home, anchor(opposite))
                assert result.returncode == 0, result.stderr
                generation = page[0]['context_page']['digest']
                required = canonical[-1] if backwards else canonical[1]
                requests = [anchor(required), {'version':1,'kind':'before' if backwards else 'after',
                            'position':2 if backwards else 0,'generation':generation}]
                if not backwards:
                    requests.append({'version':1,'kind':'first'})
                for request in requests:
                    result, page = run(home, request)
                    assert result.returncode == 2 and not page and 'context_record_too_large' in result.stderr, (size, request, result.stderr)


def test_non_json_whitespace_after_selected_records_is_not_ignored():
    for raw in (b'\x0b\n', b'\x0c\n'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path, rows = fixture(home, 2)
            with path.open('ab') as handle:
                handle.write(raw)
            full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
            result, page = run(home)
            assert full.returncode == result.returncode == 2
            assert not canonical and not page


def test_ledger_ownership_is_admitted_before_maps_or_records_grow():
    from readers import codex_usage
    import copy
    # Both generated records and overlays remain unchanged when admission fails.
    for overlay in (False, True):
        rows = [history.row(0, 'turn_context', {'model':'synthetic-model'})]
        records, sources = [], {}
        if overlay:
            rows.append(history.message(1,'assistant','existing text'))
            record = {'type':'assistant','uuid':'existing','message':{'role':'assistant',
                'content':[{'type':'text','text':'existing text'}], 'usage':{'input_tokens':3}}}
            records.append(record)
            sources[id(record)] = 1
        rows.append(ledger(len(rows),'new-response'))
        seen = {}
        before = copy.deepcopy(records), dict(sources)
        def refuse(candidate, key, total):
            assert candidate['api_message_id'] == 'new-response'
            assert seen == {} and records == before[0] and sources == before[1]
            raise context.ContextRefusal('test-refusal')
        try:
            codex_usage.apply(rows,records,sources,{},session_id=SID,namespace=SID,seen=seen,
                              retention_guard=refuse)
            assert False
        except context.ContextRefusal:
            pass
        assert seen == {} and records == before[0] and sources == before[1]


def test_actual_large_ledger_metadata_refuses_before_retention_crosses_cap():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path, rows = fixture(home,0)
        for n in range(5):
            rows.append(ledger(len(rows),str(n)+'x'*(7*1024*1024)))
        cli.write_jsonl(path,rows)
        assert path.stat().st_size > context.MAX_METADATA_BYTES
        original = context.RetainedOwnership.guard
        observed = []
        def checked(self, *arguments):
            before = self.bytes,dict(self.records),dict(self.bodies)
            try:
                original(self,*arguments)
            except Exception:
                assert (self.bytes,self.records,self.bodies) == before
                observed.append(self.bytes)
                raise
            assert self.bytes <= context.MAX_METADATA_BYTES
        with patch.object(context.RetainedOwnership,'guard',checked):
            status,page,codes = here(home)
        assert status == 2 and not page and observed and 'detail_limit_output_bytes' in codes


def test_combined_response_and_record_ownership_and_delayed_usage_are_preflighted():
    from readers import codex
    import copy
    budget = readers_cli.DetailBudget()
    record = {'type':'assistant','uuid':'one','message':{'role':'assistant','content':[]}}
    response = ('thread','response-'+'x'*4000)
    total = {'input_tokens':1,'output_tokens':1}
    candidate = {**record,'api_message_id':response[1]}
    record_size = len(readers_cli.encode(candidate))+1
    response_size = len(readers_cli.encode([response,total]))+1
    cap = max(record_size,response_size)+10
    owner = context.RetainedOwnership(readers_cli,budget,lambda _:False)
    before = owner.bytes,dict(owner.records)
    with patch.object(context,'MAX_METADATA_BYTES',cap):
        try:
            owner.guard(candidate,response,total)
            assert False
        except readers_cli.DetailLimit:
            pass
    assert (owner.bytes,owner.records) == before
    rows = [history.row(0,'session_meta',{'id':SID,'timestamp':cli.STAMP}),
            history.message(1,'assistant','kept'),history.tokens(2,100,20)]
    kept=[]
    def project(value):
        value['message']['content']=[]
        kept.append(value)
    def deny_usage(value, key, total):
        assert key is total is None and 'usage' in value['message']
        assert kept and 'usage' not in kept[-1]['message']
        raise context.ContextRefusal('delayed-refusal')
    try:
        codex.rollout_to_claude_records(rows,strict=True,title_index={},record_projection=project,
                                       retention_guard=deny_usage)
        assert False
    except context.ContextRefusal:
        pass
    assert 'usage' not in kept[-1]['message']


def test_selected_record_ledger_growth_is_preflighted_before_body_ownership():
    owner=context.RetainedOwnership(readers_cli,readers_cli.DetailBudget(),lambda _:True)
    record={'type':'assistant','uuid':'selected','message':{'role':'assistant','content':[]}}
    owner.guard(record)
    before=owner.bytes,dict(owner.records),dict(owner.bodies)
    with patch.object(context,'MAX_RETAINED_BYTES',1000):
        try:
            owner.guard({**record,'api_message_id':'x'*2000},('thread','x'*2000),{'input_tokens':1})
            assert False
        except context.ContextRefusal:
            pass
    assert (owner.bytes,owner.records,owner.bodies)==before


def test_wide_history_rechecks_paths_without_rebuilding_session_rows():
    from readers import codex
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        wide_fixture(home, 1100)
        full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        assert full.returncode == 0, full.stderr
        calls, spent = [], []
        original_list, original_observe = codex.list_sessions, readers_cli.DetailBudget.observe
        def listed(*args, **kwargs):
            rows = original_list(*args, **kwargs)
            calls.append(len(spent))
            return rows
        def observed(self):
            original_observe(self)
            spent.append(self.entries)
        with patch.object(codex, 'list_sessions', listed), patch.object(readers_cli.DetailBudget, 'observe', observed):
            status, page, codes = here(home)
        assert status == 0 and not codes
        assert page[1:] == canonical[1:]
        assert len(calls) == 1
        # The unchanged tree would charge that same walk plus rows twice.
        assert calls[0] * 2 > readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES
        assert calls[0] < max(spent) <= readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES
        assert len(spent) == max(spent)  # The cumulative budget was never reset.


def test_wider_history_still_refuses_at_the_unchanged_cumulative_entry_cap():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        wide_fixture(home, 1400)
        status, page, codes = here(home)
        assert readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES == 4096
        assert status == 2 and not page and 'detail_limit_discovery_entries' in codes


def test_final_path_snapshot_refuses_added_claimants_revisions_and_root_retargets():
    for mutation in ('claimant', 'unrelated-revision', 'root-retarget'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target, rows, others = wide_fixture(home, 2)
            configured = home / '.codex/sessions'
            if mutation == 'root-retarget':
                held = home / 'original-sessions'
                configured.rename(held)
                configured.symlink_to(held, target_is_directory=True)
                (home / 'replacement-sessions').mkdir()
            original = context.Rows.__getitem__
            fired = False
            def changed(self, index):
                nonlocal fired
                value = original(self, index)
                if not fired:
                    fired = True
                    if mutation == 'claimant':
                        cli.write_jsonl(target.parent / 'copy' / target.name, rows)
                    elif mutation == 'unrelated-revision':
                        with others[0].open('a') as handle:
                            handle.write(' \n')
                    else:
                        configured.unlink()
                        configured.symlink_to(home / 'replacement-sessions', target_is_directory=True)
                return value
            with patch.object(context.Rows, '__getitem__', changed):
                status, page, codes = here(home)
            assert fired and status == 2 and not page, (mutation, codes)


def test_final_path_discovery_errors_refuse_the_entire_page():
    from readers import codex
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        fixture(home, 2)
        original = codex._rollout_files
        calls = 0
        def unreadable(on_error=None, observe=None):
            nonlocal calls
            calls += 1
            found = original(on_error=on_error, observe=observe)
            if calls == 2:
                on_error(OSError('synthetic discovery failure'))
            return found
        with patch.object(codex, '_rollout_files', unreadable):
            status, page, codes = here(home)
        assert calls == 2 and status == 2 and not page and 'discovery_incomplete' in codes


def test_final_path_revisions_check_the_existing_deadline_between_files():
    from readers import codex
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        wide_fixture(home, 2)
        original_paths, original_revision = codex._rollout_files, readers_cli.source_revision
        calls, clock = 0, [0.0]
        final = False
        def paths(on_error=None, observe=None):
            nonlocal calls, final
            calls += 1
            found = original_paths(on_error=on_error, observe=observe)
            final = calls == 2
            return found
        def revision(*args, **kwargs):
            value = original_revision(*args, **kwargs)
            if final:
                clock[0] = 31.0
            return value
        with patch.object(codex, '_rollout_files', paths), patch.object(readers_cli, 'source_revision', revision), \
                patch.object(readers_cli, 'detail_now', lambda: clock[0]):
            status, page, codes = here(home)
        assert final and status == 2 and not page and 'detail_deadline' in codes


if __name__ == '__main__':
    for name, fn in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print('PASS', name)
    print('ALL PASS')
