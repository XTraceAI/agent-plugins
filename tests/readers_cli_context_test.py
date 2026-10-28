"""Bounded Codex pages keep full-reader identities, order, blocks and usage."""
import contextlib
import io
import json
import os
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


def forked(home, pairs=0, parent_pairs=0, pad=''):
    """A fork of SID's original rollout, cut after ``parent_pairs`` + 1 turns.

    The parent's prefix ends at cumulative usage (10 + p, 2 + p); its later,
    abandoned tail is far larger. The fork's own work adds (15, 4), then (5, 1)
    per extra turn, so every usage delta of the fork is its own work.
    """
    rows = [history.row(0, 'session_meta', {'id': SID, 'timestamp': cli.STAMP,
        'cwd': '/synthetic/project', 'originator': 'codex_cli', 'history_mode': 'paginated'})]
    for n in range(parent_pairs + 1):
        rows += [history.message(len(rows), 'user', f'parent ask {n} {pad}'),
                 history.message(len(rows) + 1, 'assistant', f'parent reply {n}')]
        rows.append(history.tokens(len(rows), 10 + n, 2 + n))
    cutoff = len(rows)
    rows += [history.message(cutoff, 'user', 'abandoned parent ask'), history.tokens(cutoff + 1, 9000, 900)]
    parent = cli.write_jsonl(home / f'.codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-{SID}.jsonl', rows)
    path, fork_rows = history.fork_fixture(home, parent, rows, cutoff)
    seed = 10 + parent_pairs, 2 + parent_pairs
    fork_rows[3] = history.tokens(fork_rows[3]['ordinal'], seed[0] + 15, seed[1] + 4)
    for n in range(pairs):
        fork_rows += [history.message(fork_rows[-1]['ordinal'] + 1, 'user', f'fork question {n}'),
                      history.message(fork_rows[-1]['ordinal'] + 2, 'assistant', f'fork answer {n}')]
        fork_rows.append(history.tokens(fork_rows[-1]['ordinal'] + 1,
                                        seed[0] + 20 + 5 * n, seed[1] + 5 + n))
    cli.write_jsonl(path, fork_rows)
    return parent, path


def run(home, request=None, sid=SID):
    request = request or {'version': 1, 'kind': 'first'}
    result, rows = cli.run(home, 'codex', '--exact-detail', '--session', sid,
                           '--context-page', json.dumps(request))
    return result, rows


