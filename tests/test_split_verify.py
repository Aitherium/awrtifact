"""split → verify round-trip: the honest signal of the whole tool.

A synthetic multi-part file (small --part-size) exercises the same code path
a 90 GB weight does: multi-slice layout, short tail, per-part digests, and
the concatenation check that proves the slices stitch back into the source.
"""

from __future__ import annotations

import os

from awrtifact import split as split_mod
from awrtifact import verify as verify_mod
from awrtifact.manifest import write as manifest_write


def _make_blob(path, size: int, seed: int = 0) -> None:
    """Deterministic pseudo-random bytes (repeatable across runs)."""
    import random

    rng = random.Random(seed)
    with open(path, "wb") as f:
        remaining = size
        while remaining > 0:
            chunk = rng.randbytes(min(8192, remaining))
            f.write(chunk)
            remaining -= len(chunk)


def test_round_trip(tmp_path):
    src = tmp_path / "blob.gguf"
    _make_blob(src, 500_000, seed=7)
    m = split_mod.split_file(src, part_size=120_000, out_dir=tmp_path)
    # Layout: 120k * 4 = 480k + 20k tail.
    assert len(m["parts"]) == 5
    assert m["total"] == 500_000
    assert sum(p["size"] for p in m["parts"]) == 500_000

    report = verify_mod.verify_manifest(m, tmp_path)
    assert report["ok"], report
    assert report["whole"]["sha256"] == m["sha256"]


def test_verify_catches_corruption(tmp_path):
    src = tmp_path / "blob.gguf"
    _make_blob(src, 200_000, seed=3)
    m = split_mod.split_file(src, part_size=100_000, out_dir=tmp_path)
    # Corrupt part1 in place — a byte flip, not a truncation.
    part1 = tmp_path / "blob.gguf.part1"
    with open(part1, "r+b") as f:
        f.seek(1000)
        f.write(b"X")
    report = verify_mod.verify_manifest(m, tmp_path)
    assert not report["ok"]
    assert not report["checks"][1]["ok"]
    assert report["checks"][1]["errors"]


def test_verify_catches_truncation(tmp_path):
    src = tmp_path / "blob.gguf"
    _make_blob(src, 200_000, seed=5)
    m = split_mod.split_file(src, part_size=100_000, out_dir=tmp_path)
    os.truncate(tmp_path / "blob.gguf.part1", 50_000)
    report = verify_mod.verify_manifest(m, tmp_path)
    assert not report["ok"]
    assert not report["checks"][1]["ok"]
    assert "size" in report["checks"][1]["errors"][0]


def test_split_refuses_oversized_part(tmp_path):
    import pytest

    src = tmp_path / "blob.gguf"
    _make_blob(src, 1000)
    with pytest.raises(ValueError, match="part_size"):
        split_mod.split_file(src, part_size=3 * 1024 * 1024 * 1024)


def test_manifest_write_load_roundtrip(tmp_path):
    src = tmp_path / "blob.gguf"
    _make_blob(src, 50_000)
    m = split_mod.split_file(src, part_size=10_000, out_dir=tmp_path)
    p = tmp_path / "manifest.json"
    manifest_write(m, p)
    from awrtifact import manifest as manifest_mod

    assert manifest_mod.load(p) == m
