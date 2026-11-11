#!/usr/bin/env python3
"""Installer entry point for local session rewind; no local HTTP daemon."""
import argparse
import json
import sys
from pathlib import Path
from rewind.state import Unavailable, journal_path, read, root, write


def main():
    parser = argparse.ArgumentParser(prog='memhub-compare')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('enable', help='Enable before-user checkpoints for future Codex and Claude Code turns')
    sub.add_parser('disable', help='Stop capturing new local checkpoints')
    hook = sub.add_parser('checkpoint', help=argparse.SUPPRESS)
    hook.add_argument('--harness', choices=['codex', 'claude_code'], default='codex')
    caps = sub.add_parser('capabilities', help='Refresh the installed harness model catalog for the next capture')
    caps.add_argument('--harness', choices=['codex', 'claude_code'], default='codex')
    turns = sub.add_parser('turns', help='List local checkpointed messages; no browser needed')
    turns.add_argument('--session', required=True)
    direct = sub.add_parser('start', help='Create and open a local alternative without Studio')
    direct.add_argument('--session', required=True)
    direct.add_argument('--event', required=True, help='Event ID from turns')
    direct.add_argument('--org', required=True)
    change = direct.add_mutually_exclusive_group(required=True)
    change.add_argument('--message-file', type=Path, help='UTF-8 replacement prompt (use - for stdin)')
    change.add_argument('--model')
    direct.add_argument('--criterion', required=True)
    direct.add_argument('--baseline', choices=['historical', 'fresh'])
    direct.add_argument('--prepare-only', action='store_true', help='Create the comparison and print its launch command')
    direct.add_argument('--request-id', help='Explicit attempt identity; reuse it on retries, change it for a new experiment')
    start = sub.add_parser('open')
    start.add_argument('comparison_id')
    start.add_argument('--org', required=True)
    start.add_argument('--role', choices=['baseline', 'alternative'], default='alternative')
    for action in ('finish', 'cancel', 'sync', 'status'):
        item = sub.add_parser(action)
        item.add_argument('run_id')
    recovery = sub.add_parser('reconcile')
    recovery.add_argument('run_id')
    recovery.add_argument('--native-branch')
    args = parser.parse_args()
    try:
        if args.command in ('enable', 'disable'):
            write(root() / 'settings.json', {'enabled': args.command == 'enable'})
            print('Local rewind checkpoints', 'enabled' if args.command == 'enable' else 'disabled')
        elif args.command == 'capabilities':
            from rewind.adapter import resolve
            from rewind.state import capabilities_path
            adapter = resolve(args.harness)
            try:
                value = adapter.capabilities()
                write(capabilities_path(args.harness), value)
                print('Model catalog refreshed locally. Continue the original session to publish it, then refresh the dialog.')
            finally:
                adapter.close()
        elif args.command == 'checkpoint':
            from rewind.hooks import capture_prompt
            capture_prompt(json.load(sys.stdin), args.harness)
        elif args.command == 'turns':
            from rewind.start import choices
            print(json.dumps([{k: v for k, v in choice.items() if not k.startswith('_')}
                              for choice in choices(args.session)], ensure_ascii=False))
        elif args.command == 'start':
            from rewind.start import start
            from rewind.runner import open_run
            if args.message_file is not None:
                message = sys.stdin.read() if str(args.message_file) == '-' else args.message_file.read_text(encoding='utf-8')
                intervention = {'kind': 'instruction', 'replacement_text': message}
            else:
                intervention = {'kind': 'model', 'model_id': args.model}
            baseline = args.baseline or ('fresh' if args.model else 'historical')
            comparison = start(args.session, args.event, args.org, intervention, args.criterion, baseline,
                               request_id=args.request_id)
            print('Comparison:', comparison, flush=True)
            print('Resume: memhub-compare open', comparison, '--org', args.org, flush=True)
            if baseline == 'fresh':
                print('Original baseline: memhub-compare open', comparison, '--org', args.org, '--role baseline', flush=True)
            if not args.prepare_only:
                open_run(comparison, args.org)
        elif args.command == 'open':
            from rewind.runner import open_run
            open_run(args.comparison_id, args.org, args.role)
        elif args.command == 'reconcile':
            from rewind.runner import reconcile
            reconcile(args.run_id, args.native_branch)
            print('Reconciled. Use the original open command to continue.')
        elif args.command in ('finish', 'cancel'):
            from rewind.runner import finish
            finish(args.run_id, args.command == 'cancel')
        else:
            journal = read(journal_path(args.run_id))
            if not journal:
                raise Unavailable('local_run_not_found')
            if args.command == 'sync':
                from rewind.runner import sync
                if not sync(journal, force=True):
                    raise Unavailable('report_queued_offline')
            print(json.dumps({'run_id': journal['run_id'], 'phase': journal['phase'],
                              'execution_state': journal.get('report', {}).get('execution_state')}))
    except (Unavailable, OSError, ValueError, KeyError) as exc:
        print(str(exc) if isinstance(exc, Unavailable) else 'local_rewind_failed', file=sys.stderr)
        return 1
    except Exception:
        print('local_rewind_failed', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
