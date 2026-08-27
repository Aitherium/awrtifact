"""`awrtifact split` — stream-rewrite one artifact into `.partN` slices.

The original lane did this in PowerShell (`_split27b.ps1`); this is the
productized form, with two additions that mattered in production:

1. PER-PART sha256 in the manifest (the PowerShell lane recorded sizes only),
   so `verify` can prove each slice against the release independently — a
   corrupted part is found BEFORE the whole-file check, and a re-upload of
   one part does not require re-verifying the other twenty.
2. One-pass hashing: the whole-file digest and every part digest are computed
   in a single read (tens of GB files are read once, never twice).

Sanity is asserted AFTER writing (Σ parts == source size) and the command
refuses to write anything unless it holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import hashes
from .manifest import DEFAULT_PART_SIZE, GITHUB_ASSET_CAP


def _out_name(src: Path, out_dir: Path, part_idx: int) -> Path:
    return out_dir / f"{src.name}.part{part_idx}"


def split_file(
    src: Path,
    part_size: int = DEFAULT_PART_SIZE,
    out_dir: Path | None = None,
) -> dict:
    """Split `src` into `.partN` slices and return the manifest dict."""
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"not a file: {src}")
    if part_size <= 0 or part_size > GITHUB_ASSET_CAP:
        raise ValueError(
            f"part_size must be in (0, {GITHUB_ASSET_CAP}]: {part_size}"
        )
    out = Path(out_dir) if out_dir else src.parent
    out.mkdir(parents=True, exist_ok=True)

    total = src.stat().st_size
    dual = hashes.DualHasher()
    parts: list[dict] = []
    idx = 0
    done = 0
    with open(src, "rb") as f:
        while True:
            part_path = _out_name(src, out, idx)
            part_size_left = part_size
            with open(part_path, "wb") as dst:
                while part_size_left > 0:
                    chunk = f.read(min(hashes.BUFFER, part_size_left))
                    if not chunk:
                        break
                    dst.write(chunk)
                    dual.update(chunk)
                    part_size_left -= len(chunk)
                    done += len(chunk)
            part_bytes = part_path.stat().st_size
            if part_bytes == 0:
                # The source is a multiple of part_size: no trailing empty part.
                part_path.unlink()
                break
            parts.append(
                {
                    "name": part_path.name,
                    "size": part_bytes,
                    "sha256": dual.close_part(),
                }
            )
            _progress(done, total)
            idx += 1

    if done != total:
        raise ValueError(f"sanity: wrote {done} bytes, source is {total}")

    manifest = {
        "name": src.name,
        "total": total,
        "sha256": dual.whole_hex(),
        "part_size": part_size,
        "parts": parts,
    }
    return manifest


def _progress(done: int, total: int) -> None:
    if sys.stderr.isatty():
        pct = 100.0 * done / total if total else 0.0
        print(f"\rsplit: {done}/{total} bytes ({pct:.1f}%)", end="", file=sys.stderr)
        if done == total:
            print(file=sys.stderr)
