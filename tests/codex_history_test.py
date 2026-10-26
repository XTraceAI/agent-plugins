"""Synthetic rollout fixtures equivalent to the pinned checkout's history data.

The first rollout contains a shared prefix and abandoned work; the continuation
inherits only the prefix and adds a retry. Readers must count six unique records.
This supplies data only and imports no producer module.
"""
import json
import os

SID = "11111111-2222-3333-4444-555555555555"
RID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FORK = "0f0f0f0f-1111-4222-8333-444444444444"
STAMP = "2026-01-01T00:00:00.123456789012Z"


def row(ordinal, kind, payload):
    return {"ordinal": ordinal, "timestamp": STAMP, "type": kind, "payload": payload}


def message(ordinal, role, text):
    return row(ordinal, "response_item", {"type": "message", "role": role,
               "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]})


def tokens(ordinal, inputs, outputs):
    return row(ordinal, "event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": inputs, "output_tokens": outputs}}})


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    os.utime(path, (1788825600, 1788825600))
    return path


def fixture(home):
    folder = home / ".codex/sessions/2026/01/01"
    meta = {"id": SID, "timestamp": STAMP, "cwd": "/synthetic/project",
            "originator": "codex_cli", "history_mode": "paginated"}
    parent_rows = [row(0, "session_meta", meta), message(1, "user", "shared ask"),
                   message(2, "assistant", "shared reply"), tokens(3, 10, 2),
                   message(4, "user", "abandoned ask"), message(5, "assistant", "abandoned reply"),
                   tokens(6, 15, 3)]
    parent = write_jsonl(folder / f"rollout-2026-01-01T00-00-00-{SID}.jsonl", parent_rows)
    cutoff = sum(map(len, parent.read_bytes().splitlines(keepends=True)[:4]))
    child_meta = {**meta, "timestamp": "2026-01-02T00:00:00Z",
                  "history_base": {"thread_id": SID, "end_ordinal_exclusive": 4, "end_byte_offset": cutoff}}
    child_rows = [row(4, "session_meta", child_meta), message(5, "user", "retry ask"),
                  message(6, "assistant", "retry reply"), tokens(7, 17, 4)]
    child = write_jsonl(folder / f"rollout-2026-01-02T00-00-00-{SID}_{RID}.jsonl", child_rows)
    return parent, child, parent_rows, child_rows


def fork_fixture(home, base_path, base_rows, cutoff_rows):
    cutoff = sum(map(len, base_path.read_bytes().splitlines(keepends=True)[:cutoff_rows]))
    start = base_rows[cutoff_rows - 1]["ordinal"] + 1
    meta = {"id": FORK, "timestamp": "2026-01-05T00:00:00Z", "cwd": "/synthetic/project",
            "originator": "codex_cli", "history_mode": "paginated",
            "forked_from_id": SID, "forked_from_ordinal_exclusive": start,
            "history_base": {"thread_id": SID, "end_ordinal_exclusive": start,
                             "end_byte_offset": cutoff}}
    rows = [row(start, "session_meta", meta), message(start + 1, "user", "fork ask"),
            message(start + 2, "assistant", "fork reply"), tokens(start + 3, 25, 6)]
    path = write_jsonl(home / f".codex/sessions/2026/01/05/rollout-2026-01-05T00-00-00-{FORK}.jsonl", rows)
    return path, rows
