"""Which canonical user records the host or this reader wrote, as metadata only.

Opt-in evidence (``--automated-input-evidence``) for a closed set of user
records a person never typed, each recognised from a structural marker in the
native source and never from text:

Codex -- a user response item whose every native content block is declared
one and the same of these kinds in
``internal_chat_message_metadata_passthrough.content_item_kinds``:

* ``multi_agent.subagent_notification`` -> ``codex_subagent_notification``:
  Codex announcing that a subagent it started finished;
* ``generic.turn_aborted`` -> ``codex_turn_aborted``: Codex's note that the
  previous turn was interrupted;
* ``additional_content.codex_apps_open_page`` -> ``codex_apps_open_page``:
  the app's own record of a page it opened.

A person's message is declared ``user.text`` / ``user.image``; an item with
any other kind beside these, or with the kinds of two of them, is not claimed.

Cursor -- in a ``store.db`` or transcript session:

* a user message whose native ``providerOptions.cursor.isSummary`` is ``true``
  -> ``cursor_conversation_summary``: the summary Cursor writes when it
  compacts a conversation;
* the ``[Imported from Cursor ...]`` record this reader itself puts first in
  every Cursor session -> ``cursor_import_banner``. It comes from no native
  message; the conversion knows structurally that the record is its own.

Nothing here reads, copies or hashes text. A claim names only the contract,
its version, the kind, the native session and the record UUID, and is made
only when every structural fact holds: the record is the one record emitted
from that native row or message, by the conversion's ordinary user path, and
its UUID is unique among the records emitted. It never changes a record, its
UUID, or which records are emitted; the evidence rides beside them, keyed by
the record object the conversion produced.
"""
from __future__ import annotations

from collections import Counter, namedtuple

from .codex_witness import _PASSTHROUGH, _payload, flat_header, session_id

CONTRACT = "xtrace.automated_input"
VERSION = 1

# Native Codex content kind -> claimed kind. Closed: any kind not listed here
# (``user.text`` and ``user.image`` included) is never claimed.
CODEX_KINDS = {
    "multi_agent.subagent_notification": "codex_subagent_notification",
    "generic.turn_aborted": "codex_turn_aborted",
    "additional_content.codex_apps_open_page": "codex_apps_open_page",
}
CURSOR_SUMMARY = "cursor_conversation_summary"
CURSOR_BANNER = "cursor_import_banner"
KINDS = frozenset((*CODEX_KINDS.values(), CURSOR_SUMMARY, CURSOR_BANNER))


def _claim(kind, native_session_id, uuid):
    return {"contract": CONTRACT, "version": VERSION, "kind": kind,
            "native_session_id": native_session_id, "record_uuid": uuid}


def codex_kind(row):
    """The claimed kind of one native Codex row, or None.

    Every declared kind is the same recognised kind, one per content block,
    and every block is native input text.
    """
    payload = _payload(row)
    if not (isinstance(row, dict) and row.get("type") == "response_item"
            and payload.get("type") == "message" and payload.get("role") == "user"):
        return None
    passthrough = payload.get(_PASSTHROUGH)
    kinds = passthrough.get("content_item_kinds") if isinstance(passthrough, dict) else None
    content = payload.get("content")
    if not (isinstance(kinds, list) and isinstance(content, list) and len(kinds) > 0
            and len(kinds) == len(content)
            and all(isinstance(kind, str) for kind in kinds)
            and all(isinstance(item, dict) and item.get("type") == "input_text"
                    and isinstance(item.get("text"), str) for item in content)):
        return None
    first = kinds[0]
    if first not in CODEX_KINDS or any(kind != first for kind in kinds):
        return None
    return CODEX_KINDS[first]


# What ``codex_describe`` keeps of one rollout once its rows are gone: its
# native item counts and each own record its rows could claim, as
# ``(record, kind, item_id)``.
_Part = namedtuple("_Part", "rollout_id item_ids candidates")