def here(home, request=None, sid=SID):
    out, err = io.StringIO(), io.StringIO()
    with detail.rooted(home, 'codex'), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = readers_cli.main(['--host', 'codex', '--exact-detail', '--session', sid,
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


def pages(home, sid, canonical):
    """Every page of ``sid`` in order, each checked against ``canonical``."""
    result, page = run(home, sid=sid)
    seen = []
    while True:
        assert result.returncode == 0, result.stderr
        meta = page[0]['context_page']
        assert {k: v for k, v in page[0].items() if k != 'context_page'} == canonical[0]
        assert page[1:] == canonical[1:][meta['start']:meta['end']]
        seen += page[1:]
        if meta['end'] == meta['total']:
            return seen, meta
        result, page = run(home, {'version': 1, 'kind': 'after', 'position': meta['end'],
                                  'generation': meta['digest']}, sid)


def test_fork_pages_match_full_exact_detail_and_count_only_own_usage():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        forked(home, pairs=110, parent_pairs=3)
        full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', history.FORK)
        assert full.returncode == 0, full.stderr
        seen, meta = pages(home, history.FORK, canonical)
        assert seen == canonical[1:] and meta['total'] == len(seen) > context.MAX_RECORDS
        assert not any('parent' in json.dumps(record) for record in seen)
        usage = [record['message']['usage'] for record in seen if 'usage' in record['message']]
        assert (usage[0]['input_tokens'], usage[0]['output_tokens']) == (15, 4)
        assert all((u['input_tokens'], u['output_tokens']) == (5, 1) for u in usage[1:])
        late = canonical[-3]
        result, page = run(home, anchor(late, generation=meta['digest']), history.FORK)
        assert result.returncode == 0, result.stderr
        at = page[0]['context_page']
        assert page[1:][at['target'] - at['start']] == late


def test_fork_without_its_resolved_parent_prefix_returns_no_page():
    from readers import codex_history
    for mutation in ('missing', 'unresolved', 'truncated', 'appended-during-read',
                     'shrunk-during-read', 'source-cap', 'line-cap'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            parent, fork = forked(home, parent_pairs=2, pad='p' * 5000 if mutation == 'line-cap' else '')
            cutoff = json.loads(fork.read_text().splitlines()[0])['payload']['history_base']['end_byte_offset']
            patches, fired = [], []
            if mutation == 'missing':
                parent.unlink()
            elif mutation == 'unresolved':
                patches.append(patch.object(codex_history, 'references', lambda *args: []))
            elif mutation == 'truncated':
                with parent.open('r+b') as handle:
                    handle.truncate(cutoff - 1)
            elif mutation.endswith('during-read') or mutation == 'line-cap':
                original = context.Prefix.readline
                def changed(self):
                    if mutation == 'line-cap':
                        try:
                            return original(self)
                        except readers_cli.DetailLimit:
                            fired.append(True)
                            raise
                    if not fired:
                        fired.append(True)
                        if mutation.startswith('appended'):
                            with parent.open('ab') as handle:
                                handle.write(b'{}\n')
                        else:
                            with parent.open('r+b') as handle:
                                handle.truncate(cutoff - 1)
                    return original(self)
                patches.append(patch.object(context.Prefix, 'readline', changed))
            elif mutation == 'source-cap':
                patches.append(patch.object(context, 'MAX_SOURCE_BYTES',
                                            fork.stat().st_size + parent.stat().st_size - 1))
            if mutation == 'line-cap':
                patches.append(patch.object(readers_cli, 'DETAIL_MAX_LINE_BYTES', 4096))
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                status, page, codes = here(home, sid=history.FORK)
            assert status == 2 and not page, (mutation, codes)
            expected = {'missing': 'discovery_incomplete', 'unresolved': 'detail_prerequisite_unsupported',
                        'truncated': 'session_unreadable', 'appended-during-read': 'source_changed',
                        'shrunk-during-read': 'source_changed', 'source-cap': 'detail_limit_source_bytes',
                        'line-cap': 'detail_limit_line_bytes'}[mutation]
            assert expected in codes, (mutation, codes)
            # The change or the line limit was met while the prefix was read.
            assert fired or mutation not in ('appended-during-read', 'shrunk-during-read', 'line-cap')


def test_fork_reads_a_large_parent_prefix_forward_without_holding_it():
    """A parent past the whole-read cap; XTRACE_FORK_PARENT_MB raises its size."""
    import time
    import tracemalloc
    size = int(os.environ.get('XTRACE_FORK_PARENT_MB', '26')) * 1024 * 1024
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        # About 3 KB per turn: many ordinary rows, not a few huge ones.
        pad = 'x' * 2048
        parent, fork = forked(home, pairs=3, parent_pairs=size // (len(pad) + 512) + 1, pad=pad)
        assert parent.stat().st_size > max(size, readers_cli.DETAIL_MAX_SOURCE_BYTES)
        whole, none = cli.run(home, 'codex', '--exact-detail', '--session', history.FORK)
        assert whole.returncode == 2 and not none and 'detail_limit_source_bytes' in whole.stderr
        # The unlimited ordinary export is the parity oracle past the whole-read cap.
        export, canonical = cli.run(home, 'codex', '--session', history.FORK)
        assert export.returncode == 0, export.stderr
        reads = []
        original = context.Window.readinto
        def counted(self, buffer):
            read = original(self, buffer)
            reads.append(read)
            return read
        tracemalloc.start()
        try:
            began = time.monotonic()
            with patch.object(context.Window, 'readinto', counted):
                status, page, codes = here(home, sid=history.FORK)
            elapsed = time.monotonic() - began
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert status == 0 and not codes, codes
        assert page[1:] == canonical[1:]
        assert max(reads) <= context.PREFIX_BUFFER_BYTES
        # Two passes read the prefix forward; neither holds it.
        assert sum(reads) >= 2 * parent.stat().st_size * 0.9
        assert peak < parent.stat().st_size / 4, peak
        print(f'  large parent {parent.stat().st_size / 2**20:.1f} MiB: page in {elapsed:.2f} s '
              f'(traced), peak traced {peak / 2**20:.1f} MiB')


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
        _, _, others = wide_fixture(home, 1100)
        full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        assert full.returncode == 0, full.stderr
        calls, spent, budgets = [], [], []
        original_list, original_observe = codex.list_sessions, readers_cli.DetailBudget.observe
        original_get = context.Rows.__getitem__
        fired = False
        def listed(*args, **kwargs):
            rows = original_list(*args, **kwargs)
            calls.append(len(spent))
            return rows
        def observed(self):
            original_observe(self)
            spent.append(self.entries)
            budgets.append(self)
        def get(self, index):
            nonlocal fired
            value = original_get(self, index)
            if not fired:
                fired = True
                with others[0].open('a') as handle:
                    handle.write(' \n')
            return value
        with patch.object(codex, 'list_sessions', listed), patch.object(readers_cli.DetailBudget, 'observe', observed), \
                patch.object(context.Rows, '__getitem__', get):
            status, page, codes = here(home)
        assert fired and status == 0 and not codes
        assert page[1:] == canonical[1:]
        assert len(calls) == 1
        # The unchanged tree would charge that same walk plus rows twice.
        assert calls[0] * 2 > readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES
        assert calls[0] < max(spent) <= readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES
        assert len(spent) == max(spent)  # The cumulative budget was never reset.
        assert budgets[0].probes == 1102 < readers_cli.DETAIL_MAX_PROBES


def test_wider_history_still_refuses_at_the_unchanged_cumulative_entry_cap():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        wide_fixture(home, 1400)
        status, page, codes = here(home)
        assert readers_cli.DETAIL_MAX_DISCOVERY_ENTRIES == 4096
        assert status == 2 and not page and 'detail_limit_discovery_entries' in codes


def test_final_path_snapshot_refuses_added_claimants_and_root_retargets():
    for mutation in ('claimant', 'root-retarget'):
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
                    else:
                        configured.unlink()
                        configured.symlink_to(home / 'replacement-sessions', target_is_directory=True)
                return value
            with patch.object(context.Rows, '__getitem__', changed):
                status, page, codes = here(home)
            assert fired and status == 2 and not page, (mutation, codes)


def test_foreign_appends_preserve_selected_records_at_each_probe_phase_and_root_alias():
    for alias in (False, True):
        for phase in ('before-open', 'before-probe', 'after-probe', 'selected-body'):
            with tempfile.TemporaryDirectory() as td:
                home = Path(td)
                target, _, others = wide_fixture(home, 1)
                if alias:
                    configured = target.parent
                    held = home / 'held-sessions'
                    configured.rename(held)
                    configured.symlink_to(held, target_is_directory=True)
                foreign = others[0].resolve()
                original_bytes = target.read_bytes()
                full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
                assert full.returncode == 0, full.stderr
                old_probe, old_get = readers_cli.probe_header, context.Rows.__getitem__
                old_revision = readers_cli.source_revision
                fired, proofs = False, []
                foreign_revisions = 0
                def append():
                    nonlocal fired
                    if not fired:
                        fired = True
                        with foreign.open('a') as handle:
                            handle.write(json.dumps(history.message(1, 'assistant', 'foreign tail')) + '\n')
                def revision(path, *args, **kwargs):
                    nonlocal foreign_revisions
                    value = old_revision(path, *args, **kwargs)
                    if Path(path).resolve() == foreign:
                        foreign_revisions += 1
                        if foreign_revisions == 2 and phase == 'before-open':
                            append()
                    return value
                def probe(reader, handle, observed, budget, proof=None):
                    is_foreign = observed.st_ino == foreign.stat().st_ino
                    if is_foreign and phase == 'before-probe':
                        append()
                    value = old_probe(reader, handle, observed, budget, proof)
                    if is_foreign:
                        proofs.append((dict(proof), budget, dict(budget.probed)))
                        if phase == 'after-probe':
                            append()
                    return value
                def get(self, index):
                    value = old_get(self, index)
                    if phase == 'selected-body':
                        append()
                    return value
                with patch.object(readers_cli, 'probe_header', probe), patch.object(context.Rows, '__getitem__', get), \
                        patch.object(readers_cli, 'source_revision', revision):
                    status, page, codes = here(home)
                assert fired and status == 0 and not codes, (alias, phase, codes)
                assert page[1:] == canonical[1:] and target.read_bytes() == original_bytes
                proof, budget, stamps = proofs[0]
                assert proof['length'] > 0 and len(proof['digest']) == 32
                assert budget.probes == 3  # Two initial headers, one changed foreign prefix.
                assert all(budget.probed[key] == stamp for key, stamp in stamps.items())
                if phase != 'before-open':
                    assert budget.probed[proof['identity']] != readers_cli.file_stamp(foreign.stat())


def test_changed_foreign_identity_header_and_prefix_fail_before_output():
    for mutation in ('header', 'prefix-body', 'replace', 'truncate', 'remove', 'unreadable', 'alias'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, _, others = wide_fixture(home, 1)
            foreign = others[0]
            with foreign.open('a') as handle:
                handle.write(json.dumps(history.message(1, 'assistant', 'x' * 20000)) + '\n')
            old_get = context.Rows.__getitem__
            fired = False
            def get(self, index):
                nonlocal fired
                value = old_get(self, index)
                if not fired:
                    fired = True
                    if mutation in ('header', 'prefix-body'):
                        stamp = foreign.stat()
                        data = foreign.read_bytes()
                        data = (data.replace(others[0].stem[-36:].encode(), SID.encode(), 1)
                                if mutation == 'header' else data.replace(b'xxxxxxxx', b'XXXXXXXX', 1))
                        foreign.write_bytes(data)
                        os.utime(foreign, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    elif mutation == 'replace':
                        saved = foreign.rename(foreign.with_suffix('.saved'))
                        foreign.write_bytes(saved.read_bytes())
                    elif mutation == 'truncate':
                        foreign.write_bytes(foreign.read_bytes().splitlines(keepends=True)[0])
                    elif mutation == 'remove':
                        foreign.unlink()
                    elif mutation == 'unreadable':
                        foreign.unlink()
                        foreign.mkdir()
                    else:
                        saved = foreign.rename(foreign.with_suffix('.saved'))
                        foreign.symlink_to(saved)
                return value
            with patch.object(context.Rows, '__getitem__', get):
                status, page, codes = here(home)
            assert fired and status == 2 and not page, (mutation, codes)


def test_selected_change_between_original_snapshot_and_probe_is_refused():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        target, rows = fixture(home, 2)
        original = readers_cli.source_revision
        fired = False
        def revision(path, *args, **kwargs):
            nonlocal fired
            value = original(path, *args, **kwargs)
            if Path(path).resolve() == target.resolve() and not fired:
                fired = True
                with target.open('a') as handle:
                    handle.write(json.dumps(history.message(len(rows), 'user', 'selected append')) + '\n')
            return value
        with patch.object(readers_cli, 'source_revision', revision):
            status, page, codes = here(home)
        assert fired and status == 2 and not page and 'source_changed' in codes


def test_foreign_shrink_uses_the_larger_initial_or_probe_observation():
    for larger in ('initial', 'probe'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, _, others = wide_fixture(home, 1)
            foreign = others[0]
            with foreign.open('a') as handle:
                handle.write(json.dumps(history.message(1, 'assistant', 'x' * 20000)) + '\n')
            initial = foreign.read_bytes()
            old_revision, old_get = readers_cli.source_revision, context.Rows.__getitem__
            revisions, fired = 0, False
            def revision(path, *args, **kwargs):
                nonlocal revisions
                value = old_revision(path, *args, **kwargs)
                if Path(path).resolve() == foreign.resolve():
                    revisions += 1
                    if revisions == 2:
                        if larger == 'initial':
                            foreign.write_bytes(initial.splitlines(keepends=True)[0])
                        else:
                            with foreign.open('a') as handle:
                                handle.write(' ' * 10000 + '\n')
                return value
            def get(self, index):
                nonlocal fired
                value = old_get(self, index)
                if not fired:
                    fired = True
                    if larger == 'probe':
                        foreign.write_bytes(initial)  # Prefix holds, but probe-observed tail disappeared.
                return value
            with patch.object(readers_cli, 'source_revision', revision), patch.object(context.Rows, '__getitem__', get):
                status, page, codes = here(home)
            assert revisions >= 2 and status == 2 and not page and 'source_changed' in codes, (larger, codes)


def test_observed_foreign_shrink_cannot_be_hidden_by_regrowth():
    for phase in ('preparation', 'probe'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, _, others = wide_fixture(home, 1)
            foreign = others[0].resolve()
            with foreign.open('a') as handle:
                handle.write(json.dumps(history.message(1, 'assistant', 'x' * 20000)) + '\n')
            initial = foreign.read_bytes()
            shorter = initial.splitlines(keepends=True)[0]
            old_revision, old_source = readers_cli.source_revision, readers_cli.regular_source
            revisions, opens, fired = 0, 0, False
            def revision(path, *args, **kwargs):
                nonlocal revisions, fired
                value = old_revision(path, *args, **kwargs)
                if Path(path).resolve() == foreign and phase == 'preparation':
                    revisions += 1
                    if revisions == 1:
                        foreign.write_bytes(shorter)
                    elif revisions == 2:
                        fired = True
                        foreign.write_bytes(initial)
                return value
            @contextlib.contextmanager
            def source(path, *args, **kwargs):
                nonlocal opens, fired
                is_foreign = Path(path).resolve() == foreign
                if is_foreign:
                    opens += 1
                if is_foreign and opens == 3 and phase == 'probe':
                    foreign.write_bytes(shorter)
                with old_source(path, *args, **kwargs) as observed:
                    if is_foreign and opens == 3 and phase == 'probe':
                        fired = True
                        foreign.write_bytes(initial)
                    yield observed
            with patch.object(readers_cli, 'source_revision', revision), \
                    patch.object(readers_cli, 'regular_source', source):
                status, page, codes = here(home)
            assert fired and foreign.read_bytes() == initial
            assert status == 2 and not page and 'source_changed' in codes, (phase, codes)


def test_foreign_growth_during_prefix_recheck_keeps_selected_canonical_parity():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        _, _, others = wide_fixture(home, 1)
        foreign = others[0]
        full, canonical = cli.run(home, 'codex', '--exact-detail', '--session', SID)
        assert full.returncode == 0, full.stderr
        old_get, old_read = context.Rows.__getitem__, readers_cli.ProbeReader.readinto
        fired, rechecked = False, False
        def append():
            with foreign.open('a') as handle:
                handle.write(' \n')
        def get(self, index):
            nonlocal fired
            value = old_get(self, index)
            if not fired:
                fired = True
                append()
            return value
        def read(self, buffer):
            nonlocal rechecked
            value = old_read(self, buffer)
            if self.budget.probes == 3 and not rechecked:
                rechecked = True
                append()
            return value
        with patch.object(context.Rows, '__getitem__', get), patch.object(readers_cli.ProbeReader, 'readinto', read):
            status, page, codes = here(home)
        assert fired and rechecked and status == 0 and not codes
        assert page[1:] == canonical[1:]


def test_context_preserves_every_saved_identity_and_nondecreasing_size_observation():
    for mutation in ('prepared-growth-then-shrink', 'saved-final-shrink',
                     'saved-final-replacement', 'post-prefix-growth-then-shrink'):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, _, others = wide_fixture(home, 1)
            foreign = others[0].resolve()
            header = foreign.read_bytes()
            middle, large = header + b' ' * 10000 + b'\n', header + b' ' * 20000 + b'\n'
            if mutation != 'prepared-growth-then-shrink':
                foreign.write_bytes(large)
            saved = foreign.with_suffix('.saved')
            old_revision, old_source = readers_cli.source_revision, readers_cli.regular_source
            old_get, old_read = context.Rows.__getitem__, readers_cli.ProbeReader.readinto
            revisions, fired, restored, prefix_read = 0, False, False, False
            def revision(path, *args, **kwargs):
                nonlocal revisions, fired, restored
                value = old_revision(path, *args, **kwargs)
                if Path(path).resolve() == foreign:
                    revisions += 1
                    if mutation == 'prepared-growth-then-shrink':
                        if revisions == 1:
                            foreign.write_bytes(large)
                        elif revisions == 2:
                            fired = True
                            foreign.write_bytes(middle)
                    elif revisions == 3 and mutation.startswith('saved-final'):
                        restored = True
                        if mutation == 'saved-final-shrink':
                            foreign.write_bytes(large)
                        else:
                            foreign.unlink()
                            saved.rename(foreign)
                return value
            def get(self, index):
                nonlocal fired
                value = old_get(self, index)
                if not fired and mutation != 'prepared-growth-then-shrink':
                    fired = True
                    if mutation == 'saved-final-shrink':
                        foreign.write_bytes(middle)
                    elif mutation == 'saved-final-replacement':
                        foreign.rename(saved)
                        foreign.write_bytes(large)
                    else:
                        with foreign.open('ab') as handle:
                            handle.write(b' \n')
                return value
            def read(self, buffer):
                nonlocal prefix_read
                value = old_read(self, buffer)
                if mutation == 'post-prefix-growth-then-shrink' and self.budget.probes == 3 and not prefix_read:
                    prefix_read = True
                    with foreign.open('ab') as handle:
                        handle.write(b' ' * 20000 + b'\n')
                return value
            @contextlib.contextmanager
            def source(path, *args, **kwargs):
                nonlocal restored
                if Path(path).resolve() == foreign and prefix_read and not restored:
                    restored = True
                    foreign.write_bytes(large + b' ' * 10000 + b'\n')
                with old_source(path, *args, **kwargs) as value:
                    yield value
            with patch.object(readers_cli, 'source_revision', revision), \
                    patch.object(readers_cli, 'regular_source', source), \
                    patch.object(context.Rows, '__getitem__', get), \
                    patch.object(readers_cli.ProbeReader, 'readinto', read):
                status, page, codes = here(home)
            assert fired and (mutation == 'prepared-growth-then-shrink' or restored), mutation
            assert status == 2 and not page and 'source_changed' in codes, (mutation, status, codes)


def test_foreign_prefix_rechecks_spend_cumulative_probe_byte_and_deadline_limits():
    for limit, expected in (('probes', 'detail_limit_probes'), ('bytes', 'detail_limit_probe_bytes'),
                            ('deadline', 'detail_deadline'), ('deadline-during-read', 'detail_deadline')):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target, _, others = wide_fixture(home, 1)
            foreign = others[0]
            initial_bytes = target.stat().st_size + foreign.stat().st_size
            old_get, old_check = context.Rows.__getitem__, readers_cli.context_prefix_held
            old_read = readers_cli.ProbeReader.readinto
            fired, checked, clock = False, False, [0.0]
            def get(self, index):
                nonlocal fired
                value = old_get(self, index)
                if not fired:
                    fired = True
                    with foreign.open('a') as handle:
                        handle.write(' \n')
                return value
            def check(path, proof, observation, budget):
                nonlocal checked
                checked = True
                assert budget.probes == 2 and budget.probe_bytes == initial_bytes
                if limit == 'deadline':
                    clock[0] = 31.0
                return old_check(path, proof, observation, budget)
            def read(self, buffer):
                value = old_read(self, buffer)
                if limit == 'deadline-during-read' and self.budget.probes == 3:
                    clock[0] = 31.0
                return value
            with patch.object(context.Rows, '__getitem__', get), \
                    patch.object(readers_cli, 'context_prefix_held', check), \
                    patch.object(readers_cli, 'DETAIL_MAX_PROBES', 2 if limit == 'probes' else 2048), \
                    patch.object(readers_cli, 'DETAIL_MAX_PROBE_TOTAL_BYTES',
                                 initial_bytes + foreign.stat().st_size - 1 if limit == 'bytes' else 64*1024*1024), \
                    patch.object(readers_cli.ProbeReader, 'readinto', read), \
                    patch.object(readers_cli, 'detail_now', lambda: clock[0]):
                status, page, codes = here(home)
            assert fired and checked and status == 2 and not page and expected in codes, (limit, codes)


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
