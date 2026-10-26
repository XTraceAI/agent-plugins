"""Where each canonical Codex record came from, as metadata only.

A witness is built from the rows a conversion just consumed, for the records
that same conversion produced. It names the source row by position and native
identifiers and never carries text, tool input or output, or anything computed
from them. Physical file facts (path, device, inode, size and times) are added
by the caller that opened the file; the reader may only hold a snapshot or the
bytes, so it cannot state them itself.

Only a flat rollout is witnessed: one file, positively shown by its own name
and header to hold the whole session (see ``flat_header``). A paginated
session is refused rather than described from any one of its files.

Guardian facts are reported only for user inputs whose rollout header is typed
as a guardian review. Each carries a closed set of structural checks, and its
status is derived from the emitted facts alone (``guardian_status``): it is
``witnessed`` only when every check holds, and otherwise abstains with a fixed
reason code. Each witness describes the whole file as this read observed it;
an append can change the checks of an earlier input in a later read, which is
a new observation, not a change to an earlier one.
"""
from __future__ import annotations

import re

CONTRACT = "memhub.codex.source_witness"
VERSION = 2

# Native identifiers are copied only in the exact shapes Codex writes for
# that field, so no identifier field can carry text: a value of any other
# shape, however short, is reported as absent. Thread and turn IDs are
# canonical UUIDs; response items and ledger responses are a fixed prefix
# over a UUID or 50 lowercase hex digits. Tool call IDs have no established
# native grammar, so they are never copied.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_HEX50 = r"[0-9a-f]{50}"
_THREAD_ID = re.compile(_UUID)
_TURN_ID = re.compile(_UUID)
_ITEM_ID = {"message": re.compile(rf"msg_(?:{_UUID}|{_HEX50})"),
            "reasoning": re.compile(rf"rs_{_HEX50}"),
            "compaction": re.compile(rf"cmp_{_HEX50}")}
_RESPONSE_ID = re.compile(rf"resp_{_HEX50}")
# A witnessed session is a Codex rollout by name as well as by header: the
# file Codex writes is ``rollout-<YYYY-MM-DDThh-mm-ss>-<session UUID>.jsonl``,
# and a paginated continuation adds ``_<rollout UUID>`` to that session UUID.
# Continuations are parsed only so they can be recognized and refused.
_ROLLOUT_NAME = re.compile(
    rf"rollout-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}"
    rf"-({_UUID})(?:_({_UUID}))?\.jsonl")
# Codex's session history modes are ``legacy`` and ``paginated``; a rollout
# written before the field existed omits it. Only those two flat spellings
# are a flat rollout's: ``null`` or any other value is not evidence of one.
_FLAT_HISTORY_MODES = ("legacy",)

_USER_INPUTS = ("user_message", "recovered_user_message")
_TURN_CLOSE = ("task_complete", "turn_aborted")
_PASSTHROUGH = "internal_chat_message_metadata_passthrough"
# The closed set of structural checks each guardian input carries, in order.
_CHECKS = ("item_id_occurrences", "open_turn_id", "named_turn_starts",
           "named_turn_matching_closes", "turn_lifecycle_valid", "turn_complete",
           "user_text_kinds_match")


def _shaped(grammar, value):
    return value if isinstance(value, str) and grammar and grammar.fullmatch(value) else None


def session_id(value):
    """``value`` when it is a canonical lowercase session UUID, else None."""
    return _shaped(_THREAD_ID, value)


def rollout_name(name):
    """``(session UUID, rollout UUID or None)`` a Codex rollout file name
    states, or None when ``name`` is not one exactly."""
    match = _ROLLOUT_NAME.fullmatch(name) if isinstance(name, str) else None
    return (match[1], match[2]) if match else None


def flat_header(header, native_session_id):
    """Whether a rollout header positively states a flat session of this ID.

    Flat means the file is the whole session: its opening ``session_meta``
    names this session, it has no history reference, and its history mode is
    absent or ``legacy``. A paginated mode, any non-null ``history_base`` and
    any unknown or malformed value are not flat, so a paginated root is never
    mistaken for a complete session.
    """
    if not isinstance(header, dict) or header.get("type") != "session_meta":
        return False
    payload = header.get("payload")
    return (isinstance(payload, dict) and session_id(native_session_id) is not None
            and payload.get("id") == native_session_id
            and payload.get("history_base") is None
            and ("history_mode" not in payload
                 or payload["history_mode"] in _FLAT_HISTORY_MODES))


def _turn_id(value):
    return _shaped(_TURN_ID, value)


def _item_id(payload):
    return _shaped(_ITEM_ID.get(payload.get("type")), payload.get("id"))


