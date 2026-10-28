"""Physical Codex rollouts belonging to one paginated session.

History references supply legacy usage baselines, not an instruction to discard
work past a rewind cutoff. Native response ledgers supersede UI token meters.
Each immutable rollout has its own record identities.

A forked session's first rollout references a rollout of the session it was
forked from. That rollout is never part of the fork: only its prefix's
cumulative counters are read, so the fork's first usage delta is its own work.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
import io
from pathlib import Path
import re

from . import codex, codex_usage
from .strict_json import loads

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
_NAME = re.compile(rf'rollout-.+-({_UUID})(?:_({_UUID}))?\.jsonl$')


def _history_base(meta):
    """A rollout header's history reference, validated, or None."""
    base = meta.get('history_base')
    if base is not None and (not isinstance(base, dict) or
            not isinstance(base.get('thread_id'), str) or
            any(type(base.get(k)) is not int or base[k] < 0
                for k in ('end_byte_offset', 'end_ordinal_exclusive'))):
        raise ValueError('invalid history reference')
    return base


def _fork_origin(meta, base):
    """The session a rollout says its history reference belongs to, or None.

    Only a rollout that references history and names a different session it
    was forked from has one. A stated fork cutoff must be the reference's.
    """
    parent = meta.get('forked_from_id')
    if base is None or parent is None:
        return None
    if not isinstance(parent, str) or not parent.strip() or parent == meta.get('id'):
        raise ValueError('invalid fork origin')
    cutoff = meta.get('forked_from_ordinal_exclusive')
    if cutoff is not None and (type(cutoff) is not int or cutoff != base['end_ordinal_exclusive']):
        raise ValueError('fork cutoff disagrees with history reference')
    return parent


def plan(items, headers):
    """Validate one discovered same-session group; never choose a newest file.

    The group's root is its one original rollout, or -- for a forked session
    -- the one rollout whose history reference names a rollout of the session
    it was forked from. ``references`` resolves that rollout separately.
    """
    segments = {}
    started = {}
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
        base = _history_base(meta)
        start = 0 if base is None else base['end_ordinal_exclusive']
        if type(raw.get('ordinal')) is not int or raw['ordinal'] != start:
            raise ValueError('rollout header ordinal disagrees with history reference')
        timestamp = codex._metadata_from_header(raw, strict=True)['started_at']
        if timestamp is None:
            raise ValueError('paginated rollout requires a native start time')
        started[rid] = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        segments[rid] = (path, revision, header, base)
    roots = [rid for rid, (path, _, _, base) in segments.items()
             if base is None or (base['thread_id'] not in segments and
                                 _fork_origin(headers[path]['payload'], base) is not None)]
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
        if base is not None and rid != root:
            visit(base['thread_id'])
        visiting.remove(rid)
        done.add(rid)
        ordered.append(rid)

    for rid in sorted(segments, key=lambda rid: (started[rid], rid)):
        visit(rid)
    header = dict(segments[root][2])
    header['mtime'] = max(item[2]['mtime'] for item in segments.values())
    revision = tuple(entry for rid in ordered for entry in segments[rid][1])
    return segments[root][0], revision, header, [(rid, *segments[rid]) for rid in ordered]


def reference_facts(path, raw):
    """``(rollout ID, small header facts)`` for a probed rollout, or None.

    Kept for every rollout discovery identified, whichever session it names,
    so a fork can find the rollout it references without holding headers.
    """
    match = _NAME.fullmatch(Path(path).name)
    payload = raw.get('payload') if isinstance(raw, dict) else None
    if not match or not isinstance(payload, dict):
        return None
    keys = ('id', 'history_mode', 'history_base', 'forked_from_id', 'forked_from_ordinal_exclusive')
    return match[2] or match[1], {'ordinal': raw.get('ordinal'),
                                  'payload': {key: payload.get(key) for key in keys}}


