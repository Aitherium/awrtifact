"""awrtifact's Strata backend: parts in a pool, content addressed, verified both ways.

The target is an in-memory stand-in with the three calls the backend uses
(`upload_verified`, `stat_or_none`, `get`); `from_env` is checked against the
real `awstorage.StrataTarget` when awstorage is importable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from awrtifact import cli
from awrtifact import manifest as manifest_mod
from awrtifact import split as split_mod
from awrtifact.backends import strata as backend


class Pool:
    def __init__(self):
        self.blobs: dict = {}
        self.uploads: list = []
        self.down = False
        self.rot = None

    def upload_verified(self, rel, data, sha256, metadata=None):
        assert hashlib.sha256(data).hexdigest() == sha256
        self.blobs[rel] = bytes(data)
        self.uploads.append(rel)
        return {"path": rel, "bytes": len(data)}

    def stat_or_none(self, rel):
        if self.down:
            raise RuntimeError("pool is down")
        if rel not in self.blobs:
            return None
        d = self.blobs[rel]
        return {"size": len(d), "hash": hashlib.sha256(d).hexdigest()}

    def get(self, rel):
        if rel not in self.blobs:
            raise RuntimeError("404")
        return (b"X" + self.blobs[rel][1:]) if rel == self.rot else self.blobs[rel]


def _split(tmp: Path, data: bytes, name="model.bin", part=1000):
    src = tmp / "src" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    out = tmp / "parts"
    return split_mod.split_file(src, part, out), out


def test_parts_are_keyed_by_their_own_sha256_under_the_tenant(tmp_path):
    m, out = _split(tmp_path, os.urandom(2500))
    pool = Pool()
    rep = backend.StrataPartStore(pool, "tnt_a").upload(m, out)
    assert rep["failed"] == [] and rep["manifest"] == "uploaded"
    assert len(rep["uploaded"]) == 3
    for p in m["parts"]:
        assert f"__t__/tnt_a/awrtifact/parts/{p['sha256'][:2]}/{p['sha256']}" in pool.blobs
    assert "__t__/tnt_a/awrtifact/manifests/model.bin.manifest.json" in pool.blobs


def test_a_new_version_sends_only_the_parts_that_changed(tmp_path):
    v1 = os.urandom(3000)
    v2 = v1[:2000] + os.urandom(1000)          # last part changed
    pool = Pool()
    store = backend.StrataPartStore(pool, "tnt_a")
    m1, out1 = _split(tmp_path / "a", v1)
    store.upload(m1, out1)
    pool.uploads.clear()
    m2, out2 = _split(tmp_path / "b", v2)
    rep = store.upload(m2, out2)
    assert rep["uploaded"] == ["model.bin.part2"]
    assert rep["skipped_present"] == ["model.bin.part0", "model.bin.part1"]
    parts_sent = [u for u in pool.uploads if "/parts/" in u]
    assert len(parts_sent) == 1


@pytest.mark.parametrize("damage", ["truncated", "same_size_wrong_digest", "no_size"])
def test_a_damaged_part_in_the_pool_is_not_present_and_is_repaired(tmp_path, damage):
    """Review 2026-10-08: `_present` must check size and digest, not mere existence.

    Mutation: `_present` returns True for any non-None stat -> every arm red."""
    m, out = _split(tmp_path, os.urandom(3000))
    pool = Pool()
    store = backend.StrataPartStore(pool, "tnt_a")
    store.upload(m, out)
    victim = store.part_rel(m["parts"][1]["sha256"])
    good = pool.blobs[victim]
    if damage == "truncated":
        pool.blobs[victim] = good[:-1]
    elif damage == "same_size_wrong_digest":
        pool.blobs[victim] = bytes([good[0] ^ 1]) + good[1:]
    else:
        stat = pool.stat_or_none
        pool.stat_or_none = lambda rel: (
            {"hash": hashlib.sha256(pool.blobs[rel]).hexdigest()} if rel == victim
            else stat(rel))
    assert store.plan(m)["need_upload"] == [1]
    pool.uploads.clear()
    rep = store.upload(m, out)
    assert rep["uploaded"] == ["model.bin.part1"] and rep["failed"] == []
    assert pool.blobs[victim] == good


def test_fetch_round_trips_byte_for_byte(tmp_path):
    data = os.urandom(2500)
    m, out = _split(tmp_path, data)
    pool = Pool()
    store = backend.StrataPartStore(pool, "tnt_a")
    store.upload(m, out)
    got = store.fetch(store.load_manifest("model.bin"), tmp_path / "dest",
                      expected_sha256=hashlib.sha256(data).hexdigest())
    assert Path(got["path"]).read_bytes() == data
    assert got["status"] == "fetched"


def test_fetch_refuses_a_rotten_part_and_writes_nothing(tmp_path):
    m, out = _split(tmp_path, os.urandom(2500))
    pool = Pool()
    store = backend.StrataPartStore(pool, "tnt_a")
    store.upload(m, out)
    pool.rot = store.part_rel(m["parts"][1]["sha256"])
    dest = tmp_path / "dest"
    with pytest.raises(backend.StrataBackendError):
        store.fetch(m, dest)
    assert list(dest.iterdir()) == []


def test_fetch_refuses_a_manifest_that_is_not_the_pinned_digest(tmp_path):
    m, out = _split(tmp_path, os.urandom(1500))
    store = backend.StrataPartStore(Pool(), "tnt_a")
    store.upload(m, out)
    with pytest.raises(backend.StrataBackendError):
        store.fetch(m, tmp_path / "dest", expected_sha256="0" * 64)


def test_a_local_part_that_does_not_hash_is_not_sent(tmp_path):
    m, out = _split(tmp_path, os.urandom(2500))
    p1 = out / "model.bin.part1"
    p1.write_bytes(b"\0" * p1.stat().st_size)          # same size, other bytes
    pool = Pool()
    rep = backend.StrataPartStore(pool, "tnt_a").upload(m, out)
    assert any("part1" in f for f in rep["failed"])
    assert rep["manifest"] == "skipped"
    assert backend.StrataPartStore(pool, "tnt_a").part_rel(m["parts"][1]["sha256"]) \
        not in pool.blobs


def test_an_outage_raises_rather_than_reading_as_absent(tmp_path):
    m, out = _split(tmp_path, os.urandom(1500))
    pool = Pool()
    pool.down = True
    with pytest.raises(backend.StrataBackendError):
        backend.StrataPartStore(pool, "tnt_a").upload(m, out)
    assert pool.uploads == []


def test_oversize_parts_and_bad_tenants_are_refused_up_front(tmp_path):
    m, out = _split(tmp_path, os.urandom(2500))
    pool = Pool()
    with pytest.raises(backend.StrataBackendError):
        backend.StrataPartStore(pool, "tnt_a", max_part_bytes=999).upload(m, out)
    assert pool.uploads == []
    for bad in ("", "../x", "a/b", ".hidden"):
        with pytest.raises(backend.StrataBackendError):
            backend.StrataPartStore(pool, bad)


@pytest.mark.skipif(importlib.util.find_spec("awstorage") is None,
                    reason="awstorage not importable here")
def test_from_env_needs_a_credential_and_a_tenant(monkeypatch):
    with pytest.raises(backend.StrataBackendError):
        backend.from_env("tnt_a", env={})
    with pytest.raises(backend.StrataBackendError):
        backend.from_env(None, env={"AWSTORAGE_STRATA_BEARER": "tok"})
    store = backend.from_env(None, env={"AWSTORAGE_STRATA_BEARER": "tok",
                                        "AWSTORAGE_STRATA_TENANT": "tnt_a"})
    assert store.tenant == "tnt_a"
    assert "X-Internal-Key" not in store.target.auth_headers()


def test_cli_push_and_fetch_through_the_backend(tmp_path, monkeypatch, capsys):
    data = os.urandom(2500)
    m, out = _split(tmp_path, data)
    manifest_mod.write(m, out / "manifest.json")
    pool = Pool()
    monkeypatch.setattr(backend, "from_env",
                        lambda tenant, tier: backend.StrataPartStore(pool, tenant or "tnt_a"))
    assert cli.main(["strata-push", str(out / "manifest.json"), "--tenant", "tnt_a"]) == 0
    assert cli.main(["strata-push", str(out / "manifest.json"), "--tenant", "tnt_a"]) == 0
    assert "0 part(s); 3 already in the pool" in capsys.readouterr().out.replace("pooled ", "")
    assert cli.main(["strata-fetch", "model.bin", "--tenant", "tnt_a",
                     "--out", str(tmp_path / "dest")]) == 0
    assert (tmp_path / "dest" / "model.bin").read_bytes() == data
