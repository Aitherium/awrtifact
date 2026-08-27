"""manifest.json validation — every invariant must actually fire."""

from __future__ import annotations

import json

import pytest
from awrtifact import manifest


def _good() -> dict:
    return {
        "name": "model.gguf",
        "total": 3000,
        "sha256": "a" * 64,
        "part_size": 1900000000,
        "parts": [
            {"name": "model.gguf.part0", "size": 2000, "sha256": "b" * 64},
            {"name": "model.gguf.part1", "size": 1000, "sha256": "c" * 64},
        ],
    }


def test_accepts_valid(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(_good()))
    assert manifest.load(p)["total"] == 3000


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda d: d.update(total=3001), "total"),
        (lambda d: d["parts"][0].update(size=1999), "total"),
        (lambda d: d["parts"][0].update(name="model.gguf.part9"), "expected"),
        (lambda d: d["parts"].append(
            {"name": "model.gguf.part2", "size": 1, "sha256": "d" * 64}), "total"),
        (lambda d: d.update(part_size=3 * 1024 * 1024 * 1024), "part_size"),
        (lambda d: d["parts"][0].update(sha256="zz"), "sha256"),
    ],
)
def test_refuses_broken(mutate, expect):
    data = _good()
    mutate(data)
    with pytest.raises(ValueError, match=expect):
        manifest.validate(data)


def test_derive_parts_uniform():
    parts = manifest.derive_parts("x.bin", 5000, 2000)
    sizes = [p["size"] for p in parts]
    assert sizes == [2000, 2000, 1000]
    assert sum(sizes) == 5000
    assert [p["name"] for p in parts] == ["x.bin.part0", "x.bin.part1", "x.bin.part2"]


def test_derive_parts_single():
    parts = manifest.derive_parts("x.bin", 900, 2000)
    assert len(parts) == 1
    assert parts[0]["size"] == 900