def references(group, headers, index):
    """The rollouts outside a forked group that its usage baseline comes from.

    ``index`` maps a rollout ID to every discovered ``(path, revision, facts)``
    carrying it (see ``reference_facts``). Each referenced rollout must be the
    one discovered file of that ID, named and headed as that ID, and belong to
    the referring rollout's own session or the session it says it was forked
    from; its own reference is followed the same way until an original
    rollout. Returned oldest first as ``(rid, path, revision, base)``; empty
    for a group whose root is an original rollout. Any doubt refuses.
    """
    rid, path, _, _, base = group[0]
    if base is None:
        return []
    meta = headers[path]['payload']
    owner = _fork_origin(meta, base)
    if owner is None:
        raise ValueError('history group requires one original rollout')
    seen = {item[0] for item in group}
    allowed = {owner}
    chain = []
    while base is not None:
        thread = base['thread_id']
        if thread in seen:
            raise ValueError('cyclic history source')
        seen.add(thread)
        found = index.get(thread, ())
        if len(found) != 1:
            raise ValueError('missing or ambiguous history source')
        source, revision, facts = found[0]
        payload = facts['payload']
        sid = payload.get('id')
        match = _NAME.fullmatch(Path(source).name)
        if (not match or match[1] != sid or (match[2] or match[1]) != thread
                or sid not in allowed or payload.get('history_mode') != 'paginated'):
            raise ValueError('history source is not the referenced rollout')
        base = _history_base(payload)
        start = 0 if base is None else base['end_ordinal_exclusive']
        if type(facts.get('ordinal')) is not int or facts['ordinal'] != start:
            raise ValueError('rollout header ordinal disagrees with history reference')
        chain.append((thread, source, revision, base))
        try:
            parent = _fork_origin(payload, base)
        except ValueError:
            # Not a fork step it can vouch for: only its own session is next.
            parent = None
        allowed = {sid} if parent is None else {sid, parent}
    chain.reverse()
    return chain


def context_boundary(rows):
    boundary = rows[0]['payload'].get('subagent_history_start_ordinal')
    if boundary is not None and (type(boundary) is not int or boundary < 0
                                 or boundary > rows[-1]['ordinal'] + 1):
        raise ValueError('invalid or incomplete subagent context boundary')
    return boundary


def project_context(rows, records, sources, convert):
    """Retain legacy IDs while projecting inherited context out of child work."""
    boundary = context_boundary(rows)
    if boundary is None or boundary <= rows[0]['ordinal'] + 1:
        return rows, records, None, []
    projected = [rows[0], *[{'type': 'inherited_context'} if row['ordinal'] < boundary else row
                           for row in rows[1:]]]
    identities = {sources[id(record)]: record['uuid'] for record in records}
    context = [record for record in records if rows[sources[id(record)]]['ordinal'] < boundary]
    own, meta = convert(projected)
    for record in own:
        if sources[id(record)] in identities:
            record['uuid'] = identities[sources[id(record)]]
    for record in context:
        record['isMeta'] = True
        record['message'].pop('usage', None)
    return projected, own, meta, context


@contextmanager
def _acquired(source):
    """One acquired segment as a binary reader.

    An acquirer hands back a private snapshot path, the segment bytes the
    caller already holds, or a binary reader the caller already opened and
    validated (anything with ``readline``), which is passed through as is and
    read from where it stands, so it serves one prefix read.
    None of them reopens the native name the caller validated.
    """
    if hasattr(source, 'readline'):
        yield source
        return
    if isinstance(source, (bytes, bytearray, memoryview)):
        yield io.BytesIO(bytes(source))
        return
    with Path(source).open('rb') as handle:
        yield handle


