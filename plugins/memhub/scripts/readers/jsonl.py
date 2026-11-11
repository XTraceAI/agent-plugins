"""Bounded byte records using Python's universal-newline framing.

Also the one place a reader learns that a transcript is zstd-compressed.
Codex compresses rollouts it is done with to ``rollout-*.jsonl.zst`` (59 of 61
on one real machine), and a compressed rollout is still a session: import,
audit and agent capture all have to read it. Compression is recognised by the
zstd frame magic, never by the file name, so a private snapshot copied under
another name (``readers_cli``) or an already-open handle reads the same.

Decompression uses the standard library's ``compression.zstd`` (Python 3.14+)
when it is there, else the ``zstd`` command-line tool. Neither present —
``/usr/bin/python3`` on macOS is 3.9 and ships no zstd tool — raises
``ZstdUnavailable``, an ``OSError``, so every caller that already treats an
unreadable transcript as "skip it and say why" keeps doing exactly that.
"""
import io
import shutil
import subprocess
from pathlib import Path

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ZSTD_SUFFIX = ".zst"
# A compressed rollout is decompressed whole. Real rollouts run to tens of MB;
# this is only the line between a large session and a decompression bomb.
MAX_DECOMPRESSED = 1 << 31
_CLI_TIMEOUT_S = 60


class ZstdUnavailable(OSError):
    """A zstd-compressed transcript, and nothing on this machine reads zstd."""


def is_zstd(data: bytes) -> bool:
    return data[:4] == ZSTD_MAGIC


def _stdlib_zstd():
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        return None
    return zstd


def zstd_available() -> bool:
    """Whether this interpreter can read a ``.jsonl.zst`` at all."""
    return _stdlib_zstd() is not None or shutil.which("zstd") is not None


def decompress(data: bytes) -> bytes:
    zstd = _stdlib_zstd()
    if zstd is not None:
        out = zstd.decompress(data)
    else:
        tool = shutil.which("zstd")
        if tool is None:
            raise ZstdUnavailable(
                "zstd-compressed transcript, but this Python has no "
                "compression.zstd and no zstd tool is on PATH")
        try:
            done = subprocess.run([tool, "-dcq"], input=data, capture_output=True,
                                  timeout=_CLI_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ZstdUnavailable(f"zstd tool failed: {exc}") from exc
        if done.returncode != 0:
            raise ValueError("zstd could not decompress the transcript: "
                             + done.stderr.decode("utf-8", "replace")[:200])
        out = done.stdout
    if len(out) > MAX_DECOMPRESSED:
        raise ValueError("decompressed transcript exceeds its bound")
    return out


def read_bytes(path) -> bytes:
    """A transcript's bytes, decompressed when it is a zstd frame."""
    data = Path(path).read_bytes()
    return decompress(data) if is_zstd(data) else data


def logical_size(path) -> int:
    """The size a growth watermark compares: the decompressed size of a zstd
    transcript, else the file's own size. Keyed on content, so the rename of
    ``rollout-X.jsonl`` to ``rollout-X.jsonl.zst`` is not a shrink."""
    p = Path(path)
    with p.open("rb") as handle:
        head = handle.read(4)
    if not is_zstd(head):
        return p.stat().st_size
    return len(read_bytes(p))


def open_lines(path):
    # Preserve CR/LF/CRLF and invalid bytes. Callers decode each returned row
    # as UTF-8; decoder read-ahead must not reject an unread later record.
    if hasattr(path, "read"):
        # An already-open binary handle: the caller validated the source when
        # it opened it and must not have it reopened by name.
        raw = _maybe_decompressed(path)
        return io.TextIOWrapper(raw, encoding="utf-8", errors="surrogateescape",
                                newline="")
    p = Path(path)
    with p.open("rb") as handle:
        head = handle.read(4)
    if is_zstd(head):
        return io.TextIOWrapper(io.BytesIO(read_bytes(p)), encoding="utf-8",
                                errors="surrogateescape", newline="")
    return p.open("r", encoding="utf-8", errors="surrogateescape", newline="")


def _maybe_decompressed(handle):
    """The handle itself, or an in-memory decompressed copy of a zstd one.
    Only a seekable handle can be sniffed without losing bytes."""
    try:
        start = handle.tell()
        head = handle.read(4)
        handle.seek(start)
    except (OSError, ValueError, AttributeError):
        return handle
    if not is_zstd(head):
        return handle
    return io.BytesIO(decompress(handle.read()))


def readline_bytes(handle, max_bytes: int) -> bytes:
    # The character bound also bounds allocation; enforce the exact byte
    # bound after losslessly recovering the original bytes.
    raw = handle.readline(max_bytes + 1).encode("utf-8", errors="surrogateescape")
    if len(raw) > max_bytes:
        raise ValueError("JSONL record exceeds its byte bound")
    return raw
