"""Physical Codex rollouts belonging to one paginated session.

History references supply legacy usage baselines, not an instruction to discard
work past a rewind cutoff. Native response ledgers supersede UI token meters.
Each immutable rollout has its own record identities.
"""
from __future__ import annotations

from contextlib import ExitStack
import re

from . import codex, codex_usage
from .strict_json import loads

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_NAME = re.compile(rf'rollout-.+-({_UUID})(?:_({_UUID}))?\.jsonl$')


def plan(items, headers):
    """Validate one discovered same-session group; never choose a newest file."""
    segments = {}
    for path, revision, header in items:
        raw = headers[path]
        meta = raw['payload']
        match = _NAME.fullmatch(path.name)
        if (not match or match[1] != meta['id'] or
                meta.get('history_mode') != 'paginated'):
            raise ValueError('duplicate session is not a paginated rollout group')
        rid = match[2] or match[1]
        if rid in segments:
            raise ValueError('duplicate immutable rollout ID')
        base = meta.get('history_base')
        if base is not None and (not isinstance(base, dict) or
                not isinstance(base.get('thread_id'), str) or
                any(type(base.get(k)) is not int or base[k] < 0
                    for k in ('end_byte_offset', 'end_ordinal_exclusive'))):
            raise ValueError('invalid history reference')
        start = 0 if base is None else base['end_ordinal_exclusive']
        if type(raw.get('ordinal')) is not int or raw['ordinal'] != start:
            raise ValueError('rollout header ordinal disagrees with history reference')
        segments[rid] = (path, revision, header, base)
    roots = [rid for rid, (_, _, _, base) in segments.items() if base is None]
    if len(roots) != 1:
        raise ValueError('history group requires one original rollout')
    root = roots[0]
    ordered = []
    visiting = set()
    done = set()

    def visit(rid):
        if rid in visiting or rid not in segments:
            raise ValueError('cyclic or missing history source')
        if rid in done:
            return
        visiting.add(rid)
        base = segments[rid][3]
        if base is not None:
            visit(base['thread_id'])
        visiting.remove(rid)
        done.add(rid)
        ordered.append(rid)

    for rid in sorted(segments):
        visit(rid)
    header = dict(segments[root][2])
    header['mtime'] = max(item[2]['mtime'] for item in segments.values())
    revision = tuple(entry for rid in ordered for entry in segments[rid][1])
    return segments[root][0], revision, header, [(rid, *segments[rid]) for rid in ordered]


def prefix_usage(path, cutoff, ordinal, inherited):
    """Read the referenced prefix's cumulative counters, proving its boundary."""
    previous = inherited
    offset = 0
    next_ordinal = None
    with path.open('rb') as source:
        while offset < cutoff:
            raw = source.readline()
            if not raw or offset + len(raw) > cutoff or not raw.endswith(b'\n'):
                raise ValueError('history cutoff is not a complete line boundary')
            offset += len(raw)
            row = loads(raw.decode('utf-8'), strict=True)
            current = row.get('ordinal') if isinstance(row, dict) else None
            if type(current) is not int or (next_ordinal is not None and current != next_ordinal):
                raise ValueError('history ordinal gap')
            next_ordinal = current + 1
            payload = row.get('payload')
            if row.get('type') == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'token_count':
                info = payload.get('info')
                total = codex._usage_total(info.get('total_token_usage') if isinstance(info, dict) else None)
                if total is not None and (previous is None or all(total[k] >= previous[k] for k in total)):
                    previous = total
    if (cutoff == 0 and ordinal != 0) or (cutoff and next_ordinal != ordinal):
        raise ValueError('history byte and ordinal bounds disagree')
    return previous



def read(group, snapshot, *, title_index=None):
    """Normalize every physical segment once, including abandoned tails."""
    session_id = group[0][3]["native_session_id"]
    with ExitStack() as stack:
        copies = {rid: stack.enter_context(snapshot(path, 'codex', revision))
                  for rid, path, revision, _, _ in group}
        seeds = {}
        records = []
        seen_responses = {}
        first_meta = None
        for rid, _, _, _, base in group:
            seed = None if base is None else prefix_usage(
                copies[base['thread_id']], base['end_byte_offset'],
                base['end_ordinal_exclusive'], seeds[base['thread_id']])
            seeds[rid] = seed
            rows = codex.load_rollout(copies[rid], strict=True)
            if (not rows or rows[0].get('type') != 'session_meta'
                    or rows[0].get('payload', {}).get('id') != session_id
                    or rows[0]['payload'].get('history_mode') != 'paginated'
                    or rows[0]['payload'].get('history_base') != base):
                raise ValueError('history header changed since discovery')
            start = 0 if base is None else base['end_ordinal_exclusive']
            for i, row in enumerate(rows):
                if (i and row.get('type') == 'session_meta') or type(row.get('ordinal')) is not int or row['ordinal'] != start + i:
                    raise ValueError('rollout has missing or inconsistent ordinals')
            record_sources, usage_targets = {}, {}
            namespace = rid if base is None else f"{session_id}:rollout:{rid}"
            converted, meta = codex.rollout_to_claude_records(
                rows, strict=True, title_index=title_index,
                identity_namespace=namespace,
                initial_usage_total=seed,
                usage_baseline_unknown=base is not None and seed is None,
                record_sources=record_sources, usage_targets=usage_targets)
            converted = codex_usage.apply(rows, converted, record_sources, usage_targets,
                session_id=session_id, namespace=namespace, seen=seen_responses)
            records.extend(converted)
            if first_meta is None:
                first_meta = meta
        return records, first_meta