def prefix_usage(source, cutoff, ordinal, inherited):
    """Read the referenced prefix's cumulative counters, proving its boundary."""
    previous = inherited
    offset = 0
    next_ordinal = None
    owned_from = 0
    with _acquired(source) as prefix:
        while offset < cutoff:
            raw = prefix.readline()
            if not raw or offset + len(raw) > cutoff or not raw.endswith(b'\n'):
                raise ValueError('history cutoff is not a complete line boundary')
            offset += len(raw)
            row = loads(raw.decode('utf-8'), strict=True)
            current = row.get('ordinal') if isinstance(row, dict) else None
            if type(current) is not int or (next_ordinal is not None and current != next_ordinal):
                raise ValueError('history ordinal gap')
            if next_ordinal is None:
                owned_from = row.get('payload', {}).get('subagent_history_start_ordinal') or 0
            next_ordinal = current + 1
            payload = row.get('payload')
            if current >= owned_from and row.get('type') == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'token_count':
                info = payload.get('info')
                total = codex._usage_total(info.get('total_token_usage') if isinstance(info, dict) else None)
                if total is not None and (previous is None or all(total[k] >= previous[k] for k in total)):
                    previous = total
    if (cutoff == 0 and ordinal != 0) or (cutoff and next_ordinal != ordinal):
        raise ValueError('history byte and ordinal bounds disagree')
    return previous