def _payload(row):
    payload = row.get("payload") if isinstance(row, dict) else None
    return payload if isinstance(payload, dict) else {}


def _event(row, kind):
    return row.get("type") == "event_msg" and _payload(row).get("type") == kind


def _user_message(row):
    payload = _payload(row)
    return (row.get("type") == "response_item" and payload.get("type") == "message"
            and payload.get("role") == "user")


def _guardian_root(header, native_session_id):
    """``(marked, verified_root)`` for one rollout header.

    ``marked`` says the header claims a guardian review at all. The root is
    verified only when both native markers are exact and it names a parent
    other than itself.
    """
    if not isinstance(header, dict) or header.get("type") != "session_meta":
        return False, None
    payload = _payload(header)
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    typed = source == {"subagent": {"other": "guardian"}}
    marked = (typed or payload.get("thread_source") == "guardian_review"
              or (isinstance(subagent, dict) and subagent.get("other") == "guardian"))
    if not marked:
        return False, None
    parent = _shaped(_THREAD_ID, payload.get("parent_thread_id"))
    if (not typed or payload.get("thread_source") != "guardian_review"
            or parent is None or parent == native_session_id
            or payload.get("id") != native_session_id):
        return True, None
    return True, {"subagent_other": "guardian", "thread_source": "guardian_review",
                  "parent_thread_id": parent}


def _derivation(row, entry, record):
    # Ledger records are added after conversion, so the row type decides them
    # before any origin entry is consulted.
    if isinstance(row, dict) and row.get("type") == "token_usage_record":
        return "ledger_usage"
    if entry is None or entry[0] is not record:
        raise ValueError("record has no conversion origin")
    return entry[1]


def _native_ids(row):
    payload = _payload(row)
    passthrough = payload.get(_PASSTHROUGH)
    kind = row.get("type") if isinstance(row, dict) else None
    return {
        "item_id": _item_id(payload) if kind == "response_item" else None,
        "call_id": None,
        "turn_id": (_turn_id(passthrough.get("turn_id"))
                    if kind == "response_item" and isinstance(passthrough, dict) else None),
        "response_id": (_shaped(_RESPONSE_ID, payload.get("response_id"))
                        if kind == "token_usage_record" else None),
    }


def _user_text_only(payload):
    """Native kinds declare exactly the text items the message holds."""
    passthrough = payload.get(_PASSTHROUGH)
    kinds = passthrough.get("content_item_kinds") if isinstance(passthrough, dict) else None
    content = payload.get("content")
    return (isinstance(kinds, list) and isinstance(content, list) and kinds
            and len(kinds) == len(content)
            and all(kind == "user.text" for kind in kinds)
            and all(isinstance(item, dict) and item.get("type") == "input_text"
                    for item in content))


class _Turns:
    """Turn structure of a flat rollout's rows, from native events only."""

    def __init__(self, rows, input_rows):
        self.starts = {}          # turn_id -> [row index of each task_started]
        self.closes = {}          # turn_id -> closes that name it, anywhere
        self.closed = set()       # turns a close naming them ended while open
        self.open_at = {}         # input row -> (turn_id or None, task_started row)
        self.item_ids = {}        # native item id -> occurrences
        # Turns whose lifecycle is not one start and one matching close: a
        # close that names another turn or none, a second close, or a start
        # that supersedes a turn still open. Every input of such a turn
        # abstains, including inputs that came before the fault was seen. A
        # turn still open at the end of the rollout is not invalid, only
        # incomplete.
        self.invalid = set()
        open_turn = None
        for index in range(len(rows)):
            row = rows[index]
            payload = _payload(row)
            if _event(row, "task_started"):
                turn = _turn_id(payload.get("turn_id"))
                if turn is not None:
                    self.starts.setdefault(turn, []).append(index)
                if open_turn is not None and open_turn[0] is not None:
                    self.invalid.add(open_turn[0])
                open_turn = (turn, index)
            elif row.get("type") == "event_msg" and payload.get("type") in _TURN_CLOSE:
                # Any close ends whatever turn is open, so no later input can
                # sit in a turn whose end was malformed.
                named = _turn_id(payload.get("turn_id"))
                current = open_turn[0] if open_turn is not None else None
                if named is not None:
                    self.closes[named] = self.closes.get(named, 0) + 1
                if current is None or named != current:
                    self.invalid.update(turn for turn in (named, current) if turn is not None)
                else:
                    self.closed.add(current)
                open_turn = None
            item = payload.get("id")
            if row.get("type") == "response_item" and isinstance(item, str):
                self.item_ids[item] = self.item_ids.get(item, 0) + 1
            if index in input_rows:
                self.open_at[index] = open_turn
        # Every emitted input that sits in a turn or names it counts toward
        # that turn, including inputs without native turn or kind metadata.
        self.members = {}
        for index in input_rows:
            claimed = set()
            opened = self.open_at.get(index)
            if opened is not None and opened[0] is not None:
                claimed.add(opened[0])
            passthrough = _payload(rows[index]).get(_PASSTHROUGH)
            named = (_turn_id(passthrough.get("turn_id"))
                     if isinstance(passthrough, dict) else None)
            if named is not None:
                claimed.add(named)
            for turn in claimed:
                self.members.setdefault(turn, set()).add(index)

    def summary(self, rows, turn):
        members = self.members.get(turn, ())
        without_turn = without_kinds = 0
        for index in members:
            passthrough = _payload(rows[index]).get(_PASSTHROUGH)
            if not isinstance(passthrough, dict) or _turn_id(passthrough.get("turn_id")) is None:
                without_turn += 1
            if (not isinstance(passthrough, dict)
                    or not isinstance(passthrough.get("content_item_kinds"), list)):
                without_kinds += 1
        return {"emitted": len(members), "without_turn_id": without_turn,
                "without_content_kinds": without_kinds}

    def checks(self, index, item, named):
        """The structural facts of one owned input, each null when the input
        gives nothing to evaluate it against."""
        opened = self.open_at.get(index)
        starts = len(self.starts.get(named, ())) if named is not None else None
        return {
            "item_id_occurrences": self.item_ids.get(item, 0) if item is not None else None,
            "open_turn_id": opened[0] if opened is not None else None,
            "named_turn_starts": starts,
            "named_turn_matching_closes": (self.closes.get(named, 0)
                                           if named is not None else None),
            "turn_lifecycle_valid": (starts == 1 and named not in self.invalid
                                     if starts else None),
            "turn_complete": named in self.closed if starts == 1 else None,
        }


