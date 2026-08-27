"""Streaming hashing — one pass, both scopes.

split() needs the WHOLE-file sha256 AND a per-part sha256 in a single read;
reading the file twice at these sizes (tens of GB) is not acceptable. This
module feeds every chunk to both hashers as it flows.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Read buffer: 4 MiB — the same size the original PowerShell splitter used.
BUFFER = 4 * 1024 * 1024


def sha256_file(path: Path) -> str:
    """Whole-file sha256, streamed. The one scope fetch/verify trust."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BUFFER)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class DualHasher:
    """Feeds every byte to a whole-file hasher and a running part hasher.

    The caller closes the current part (collecting the part digest) and opens
    the next; the whole-file hasher is never reset.
    """

    def __init__(self) -> None:
        self.whole = hashlib.sha256()
        self.part = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        self.whole.update(chunk)
        self.part.update(chunk)

    def close_part(self) -> str:
        digest = self.part.hexdigest()
        self.part = hashlib.sha256()
        return digest

    def whole_hex(self) -> str:
        return self.whole.hexdigest()
