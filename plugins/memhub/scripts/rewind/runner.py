"""Claim once, journal before side effects, and hand off to a native terminal."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
from .adapter import resolve
from .checkpoints import Store, find_checkpoint
from .client import Client
from .state import (Unavailable, branch_path, capabilities_path, client_id, digest, identifier, journal_path,
                    lock, read, root, write)

TERMINAL = {'completed', 'failed', 'canceled'}


def report(journal, state=None, error=None, final_watermark=None):
    previous = journal.get('report', {})
    branch = read(branch_path(journal['native_branch_id']), {}) if journal.get('native_branch_id') else {}
    reported = journal.get('reported_config')
    if reported and branch.get('actual_model_id'):
        reported['config']['model_id'] = branch['actual_model_id']
        reported['model_verification'] = 'harness_reported'
        deviations = []
        if branch['actual_model_id'] != journal['plan']['requested_config']['model_id']:
            deviations.append({'code': 'model_changed', 'event_id': None,
                               'detail': 'Native continuation reports a different model.'})
        deviations.extend({'code': 'human_steering', 'event_id': event,
                           'detail': 'Additional human input after the common boundary.'}
                          for event in branch.get('human_interventions', [])[:49])
        reported['deviations'] = deviations
    if previous.get('execution_state') in TERMINAL and state not in (None, previous['execution_state']):
        raise Unavailable('terminal_run')
    body = {**previous, 'claim_id': journal['claim_id'],
            'sequence': previous.get('sequence', 0) + 1,
            'execution_state': state or previous.get('execution_state', 'preparing'),
            'reported_config': journal.get('reported_config'),
            'native_branch_id': journal.get('native_branch_id'),
            'final_watermark': final_watermark or previous.get('final_watermark'), 'error': error}
    journal['report'] = body
    write(journal_path(journal['run_id']), journal)
    return sync(journal)


def sync(journal, force=False):
    if not journal.get('report'):
        return True
    if not force and journal.get('next_sync_at', 0) > time.time():
        return False
    try:
        Client(journal['org_id']).report(journal['comparison_id'], journal['run_id'], journal['report'])
        journal['retry_delay'] = 0
        journal['next_sync_at'] = 0
        write(journal_path(journal['run_id']), journal)
        return True
    except Unavailable:
        journal['retry_delay'] = min(120, max(15, journal.get('retry_delay', 0) * 2))
        journal['next_sync_at'] = time.time() + journal['retry_delay']
        write(journal_path(journal['run_id']), journal)
        return False  # durable cumulative body is retried on heartbeat/resume/sync


def register_branch(journal, manifest, fork):
    plan = journal['plan']
    branch = {'protocol_version': 1, 'comparison_id': journal['comparison_id'],
              'run_id': journal['run_id'], 'claim_id': journal['claim_id'],
              'native_branch_id': fork['native_branch_id'], 'parent': plan['source'],
              'parent_boundary': plan['boundary']['selected_event'],
              'checkpoint_manifest_digest': manifest['manifest_digest'],
              'source_native_session_id': manifest['native_session_id'],
              'source_transcript': manifest['local_config']['transcript_path']}
    write(branch_path(fork['native_branch_id']), branch)
    journal.update(fork)
    journal['phase'] = 'forked'
    write(journal_path(journal['run_id']), journal)


def open_run(comparison_id, org_id, role='alternative', adapter_factory=resolve):
    client = Client(org_id)
    comparison_id, org_id = identifier(comparison_id), identifier(org_id)
    index_path = root() / 'comparisons' / (digest([org_id, comparison_id, role]) + '.json')
    cached = read(index_path)
    if cached and journal_path(cached['run_id']).exists():
        run_id = identifier(cached['run_id'])
    else:
        comparison = client.get(comparison_id)
        runs = [r for r in comparison['runs'] if r['role'] == role and r['execution_mode'] == 'local']
        if len(runs) != 1:
            raise Unavailable('run_not_executable')
        run_id = identifier(runs[0]['id'])
    path = journal_path(run_id)
    with lock(path.with_suffix('.lock')):
        journal = read(path)
        if journal is None:
            journal = {'run_id': run_id, 'comparison_id': comparison_id, 'org_id': org_id,
                       'client_id': client_id(), 'claim_id': str(uuid4()), 'phase': 'claiming'}
            write(path, journal)  # lost claim ACK retries this SAME claim
            write(index_path, {'run_id': run_id})
        if journal['org_id'] != org_id or journal['comparison_id'] != comparison_id:
            raise Unavailable('local_run_identity_conflict')
        if journal.get('report', {}).get('execution_state') in TERMINAL:
            sync(journal)
            raise Unavailable('terminal_run')
        if 'plan' not in journal:
            reply = client.claim(comparison_id, run_id, journal['client_id'], journal['claim_id'])
            journal['plan'] = reply['plan']
            journal['phase'] = 'claimed'
            write(path, journal)
        plan = journal['plan']
        checkpoint = plan['boundary']['checkpoint']
        manifest = find_checkpoint(checkpoint['checkpoint_id'])
        if (manifest['manifest_digest'] != checkpoint['manifest_digest'] or
                manifest['client_id'] != journal['client_id'] or
                manifest['native_turn_id'] != checkpoint['native_turn_id']):
            raise Unavailable('checkpoint_binding_mismatch')
        adapter = adapter_factory(plan['requested_config']['harness'])
        try:
            config = adapter.preflight(manifest, plan['requested_config'])
            write(capabilities_path(plan['requested_config']['harness']), adapter.capabilities())
            journal['config'] = config
            journal['reported_config'] = {'config': {k: v for k, v in config.items() if not k.startswith('_')},
                                          'model_verification': 'requested', 'deviations': []}
            if journal['phase'] == 'interactive':
                pid = journal.get('native_pid')
                if pid:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise Unavailable('native_process_may_still_be_running')
                else:
                    raise Unavailable('launch_reconciliation_required')
            if journal['phase'] in ('forking', 'restoring'):
                raise Unavailable('launch_reconciliation_required')
            if 'native_branch_id' not in journal:
                store = Store(manifest['repo'])
                store.pin(manifest, run_id)
                target = root() / 'worktrees' / run_id
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if journal['phase'] != 'restored':
                    journal.update(phase='restoring', cwd=str(target))
                    report(journal, 'preparing')
                    write(root() / 'launches' / (digest(str(target.resolve())) + '.json'), {'run_id': run_id})
                    store.restore(manifest, target)
                    journal['phase'] = 'restored'
                    write(path, journal)
                if hasattr(adapter, 'prepare'):
                    adapter.prepare(manifest, config, str(target))
                journal['phase'] = 'forking'
                write(path, journal)
                fork = adapter.fork(manifest, config, str(target))
                register_branch(journal, manifest, fork)
                journal['reported_config']['model_verification'] = 'harness_reported'
            if not config.get('_isolation') and hasattr(adapter, 'prepare'):
                adapter.prepare(manifest, config, journal['cwd'])
            write(path, journal)
            inputs = None
            if not journal.get('input_dispatched'):
                inputs = journal['input']
                replacement = plan['input'].get('replacement_text')
                if replacement is not None:
                    inputs = [{'type': 'text', 'text': replacement}] + [p for p in inputs if p['type'] != 'text']
            command = adapter.command(journal['native_branch_id'], journal['cwd'], config, inputs)
            print('Local comparison checkout:', journal['cwd'])
            print('Model:', config['model_id'])
            print('Remote effects are not restored. Exit the native terminal to pause; explicitly finish or cancel using memhub-compare.')
            # Persist BEFORE dispatch. A crash here resumes without resubmitting the prompt.
            journal['input_dispatched'] = True
            journal['phase'] = 'interactive'
            report(journal, 'running')
            adapter.close()
            adapter = None
            proc = subprocess.Popen(command, cwd=journal['cwd'])
            journal['native_pid'] = proc.pid
            write(path, journal)
            try:
                while proc.poll() is None:
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        report(journal, 'running')
            except KeyboardInterrupt:
                proc.wait()
            journal['phase'] = 'paused'
            if proc.returncode not in (0, 130):
                report(journal, 'failed', {'code': 'native_process_failed', 'detail': 'Native terminal exited unsuccessfully.'})
                raise Unavailable('native_process_failed')
            report(journal, 'awaiting_user')
            print('Paused. Resume with the same open command; finish with: memhub-compare finish', run_id)
        except Unavailable as exc:
            # Ambiguous native side effects must remain reconcilable, never retry a fork.
            if str(exc) == 'rewind_hook_review_required':
                report(journal, 'awaiting_user', {'code': str(exc), 'detail': 'Run the same command in a terminal to review the isolated hooks.'})
            elif str(exc) == 'checkpoint_store_busy':
                report(journal, 'awaiting_user', {'code': str(exc), 'detail': 'Checkpoint storage is busy. Retry the same open command.'})
            elif journal['phase'] not in ('forking', 'restoring', 'interactive'):
                report(journal, 'failed', {'code': str(exc), 'detail': str(exc)})
            raise
        finally:
            if adapter is not None:
                adapter.close()


def finish(run_id, cancel=False):
    path = journal_path(run_id)
    with lock(path.with_suffix('.lock')):
        journal = read(path)
        if not journal:
            raise Unavailable('local_run_not_found')
        if journal.get('phase') in ('interactive', 'forking', 'restoring'):
            raise Unavailable('local_run_reconciliation_required')
        branch = read(branch_path(journal.get('native_branch_id')), {})
        watermark = branch.get('last_capture_watermark')
        if not cancel and not watermark:
            raise Unavailable('final_capture_not_confirmed')
        report(journal, 'canceled' if cancel else 'completed', final_watermark=watermark)
        checkpoint = journal.get('plan', {}).get('boundary', {}).get('checkpoint')
        if checkpoint:
            try:
                manifest = find_checkpoint(checkpoint['checkpoint_id'])
                Store(manifest['repo']).release(manifest, run_id)
            except Unavailable:
                pass
        print('Canceled' if cancel else 'Completed', run_id)


def reconcile(run_id, native_branch_id=None):
    """Explicitly attach a native branch after an ambiguous fork; never create one."""
    path = journal_path(run_id)
    with lock(path.with_suffix('.lock')):
        journal = read(path)
        if not journal:
            raise Unavailable('local_run_not_found')
        plan = journal['plan']
        manifest = find_checkpoint(plan['boundary']['checkpoint']['checkpoint_id'])
        if journal['phase'] == 'restoring' and native_branch_id is None:
            from .checkpoints import inventory, git
            target = Path(journal['cwd'])
            actual = inventory(target)
            expected = {k: v for k, v in manifest['files'].items() if v is not None}
            if (git(target, 'rev-parse', 'HEAD').decode().strip() != manifest['commit'] or
                    set(actual) != set(expected) or any(actual[k][3] != v[3] for k, v in expected.items())):
                raise Unavailable('restore_reconciliation_failed')
            journal['phase'] = 'restored'
            write(path, journal)
            return
        if journal['phase'] == 'forking' and plan['requested_config']['harness'] == 'claude_code':
            # The Claude fork is a local file write: the one branch it may have
            # left is found in the checkout's transcript folder and verified.
            adapter = resolve('claude_code')
            try:
                register_branch(journal, manifest, adapter.reconcile(manifest, journal, native_branch_id))
            finally:
                adapter.close()
            return
        if journal['phase'] != 'forking' or native_branch_id is None:
            raise Unavailable('reconciliation_requires_native_branch')
        from readers import codex as reader
        isolation = journal.get('config', {}).get('_isolation')
        if isolation:
            matches = list((Path(isolation['home']) / 'sessions').rglob('*' + native_branch_id + '.jsonl'))
            native_path = matches[0] if len(matches) == 1 else None
        else:
            native_path, _ = reader.locate(native_branch_id)
        if native_path is None:
            raise Unavailable('native_branch_unavailable')
        rows = reader.load_rollout(native_path)
        header = rows[0]['payload']
        if (header.get('id') != native_branch_id or header.get('forked_from_id') != manifest['native_session_id'] or
                Path(header['cwd']).resolve() != Path(journal['cwd']).resolve()):
            raise Unavailable('reconciliation_identity_mismatch')
        adapter = resolve(plan['requested_config']['harness'])
        try:
            if isolation:
                adapter.prepare(manifest, journal['config'], journal['cwd'])
            boundary = journal['config']['_boundary'] if isolation else adapter.resolve_boundary(manifest)
            turns = adapter.turns(native_branch_id) if isolation else adapter.rpc.request('thread/read', {'threadId': native_branch_id, 'includeTurns': True})['thread']['turns']
            if turns != boundary['prefix']:
                raise Unavailable('reconciliation_prefix_mismatch')
            register_branch(journal, manifest, {'native_branch_id': native_branch_id,
                            'input': boundary['input'], 'prefix_turn_ids': [t['id'] for t in boundary['prefix']]})
        finally:
            adapter.close()