def read(group, acquire, *, title_index=None, record_guard=None, origin=None, human=None,
         automated=None, references=(), row_provider=None, record_projection=None, retention_guard=None):
    """Normalize every physical segment once, including abandoned tails.

    ``acquire`` is the caller's bounded source acquisition, called as
    ``acquire(path, 'codex', revision)``. It may yield a private snapshot path,
    the segment's bytes, or an open binary reader; a referenced prefix is
    re-read from the segment already acquired for it, so a history reference
    costs no second read.

    ``record_guard`` bounds what the whole group may hold. Each segment's
    conversion is guarded against the total already kept, so the ceiling is a
    session's rather than a rollout's, and it is reached while the records are
    accumulating.

    ``origin``, when a dict, receives the origin evidence of each record
    converted from an injected skill item of a segment's own rows (see
    ``codex_origin``). Inherited context is never claimed. Each segment's
    facts are taken as soon as it is converted, so its rows are let go before
    the next segment is parsed, as they are without evidence. Without it the
    conversion is exactly the one it always was. ``human`` similarly receives
    image-wrapper length evidence, only for surviving own records, and
    ``automated`` automated-input evidence (see ``automated_input``) the same way.

    ``references`` are the rollouts outside a forked group (see
    ``references``). Each is acquired like a segment, but only its prefix's
    counters are read: none of its rows become this session's records.
    """
    session_id = group[0][3]["native_session_id"]
    from . import automated_input, codex_human, codex_origin
    human_segments = [] if human is not None else None
    automated_segments = [] if automated is not None else None
    segments = None
    if origin is not None:
        segments = []
    with ExitStack() as stack:
        copies = {rid: stack.enter_context(acquire(path, 'codex', revision))
                  for rid, path, revision, _, _ in group}
        root = group[0][0]
        seeds = {}

        def inherited(base):
            if base is None:
                return None
            if base['thread_id'] not in copies or base['thread_id'] not in seeds:
                raise ValueError('cyclic or missing history source')
            return prefix_usage(copies[base['thread_id']], base['end_byte_offset'],
                                base['end_ordinal_exclusive'], seeds[base['thread_id']])

        for rid, path, revision, base in references:
            if rid in copies:
                raise ValueError('duplicate immutable rollout ID')
            copies[rid] = stack.enter_context(acquire(path, 'codex', revision))
            seeds[rid] = inherited(base)
        records = []
        seen_responses = {}
        first_meta = None
        native_title = None
        for rid, _, _, _, base in group:
            seed = inherited(base)
            seeds[rid] = seed
            rows = (codex.load_rollout(copies[rid], strict=True) if row_provider is None
                    else row_provider(copies[rid]))
            if (not rows or rows[0].get('type') != 'session_meta'
                    or rows[0].get('payload', {}).get('id') != session_id
                    or rows[0]['payload'].get('history_mode') != 'paginated'
                    or rows[0]['payload'].get('history_base') != base):
                raise ValueError('history header changed since discovery')
            start = 0 if base is None else base['end_ordinal_exclusive']
            for i, row in enumerate(rows):
                if type(row.get('ordinal')) is not int or row['ordinal'] != start + i:
                    raise ValueError('rollout has missing or inconsistent ordinals')
            boundary = context_boundary(rows)
            if any(row.get('type') == 'session_meta' and
                   (boundary is None or row['ordinal'] >= boundary) for row in rows[1:]):
                raise ValueError('session metadata outside inherited context')
            record_sources, usage_targets = {}, {}
            # Held only for origin evidence, until this segment's facts are
            # taken. It keeps every record either conversion made alive until
            # then, so no ``id`` in ``record_sources`` is reused.
            record_origins = ({} if segments is not None or human_segments is not None
                              or automated_segments is not None else None)
            # The root -- original or forked -- keeps its own rollout ID.
            namespace = rid if rid == root else f"{session_id}:rollout:{rid}"
            # What earlier segments already hold; this segment's guard counts
            # from there, so the bound is the session's and not each file's.
            held = len(records)
            segment_guard = (None if record_guard is None
                             else lambda count: record_guard(held + count))

            def convert(items):
                usage_targets.clear()
                return codex.rollout_to_claude_records(
                    items, strict=True, title_index={}, identity_namespace=namespace,
                    initial_usage_total=seed,
                    usage_baseline_unknown=base is not None and seed is None,
                    record_sources=record_sources, usage_targets=usage_targets,
                    record_guard=segment_guard, record_origins=record_origins,
                    record_projection=record_projection, retention_guard=retention_guard)
            converted, meta = convert(rows)
            parsed = rows
            rows, converted, own_meta, context = project_context(rows, converted, record_sources, convert)
            meta = own_meta or meta
            # ``apply`` grows ``converted``; what the group already holds is
            # the earlier segments plus the context about to be kept beside
            # it, so the ceiling it is measured against is the session's.
            ledger_base = len(records) + len(context)
            converted = codex_usage.apply(rows, converted, record_sources, usage_targets,
                session_id=session_id, namespace=namespace, seen=seen_responses,
                retention_guard=retention_guard, record_guard=(None if record_guard is None
                              else lambda count: record_guard(ledger_base + count)))
            records.extend(context)
            records.extend(converted)
            if human_segments is not None:
                human_segments.append(codex_human.segment(
                    rid, parsed, converted, record_sources, record_origins))
            if automated_segments is not None:
                automated_segments.append(automated_input.codex_segment(
                    rid, parsed, converted, record_sources, record_origins))
            if segments is not None:
                # ``converted`` is this rollout's own work; ``context`` is what
                # it inherited, and is never offered for a claim. Only small
                # facts are kept: neither the parsed rows nor the records the
                # first conversion made outlive this segment.
                segments.append(codex_origin.segment(
                    rid, parsed, converted, record_sources, record_origins))
            parsed = record_origins = None
            if record_guard is not None:
                record_guard(len(records))
            native_title = codex._rollout_thread_name(rows, strict=True) or native_title
            if first_meta is None:
                first_meta = meta
        # Resolve the fallback once, after seeing every native rename. An early
        # root fallback must not hide a continuation's title or require its index.
        first_meta['title'] = (native_title or codex._title(
            [], session_id, strict=True, title_index=title_index) or first_meta['title'])
        if segments is not None:
            codex_origin.collect(origin, lambda: codex_origin.describe(
                segments, records, native_session_id=session_id,
                history="paginated", out=origin))
        if human_segments is not None:
            codex_origin.collect(human, lambda: codex_human.describe(
                human_segments, records, native_session_id=session_id, out=human))
        if automated_segments is not None:
            codex_origin.collect(automated, lambda: automated_input.codex_describe(
                automated_segments, records, native_session_id=session_id, out=automated))
        return records, first_meta
