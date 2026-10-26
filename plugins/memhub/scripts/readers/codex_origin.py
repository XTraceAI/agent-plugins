"""Which canonical Codex records the host itself injected, as metadata only.

Opt-in evidence for one closed origin kind: a Codex user response item whose
every native content block is declared ``skills.selected_skill_instructions``
-- the body Codex adds when a skill is selected, beside (not instead of) the
person's own ``$skill`` request, which Codex declares ``user.text``. Nothing
here reads, copies or hashes text: the claim rests on the native kinds, the
native item and turn identifiers, and the exact record the conversion emitted
from that item.

A claim is made only when every structural fact holds; otherwise the record
simply carries none. It never changes a record, its UUID, or which records are
emitted -- the evidence rides beside them, keyed by the record object the same
conversion produced.
"""
from __future__ import annotations

from collections import Counter, namedtuple

from .codex_witness import (_PASSTHROUGH, _TURN_CLOSE, _Turns, _event, _item_id,
                            _payload, _turn_id, flat_header, session_id)

CONTRACT = "memhub.codex.origin_evidence"
VERSION = 1
KIND = "codex_selected_skill_instructions"
# The one native content kind this evidence recognizes. Pasted text that looks
# like a skill body is still ``user.text``, and so are guardian review inputs
# and heartbeats: none of them is ever this kind.
_NATIVE_KIND = "skills.selected_skill_instructions"


class Withholding(dict):
    """Claims for a read whose records must not depend on them.

    The bulk export reads every session once. If making its claims fails, the
    records that same conversion produced are still the ordinary ones, so the
    claims are dropped -- all of them -- and ``failed`` says so, rather than
    the session being lost with them. A plain dict keeps the exact mode's
    rule: a failure refuses the read.
    """
    failed = False


def collect(out, make):
    """Run ``make``, which fills ``out`` with claims.

    For a ``Withholding`` sink a failure empties it and marks it failed; for
    any other sink the failure propagates as it always did.
    """
    if not isinstance(out, Withholding):
        make()
        return
    try:
        make()
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        out.clear()
        out.failed = True


def _selected_skill(row):
    """Every declared kind is the selected-skill kind, one per text block held."""
    payload = _payload(row)
    if not (isinstance(row, dict) and row.get("type") == "response_item"
            and payload.get("type") == "message" and payload.get("role") == "user"):
        return False
    passthrough = payload.get(_PASSTHROUGH)
    kinds = passthrough.get("content_item_kinds") if isinstance(passthrough, dict) else None
    content = payload.get("content")
    return (isinstance(kinds, list) and isinstance(content, list) and len(kinds) > 0
            and len(kinds) == len(content)
            and all(kind == _NATIVE_KIND for kind in kinds)
            and all(isinstance(item, dict) and item.get("type") == "input_text"
                    and isinstance(item.get("text"), str) for item in content))


# What ``describe`` keeps of one rollout once its rows are gone: its native
# item and turn counts, and each record its own rows could claim, as
# ``(record, row index, ordinal, item_id, turn_id)``. ``error`` is a failure met
# while taking them, raised by ``describe``.
_Segment = namedtuple("_Segment", "rollout_id item_ids starts closes candidates error")