def codex_segment(rollout_id, rows, own, sources, origins):
    """One rollout's facts for ``codex_describe``, taken while its rows are held.

    ``own`` are the records converted from this rollout's own rows (never
    inherited or copied context); ``sources`` maps ``id(record)`` to row index
    and ``origins`` ``id(record)`` to ``(record, derivation)``.
    """
    item_ids = Counter(_payload(row).get("id") for row in rows
                       if isinstance(row, dict) and row.get("type") == "response_item"
                       and isinstance(_payload(row).get("id"), str))
    by_row = {}
    for record in own:
        index = sources.get(id(record))
        if type(index) is int and 0 <= index < len(rows):
            by_row.setdefault(index, []).append(record)
    candidates = []
    for index in sorted(by_row):
        records = by_row[index]
        if len(records) != 1:
            continue
        record = records[0]
        origin = origins.get(id(record))
        if origin is None or origin[0] is not record or origin[1] != "user_message":
            continue
        kind = codex_kind(rows[index])
        if kind is None:
            continue
        item = _payload(rows[index]).get("id")
        if item is not None and (not isinstance(item, str) or not item):
            continue
        candidates.append((record, kind, item))
    return _Part(rollout_id, item_ids, candidates)


def codex_flat_segments(rows, records, sources, origins, native_session_id):
    """One flat rollout as ``codex_describe`` takes it: its records are its own
    only when the header names this session, flat, with no other
    ``session_meta`` and no subagent boundary."""
    header = rows[0] if rows else None
    owned = (flat_header(header, native_session_id)
             and [i for i, row in enumerate(rows)
                  if isinstance(row, dict) and row.get("type") == "session_meta"] == [0]
             and _payload(header).get("subagent_history_start_ordinal") is None)
    return [codex_segment(None, rows, records if owned else [], sources, origins)]


def codex_describe(parts, records, *, native_session_id, out):
    """Claim each qualifying record into ``out[id(record)] = (record, claim)``.

    A native item ID repeated anywhere in what was read, or a record UUID
    repeated among the records emitted, is never claimed.
    """
    if session_id(native_session_id) is None:
        return out
    uuids = Counter(record.get("uuid") for record in records)
    emitted = {id(record) for record in records}
    items = Counter()
    for part in parts:
        items.update(part.item_ids)
    for part in parts:
        for record, kind, item in part.candidates:
            uuid = record.get("uuid")
            if (id(record) not in emitted or not isinstance(uuid, str) or uuids[uuid] != 1
                    or (item is not None and items[item] != 1)):
                continue
            if id(record) in out:
                raise ValueError("automated input claimed twice")
            out[id(record)] = (record, _claim(kind, native_session_id, uuid))
    return out


def cursor_summary(message):
    """Whether a native Cursor message is marked as Cursor's own summary:
    a user message whose ``providerOptions.cursor.isSummary`` is ``true``."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    options = message.get("providerOptions")
    cursor = options.get("cursor") if isinstance(options, dict) else None
    return isinstance(cursor, dict) and cursor.get("isSummary") is True


def cursor_describe(messages, records, origins, *, native_session_id, out):
    """Claim Cursor's summaries and this reader's banner.

    ``messages`` are the native messages the conversion read, in order;
    ``origins`` maps ``id(record)`` to ``(record, derivation, message index)``
    as the conversion kept each one: ``import_banner`` for the record it
    synthesised, ``user_message`` for a user record made from that message.
    """
    if not isinstance(native_session_id, str) or not native_session_id.strip():
        return out
    uuids = Counter(record.get("uuid") for record in records)
    from_message = Counter(origin[2] for origin in origins.values()
                           if origin[1] == "user_message")
    banners = [origin for origin in origins.values() if origin[1] == "import_banner"]
    for n, record in enumerate(records):
        origin = origins.get(id(record))
        uuid = record.get("uuid")
        if (origin is None or origin[0] is not record or not isinstance(uuid, str)
                or uuids[uuid] != 1 or record.get("type") != "user"):
            continue
        kind = None
        if origin[1] == "import_banner" and n == 0 and len(banners) == 1:
            kind = CURSOR_BANNER
        elif origin[1] == "user_message":
            index = origin[2]
            if (type(index) is int and 0 <= index < len(messages)
                    and from_message[index] == 1 and cursor_summary(messages[index][0])):
                kind = CURSOR_SUMMARY
        if kind is None:
            continue
        if id(record) in out:
            raise ValueError("automated input claimed twice")
        out[id(record)] = (record, _claim(kind, native_session_id, uuid))
    return out