def _guardian(rows, index, provenance, root, turns):
    """Guardian facts for one emitted user input. Its status is filled in
    later, from these facts alone, by ``guardian_status``."""
    block = {"status": None, "reason": None, "root": root,
             "turn": None, "turn_user_inputs": None}
    payload = _payload(rows[index])
    passthrough = payload.get(_PASSTHROUGH)
    named = (_turn_id(passthrough.get("turn_id"))
             if isinstance(passthrough, dict) else None)
    opened = turns.open_at.get(index) if turns is not None else None
    turn = opened[0] if opened is not None and opened[0] is not None else named
    # Turn facts describe the rollout's own turns, so copied inputs have none.
    owned = turns is not None and provenance == "own"
    if owned and turn is not None:
        starts = turns.starts.get(turn, [])
        started = starts[0] if len(starts) == 1 else None
        root_turn = (_turn_id(_payload(rows[started]).get("root_turn_id"))
                     if started is not None else None)
        block["turn"] = {"turn_id": turn, "root_turn_id": root_turn,
                         "task_started_row": started}
        block["turn_user_inputs"] = turns.summary(rows, turn)
    checks = (turns.checks(index, _item_id(payload), named) if owned
              else dict.fromkeys(_CHECKS[:-1]))
    checks["user_text_kinds_match"] = _user_text_only(payload)
    block["checks"] = checks
    return block


def _count(value):
    return value if type(value) is int else None


def guardian_status(witness):
    """``(status, reason)`` that one witness's own emitted facts entail.

    Only ``witnessed`` is a positive statement, and it is derived here from
    the metadata on the witness line alone, so a consumer holding that line
    can evaluate it again without the rollout. Anything missing, of another
    type or out of shape abstains.
    """
    guardian = witness.get("guardian")
    if not isinstance(guardian, dict):
        return None
    root = guardian.get("root")
    checks = guardian.get("checks")
    checks = checks if isinstance(checks, dict) and set(checks) == set(_CHECKS) else {}
    native = witness.get("native") if isinstance(witness.get("native"), dict) else {}
    turn = guardian.get("turn") if isinstance(guardian.get("turn"), dict) else {}
    inputs = (guardian.get("turn_user_inputs")
              if isinstance(guardian.get("turn_user_inputs"), dict) else {})
    item = native.get("item_id")
    named = _turn_id(native.get("turn_id"))
    closes = _count(checks.get("named_turn_matching_closes"))
    root_turn = _turn_id(turn.get("root_turn_id"))
    parent = (_shaped(_THREAD_ID, root.get("parent_thread_id"))
              if isinstance(root, dict) else None)
    failures = (
        ("malformed_guardian_root",
         parent is None or parent == witness.get("native_session_id")
         or root != {"subagent_other": "guardian", "thread_source": "guardian_review",
                     "parent_thread_id": parent}),
        ("provenance_unknown", witness.get("provenance") != "own"),
        ("context_injection", witness.get("derivation") != "user_message"),
        ("missing_message_id", _shaped(_ITEM_ID["message"], item) is None),
        ("duplicate_message_id", _count(checks.get("item_id_occurrences")) != 1),
        ("missing_turn_id", named is None),
        ("no_open_turn", _turn_id(checks.get("open_turn_id")) is None),
        ("turn_mismatch", checks.get("open_turn_id") != named),
        ("duplicate_turn", _count(checks.get("named_turn_starts")) != 1),
        ("turn_lifecycle_invalid",
         checks.get("turn_lifecycle_valid") is not True or closes is None or closes > 1),
        ("turn_incomplete", checks.get("turn_complete") is not True or closes != 1),
        ("missing_root_turn",
         root_turn is None or turn.get("turn_id") != named
         or type(turn.get("task_started_row")) is not int),
        ("root_is_turn", root_turn == named),
        ("input_kinds_not_user_text", checks.get("user_text_kinds_match") is not True),
        ("ambiguous_turn", _count(inputs.get("emitted")) != 1),
    )
    for reason, failed in failures:
        if failed:
            return "abstained", reason
    return "witnessed", None


