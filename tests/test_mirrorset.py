"""mirrorset.py — the set manifest's decisions: asset naming, chunking at the
cap, collision refusal, the missing-items plan, and lane packing."""

from __future__ import annotations

import pytest

from awrtifact import hf, mirrorset
from awrtifact.manifest import GITHUB_ASSET_CAP

REF = hf.HfRef("model", "org/name", "main", "")


def _files():
    return [
        hf.HfFile("config.json", 807, None, "a" * 40),
        hf.HfFile("onnx/model.onnx", 3000, "c" * 64, "b" * 40),
        hf.HfFile("big.safetensors", GITHUB_ASSET_CAP + 10, "d" * 64, "e" * 40),
        hf.HfFile("empty.txt", 0, None, "f" * 40),
    ]


def test_asset_name_flattens_and_sanitises():
    assert mirrorset.asset_name("onnx/model.onnx") == "onnx__model.onnx"
    assert mirrorset.asset_name("a b/c:d.txt") == "a_b__c_d.txt"
    assert mirrorset.asset_name(".gitattributes") == "_.gitattributes"  # GitHub renames dot-leading
    with pytest.raises(mirrorset.MirrorSetError):
        mirrorset.asset_name("/")


def test_build_chunks_over_cap_and_keeps_paths():
    data = mirrorset.build(_files(), ref=REF, commit="deadbeef", repo="o/r",
                           release="rel", part_size=1_000_000_000)
    by = {f["path"]: f for f in data["files"]}
    assert by["onnx/model.onnx"]["asset"] == "onnx__model.onnx"
    assert by["onnx/model.onnx"]["parts"] == []
    big = by["big.safetensors"]
    assert [p["name"] for p in big["parts"]] == [
        "big.safetensors.part0", "big.safetensors.part1", "big.safetensors.part2"]
    assert sum(p["size"] for p in big["parts"]) == big["size"]
    assert data["total"] == sum(f.size for f in _files())
    assert data["name"] == "org--name"
    assert mirrorset.manifest_asset(data) == "org--name.mirrorset.json"
    assert mirrorset.default_release("org/name", "deadbeefcafe") == "hf-org--name-deadbee"


def test_build_refuses_asset_collisions():
    files = [hf.HfFile("a/b.txt", 1, None, ""), hf.HfFile("a__b.txt", 1, None, "")]
    with pytest.raises(mirrorset.MirrorSetError, match="collision"):
        mirrorset.build(files, ref=REF, commit="c", repo="o/r", release="rel")


def test_validate_catches_tampering(tmp_path):
    data = mirrorset.build(_files(), ref=REF, commit="c", repo="o/r", release="rel",
                           part_size=1_000_000_000)
    path = mirrorset.write(data, tmp_path / "set.json")
    assert mirrorset.load(path)["name"] == "org--name"
    bad = dict(data)
    bad["total"] = data["total"] + 1
    with pytest.raises(mirrorset.MirrorSetError, match="total"):
        mirrorset.validate(bad)
    bad = mirrorset.load(path)
    big = next(f for f in bad["files"] if f["path"] == "big.safetensors")
    big["parts"][0]["name"] = "wrong.part0"
    with pytest.raises(mirrorset.MirrorSetError, match="misnamed"):
        mirrorset.validate(bad)
    bad = mirrorset.load(path)
    bad["files"][1]["sha256"] = "nothex"
    with pytest.raises(mirrorset.MirrorSetError, match="sha256"):
        mirrorset.validate(bad)


def test_plan_missing_uses_sizes_and_skips_empty_files():
    data = mirrorset.build(_files(), ref=REF, commit="c", repo="o/r", release="rel",
                           part_size=1_000_000_000)
    expected = mirrorset.expected_assets(data)
    assert "empty.txt" not in expected
    assert len(expected) == 2 + 3
    # nothing present: every non-empty asset is missing
    items = mirrorset.plan_missing(data, set())
    assert {i["asset"] for i in items} == set(expected)
    # a short part counts as missing; a correct one does not
    present = dict(expected)
    present["big.safetensors.part1"] -= 1
    items = mirrorset.plan_missing(data, present)
    assert [i["asset"] for i in items] == ["big.safetensors.part1"]
    assert items[0]["offset"] == 1_000_000_000 and items[0]["whole"] is False
    # a name-only listing (set) cannot judge size — present means present
    assert mirrorset.plan_missing(data, set(expected)) == []


def test_pack_lanes_balances_and_caps():
    items = [{"asset": f"a{i}", "size": s} for i, s in enumerate([10, 1, 1, 1, 9, 2])]
    lanes = mirrorset.pack_lanes(items, max_lanes=2)
    assert len(lanes) == 2
    loads = sorted(sum(i["size"] for i in lane) for lane in lanes)
    assert loads == [12, 12]
    assert mirrorset.pack_lanes([], 20) == []
    assert len(mirrorset.pack_lanes(items, 100)) == len(items)
    with pytest.raises(mirrorset.MirrorSetError):
        mirrorset.pack_lanes(items, 0)


# ----------------------------------------------------------------------------- dedupe
def _rev(release, onnx_sha="c" * 64):
    files = [hf.HfFile("config.json", 807, None, "a" * 40),
             hf.HfFile("onnx/model.onnx", 3000, onnx_sha, "b" * 40),
             hf.HfFile("big.safetensors", GITHUB_ASSET_CAP + 10, "d" * 64, "e" * 40)]
    return mirrorset.build(files, ref=REF, commit="c", repo="o/r", release=release,
                           part_size=1_000_000_000)


def test_link_previous_serves_unchanged_shards_from_the_earlier_release():
    old, new = _rev("hf-v1"), _rev("hf-v2", onnx_sha="9" * 64)
    r = mirrorset.link_previous(new, old)
    by = {f["path"]: f for f in new["files"]}
    assert r["linked"] == 1 and by["big.safetensors"]["from_release"] == "hf-v1"
    assert "from_release" not in by["onnx/model.onnx"]  # changed: uploaded again
    assert "from_release" not in by["config.json"]       # no sha256: never linked
    assert mirrorset.validate(new) is new
    planned = {i["asset"] for i in mirrorset.plan_missing(new, set())}
    assert not any(a.startswith("big.safetensors") for a in planned)
    assert "big.safetensors.part0" not in mirrorset.expected_assets(new)


def test_link_previous_flattens_a_chain_and_ignores_another_repo():
    v1, v2, v3 = _rev("hf-v1"), _rev("hf-v2"), _rev("hf-v3")
    mirrorset.link_previous(v2, v1)
    mirrorset.link_previous(v3, v2)
    assert {f["path"]: f.get("from_release") for f in v3["files"]}["big.safetensors"] == "hf-v1"
    other = _rev("hf-x")
    other["target"]["repo"] = "someone/else"
    assert mirrorset.link_previous(_rev("hf-v4"), other)["linked"] == 0


def test_validate_refuses_a_linked_file_without_sha256():
    data = _rev("hf-v2")
    cfg = next(f for f in data["files"] if f["path"] == "config.json")  # sha256 null
    cfg["from_release"] = "hf-v1"
    with pytest.raises(mirrorset.MirrorSetError):
        mirrorset.validate(data)
