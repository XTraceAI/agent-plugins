"""Optional, text-free Human length evidence from the same native conversion.

Only the complete request/opening/image/closing shape is recognized. Neither
native content nor canonical records are modified or retained in this evidence.
"""
from collections import Counter, namedtuple
import re

from . import codex
from .codex_witness import flat_header, session_id, _payload
from . import _parseable_timestamp

CONTRACT = "xtrace.human_input"
VERSION = 1
_OPEN = re.compile(r'<image name=\[Image #1\] path="[^"\r\n]+">')
_Part = namedtuple("_Part", "rollout_id item_ids candidates")


def segment(rollout_id, rows, own, sources, origins):
    item_ids = Counter(_payload(row).get("id") for row in rows
                       if row.get("type") == "response_item"
                       and isinstance(_payload(row).get("id"), str))
    by_row = {}
    for record in own:
        index = sources.get(id(record))
        if type(index) is int and 0 <= index < len(rows):
            by_row.setdefault(index, []).append(record)
    candidates = []
    for index, records in by_row.items():
        if len(records) != 1:
            continue
        record = records[0]
        origin = origins.get(id(record))
        if origin is None or origin[0] is not record or origin[1] != "user_message":
            continue
        row = rows[index]
        payload = _payload(row)
        blocks = payload.get("content")
        if (row.get("type") != "response_item" or payload.get("type") != "message"
                or payload.get("role") != "user" or not isinstance(blocks, list)
                or len(blocks) != 4 or not all(isinstance(b, dict) for b in blocks)):
            continue
        request, opening, image, closing = blocks
        if (any(b.get("type") != "input_text" or not isinstance(b.get("text"), str)
                for b in (request, opening, closing))
                or not _OPEN.fullmatch(opening["text"])
                or image.get("type") != "input_image" or "text" in image
                or not isinstance(image.get("image_url"), str) or not image["image_url"]
                or closing["text"] != "</image>"):
            continue
        original = codex._text_of(blocks, strict=True).strip()
        # Decline if any other canonical cleanup changed the native text.
        if record.get("message", {}).get("content") != original:
            continue
        retained = len(request["text"].strip())
        item = payload.get("id")
        if item is not None and (not isinstance(item, str) or not item):
            continue
        candidates.append((record, index, item, len(original), retained))
    return _Part(rollout_id, item_ids, candidates)


def flat_segments(rows, records, sources, origins, native_session_id):
    header = rows[0] if rows else None
    owned = (flat_header(header, native_session_id)
             and [i for i, row in enumerate(rows) if row.get("type") == "session_meta"] == [0]
             and header["payload"].get("subagent_history_start_ordinal") is None)
    return [segment(None, rows, records if owned else [], sources, origins)]


def describe(parts, records, *, native_session_id, out):
    if session_id(native_session_id) is None:
        return
    uuids = Counter(record.get("uuid") for record in records)
    emitted = {id(record) for record in records}
    items = sum((part.item_ids for part in parts), Counter())
    for part in parts:
        for record, index, item, original, retained in part.candidates:
            uuid, timestamp = record.get("uuid"), record.get("timestamp")
            if (id(record) not in emitted or not isinstance(uuid, str) or uuids[uuid] != 1
                    or not isinstance(timestamp, str) or not _parseable_timestamp(timestamp)
                    or (item is not None and items[item] != 1)):
                continue
            # A source coordinate also identifies legacy native items without IDs.
            identity = f"{native_session_id}:{part.rollout_id or 'flat'}:row:{index}"
            if len(identity.encode("utf-8")) > 256:
                continue
            if id(record) in out:
                raise ValueError("Human adjustment claimed twice")
            out[id(record)] = (record, {
                "contract": CONTRACT, "version": VERSION, "reason": "image_wrapper",
                "record_uuid": uuid, "original_ts": timestamp,
                "original_length": original, "retained_length": retained,
                "native_item_id": identity, "native_session_id": native_session_id,
            })