def segment(rollout_id, rows, own, sources, origins):
    """One rollout's facts for ``describe``, taken while its rows are held.

    ``rows`` are the rollout's rows as parsed; ``own`` the records converted
    from its own rows (never inherited or copied context); ``sources`` maps
    ``id(record)`` to row index and ``origins`` ``id(record)`` to ``(record,
    derivation)``, which must still hold every record the conversion made so
    no ``id`` has been reused. None of them is kept, so a paginated read can
    let each rollout's rows go before it parses the next. A failure is kept
    for ``describe``, which raises it where it always did.
    """
    try:
        item_ids, starts, closes = Counter(), Counter(), Counter()
        for row in rows:
            payload = _payload(row)
            if row.get("type") == "response_item" and isinstance(payload.get("id"), str):
                item_ids[payload["id"]] += 1
            if _event(row, "task_started"):
                turn = _turn_id(payload.get("turn_id"))
                if turn is not None:
                    starts[turn] += 1
            elif row.get("type") == "event_msg" and payload.get("type") in _TURN_CLOSE:
                turn = _turn_id(payload.get("turn_id"))
                if turn is not None:
                    closes[turn] += 1
        # Each native row this rollout's own records came from, with every
        # record that names it: one row must yield exactly one record.
        by_row = {}
        for record in own:
            index = sources.get(id(record))
            if type(index) is int and 0 <= index < len(rows):
                by_row.setdefault(index, []).append(record)
        skills = {index for index in by_row if _selected_skill(rows[index])}
        candidates = []
        turns = _Turns(rows, skills) if skills else None
        for index in sorted(skills):
            if len(by_row[index]) != 1:
                continue
            record = by_row[index][0]
            entry = origins.get(id(record))
            if entry is None or entry[0] is not record or entry[1] != "user_message":
                continue
            payload = _payload(rows[index])
            item = _item_id(payload)
            turn = _turn_id(payload[_PASSTHROUGH].get("turn_id"))
            opened = turns.open_at.get(index)
            if (item is None or turn is None or opened is None or opened[0] != turn
                    or turn in turns.invalid):
                continue
            ordinal = rows[index].get("ordinal")
            candidates.append((record, index, ordinal if type(ordinal) is int else None,
                               item, turn))
        return _Segment(rollout_id, item_ids, starts, closes, candidates, None)
    except Exception as error:  # noqa: BLE001 -- ``describe`` raises it again
        # Without its traceback the failure keeps no frame, and so no row.
        return _Segment(rollout_id, Counter(), Counter(), Counter(), [],
                        error.with_traceback(None))


def flat_segments(rows, records, sources, origins, native_session_id):
    """One flat rollout as ``describe`` takes it.

    Its records are its own only when the rollout positively shows it: a flat
    header naming this session, no other ``session_meta`` and no subagent
    boundary. Otherwise no record of it is claimed.
    """
    header = rows[0] if rows else None
    owned = (flat_header(header, native_session_id)
             and [i for i, row in enumerate(rows) if row.get("type") == "session_meta"] == [0]
             and _payload(header).get("subagent_history_start_ordinal") is None)
    return [segment(None, rows, records if owned else [], sources, origins)]


def describe(segments, records, *, native_session_id, history, out):
    """Claim each qualifying record into ``out[id(record)] = (record, evidence)``.

    ``segments`` lists every rollout the conversion consumed, in order, as
    ``segment`` took it. ``records`` is every record emitted, in order.
    Identifier counts span all segments, so an item or turn repeated anywhere
    in what was read is never unique; a record UUID must be unique among the
    records emitted, as they are now.
    """
    if session_id(native_session_id) is None or history not in ("flat", "paginated"):
        return out
    uuids = Counter(record.get("uuid") for record in records)
    emitted = {id(record) for record in records}
    item_ids, starts, closes = Counter(), Counter(), Counter()
    for part in segments:
        if part.error is not None:
            raise part.error
        item_ids.update(part.item_ids)
        starts.update(part.starts)
        closes.update(part.closes)
    for part in segments:
        for record, index, ordinal, item, turn in part.candidates:
            uuid = record.get("uuid")
            if (id(record) not in emitted or not isinstance(uuid, str) or uuids[uuid] != 1
                    or item_ids[item] != 1 or starts[turn] != 1 or closes[turn] > 1):
                continue
            if id(record) in out:
                raise ValueError("record claimed twice")
            out[id(record)] = (record, {
                "contract": CONTRACT,
                "version": VERSION,
                "kind": KIND,
                "native_session_id": native_session_id,
                "segment": {"history": history, "rollout_id": part.rollout_id},
                "row": {"index": index, "ordinal": ordinal},
                "item_id": item,
                "turn_id": turn,
                "record_uuid": uuid,
            })
    return out
