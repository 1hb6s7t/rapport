"""Bounded-memory, authenticated local streams. No plaintext temporary files.

Each Fernet record authenticates its stream UUID and sequence number. The final
record authenticates total size and SHA256, so truncation/reordering fail closed.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

from rapport.security.crypto import Crypto

CHUNK_SIZE = 1024 * 1024
MAGIC = b"RAPPORT-ARCHIVE-1\n"
MAX_RECORD = 2 * CHUNK_SIZE


def write_chunks(path: Path, crypto: Crypto, chunks: Iterable[bytes], max_bytes: int) -> tuple[int, str]:
    stream_id = uuid.uuid4().bytes
    digest = hashlib.sha256()
    size = 0
    index = 0
    temporary = path.with_suffix(".part")
    try:
        with temporary.open("xb") as target:
            target.write(MAGIC + stream_id)
            for data in chunks:
                for start in range(0, len(data), CHUNK_SIZE):
                    block = data[start : start + CHUNK_SIZE]
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError("archive_asset_too_large")
                    digest.update(block)
                    token = crypto.encrypt(stream_id + struct.pack(">Q", index) + b"D" + block)
                    target.write(struct.pack(">I", len(token)) + token)
                    index += 1
            token = crypto.encrypt(stream_id + struct.pack(">Q", index) + b"E" + struct.pack(">Q", size) + digest.digest())
            target.write(struct.pack(">I", len(token)) + token)
            target.flush()
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


def read_chunks(path: Path, crypto: Crypto) -> Iterator[bytes]:
    digest = hashlib.sha256()
    index = size = 0
    with path.open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise ValueError("archive_corrupt_header")
        stream_id = source.read(16)
        if len(stream_id) != 16:
            raise ValueError("archive_truncated")
        while True:
            prefix = source.read(4)
            if len(prefix) != 4:
                raise ValueError("archive_truncated")
            length = struct.unpack(">I", prefix)[0]
            if length > MAX_RECORD or length < 100:
                raise ValueError("archive_corrupt_record")
            token = source.read(length)
            if len(token) != length:
                raise ValueError("archive_truncated")
            record = crypto.decrypt(token)
            if record[:16] != stream_id or len(record) < 25 or struct.unpack(">Q", record[16:24])[0] != index:
                raise ValueError("archive_corrupt_order")
            if record[24:25] == b"E":
                if record[25:] != struct.pack(">Q", size) + digest.digest() or source.read(1):
                    raise ValueError("archive_corrupt_digest")
                return
            if record[24:25] != b"D" or len(record) > CHUNK_SIZE + 25:
                raise ValueError("archive_corrupt_record")
            data = record[25:]
            digest.update(data)
            size += len(data)
            index += 1
            yield data