def describe(rows, spans, records, sources, origins, *, native_session_id, out,
             usage_sources=None):
    """Describe one flat rollout's records into ``out[id(record)] = (record, witness)``.

    ``rows`` and ``spans`` are the rollout as parsed; ``records`` is every
    record its conversion produced. ``usage_sources`` maps ``id(record)`` to
    the ledger rows that gave an existing record its response identity and
    usage. Physical segment facts are left for the caller to add.
    """
    if session_id(native_session_id) is None:
        # Checked here as well as where the CLI selects the source, so no
        # caller can serialize a header value that is not a session UUID.
        raise ValueError("native session ID is not a canonical session UUID")
    header = rows[0] if rows else None
    if not flat_header(header, native_session_id):
        # A paginated root is not the whole session, and no other file of it
        # was read: nothing here may be described as if it were.
        raise ValueError("only a flat rollout is witnessed")
    if len(spans) != len(rows):
        raise ValueError("row positions do not cover the rows")
    usage_sources = usage_sources or {}

    def located(index):
        ordinal = rows[index].get("ordinal")
        return {"index": index, "ordinal": ordinal if type(ordinal) is int else None,
                "byte_start": spans[index][0], "byte_end": spans[index][1]}
    metas = [i for i, row in enumerate(rows) if row.get("type") == "session_meta"]
    knowable = (metas == [0]
                and _payload(header).get("subagent_history_start_ordinal") is None)
    marked, root = _guardian_root(header, native_session_id)
    records = [(record, "own" if knowable else "unknown") for record in records]
    kinds = {}
    for record, provenance in records:
        index = sources.get(id(record))
        if type(index) is not int or not 0 <= index < len(rows):
            raise ValueError("record has no source row")
        kinds[id(record)] = _derivation(rows[index], origins.get(id(record)), record)
    input_rows = {sources[id(record)] for record, provenance in records
                  if provenance == "own" and kinds[id(record)] in _USER_INPUTS}
    turns = _Turns(rows, input_rows) if marked and knowable else None
    for record, provenance in records:
        if id(record) in out:
            raise ValueError("record described twice")
        index = sources[id(record)]
        row = rows[index]
        derivation = kinds[id(record)]
        guardian = (_guardian(rows, index, provenance, root, turns)
                    if marked and derivation in _USER_INPUTS else None)
        origin = located(index)
        contributors = [{"role": "origin", "rollout_id": None, "row": origin}]
        ledgers = usage_sources.get(id(record), [])
        if ledgers:
            # A ledger row set this record's response identity and usage. It
            # is named beside the origin; there is never more than one, and
            # never one for a user input.
            if (len(ledgers) != 1 or derivation in _USER_INPUTS
                    or not 0 <= ledgers[0] < len(rows) or ledgers[0] == index
                    or rows[ledgers[0]].get("type") != "token_usage_record"):
                raise ValueError("record usage has no single ledger row")
            contributors.append({"role": "usage_ledger", "rollout_id": None,
                                 "row": located(ledgers[0])})
        witness = {
            "contract": CONTRACT,
            "version": VERSION,
            "native_session_id": native_session_id,
            "segment": {"history": "flat", "rollout_id": None},
            "provenance": provenance,
            "identity_origin": origin,
            "contributors": contributors,
            "derivation": derivation,
            "native": _native_ids(row),
            "guardian": guardian,
        }
        if guardian is not None:
            guardian["status"], guardian["reason"] = guardian_status(witness)
        out[id(record)] = (record, witness)
    return out
