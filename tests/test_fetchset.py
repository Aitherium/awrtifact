"""fetch-set — restores the ORIGINAL layout, stitches parts, verifies against
the origin digest, and never calls an unhashed file verified."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from awrtifact import fetch as fetch_mod
from awrtifact import fetchset, hf, mirrorset

REF = hf.HfRef("model", "org/name", "main", "")
BIG = bytes(range(256)) * 20  # 5120 bytes


def _set(part_size=2048, with_sha=True):
    files = [
        hf.HfFile("config.json", 7, None, ""),
        hf.HfFile("onnx/model.onnx", len(BIG),
                  hashlib.sha256(BIG).hexdigest() if with_sha else None, ""),
        hf.HfFile("empty.txt", 0, None, ""),
    ]
    return mirrorset.build(files, ref=REF, commit="c", repo="o/r", release="rel",
                           part_size=part_size)


def _store():
    parts = [BIG[i:i + 2048] for i in range(0, len(BIG), 2048)]
    return {"config.json": b"{\"a\":1}",
            **{f"onnx__model.onnx.part{i}": p for i, p in enumerate(parts)}}


def _downloader(store, corrupt: str | None = None):
    def dl(url, dest, expected, token=None):
        name = url.rsplit("/", 1)[1]
        data = store[name]
        if corrupt == name:
            data = b"!" + data[1:]
        Path(dest).write_bytes(data)
    return dl


def test_fetch_set_restores_layout_and_stitches(tmp_path):
    rep = fetchset.fetch_set(_set(), tmp_path, downloader=_downloader(_store()))
    assert rep["ok"] and rep["fetched"] == 3 and rep["failed"] == []
    assert (tmp_path / "onnx" / "model.onnx").read_bytes() == BIG
    assert (tmp_path / "config.json").read_bytes() == b"{\"a\":1}"
    assert (tmp_path / "empty.txt").read_bytes() == b""
    assert not list(tmp_path.glob("onnx/*.part*"))
    assert sorted(rep["unverified"]) == ["config.json", "empty.txt"]


def test_fetch_set_detects_corruption_against_origin_digest(tmp_path):
    rep = fetchset.fetch_set(_set(), tmp_path,
                             downloader=_downloader(_store(), corrupt="onnx__model.onnx.part1"))
    assert not rep["ok"]
    assert [p for p, _ in rep["failed"]] == ["onnx/model.onnx"]
    assert not (tmp_path / "onnx" / "model.onnx").exists()


def test_fetch_set_is_idempotent_on_verified_files(tmp_path):
    fetchset.fetch_set(_set(), tmp_path, downloader=_downloader(_store()))
    calls = []

    def dl(url, dest, expected, token=None):
        calls.append(url)
        _downloader(_store())(url, dest, expected)
    rep = fetchset.fetch_set(_set(), tmp_path, downloader=dl)
    assert rep["up_to_date"] == 1  # only the hashed file can be proven current
    assert all("model.onnx" not in c for c in calls)


def test_fetch_set_only_filters_and_refuses_escapes(tmp_path):
    rep = fetchset.fetch_set(_set(), tmp_path, only=["config.json"],
                             downloader=_downloader(_store()))
    assert rep["files"] == 1 and rep["fetched"] == 1
    data = _set()
    data["files"][0]["path"] = "../escape.json"
    rep = fetchset.fetch_set(data, tmp_path, downloader=_downloader(_store()))
    assert any("escapes" in why for _, why in rep["failed"])
    assert not (tmp_path.parent / "escape.json").exists()


def test_download_failure_is_reported_not_raised(tmp_path):
    def dl(url, dest, expected, token=None):
        raise fetch_mod.FetchError("boom")
    rep = fetchset.fetch_set(_set(), tmp_path, downloader=dl)
    assert not rep["ok"] and len(rep["failed"]) == 2  # the empty file needs no download


def test_release_asset_url_quotes():
    assert fetchset.release_asset_url("o/r", "v 1", "a b.txt") == \
        "https://github.com/o/r/releases/download/v%201/a%20b.txt"


def test_load_set_requires_exactly_one_set(monkeypatch):
    from awrtifact import gh
    monkeypatch.setattr(gh, "release_assets",
                        lambda repo, rel: {"a.mirrorset.json", "b.mirrorset.json"})
    with pytest.raises(mirrorset.MirrorSetError, match="2 mirror sets"):
        fetchset.load_set_from_release("o/r", "rel")


def test_private_repo_falls_back_to_gh(monkeypatch, tmp_path):
    from awrtifact import gh
    calls = []

    def once(url, dest, expected):
        raise fetch_mod.FetchError("HTTP 404 fetching x")

    def dl(repo, release, name, dest):
        calls.append((repo, release, name))
        Path(dest).write_bytes(b"12345")
        return 5
    monkeypatch.setattr(fetch_mod, "_download_once", once)
    monkeypatch.setattr(gh, "download_asset", dl)
    fetchset._download(fetchset.release_asset_url("o/r", "rel", "a b.bin"), tmp_path / "a", 5)
    assert calls == [("o/r", "rel", "a b.bin")]
    with pytest.raises(fetch_mod.FetchError, match="wanted 6"):
        fetchset._download(fetchset.release_asset_url("o/r", "rel", "a b.bin"), tmp_path / "b", 6)
