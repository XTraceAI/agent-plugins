"""Bounded byte records using Python's universal-newline framing."""
from pathlib import Path


def open_lines(path):
    # Preserve CR/LF/CRLF and invalid bytes. Callers decode each returned row
    # as UTF-8; decoder read-ahead must not reject an unread later record.
    return Path(path).open("r", encoding="utf-8", errors="surrogateescape",
                           newline="")


def readline_bytes(handle, max_bytes: int) -> bytes:
    # The character bound also bounds allocation; enforce the exact byte
    # bound after losslessly recovering the original bytes.
    raw = handle.readline(max_bytes + 1).encode("utf-8", errors="surrogateescape")
    if len(raw) > max_bytes:
        raise ValueError("JSONL record exceeds its byte bound")
    return raw
