"""mirror_hf_repo — both lanes end-to-end against a fake Hub and a fake gh.

The cloud lane must land the manifest BEFORE the dispatch (the workflow reads
it from the release), seed the workflow into a repo that lacks it, and skip
the dispatch when nothing is missing. The local lane must fill in sha256 for
non-LFS files, refuse a Hub-hash mismatch, and leave the release complete.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from awrtifact import gh, mirror, mirrorset

_FILES = {
    "config.json": b'{"a": 1}',
    "onnx/model.onnx": b"x" * 3000,
    "README.md": b"",
}


def _proc(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], rc, stdout=out, stderr=err)


class FakeHub:
    def __init__(self, lfs: set[str] = frozenset({"onnx/model.onnx"}),
                 wrong_hash_for: str | None = None):
        self.lfs = set(lfs)
        self.wrong = wrong_hash_for

    def fetch(self, url, token=None):
        if url.endswith("/api/models/org/name"):
            return {"sha": "deadbeefcafe", "private": False, "gated": False}, {}
        assert "/tree/deadbeefcafe" in url, url
        items = []
        for path, data in _FILES.items():
            it = {"type": "file", "oid": "0" * 40, "size": len(data), "path": path}
            if path in self.lfs:
                sha = hashlib.sha256(data).hexdigest()
                if self.wrong == path:
                    sha = "f" * 64
                it["lfs"] = {"oid": sha, "size": len(data)}
            items.append(it)
        return items, {}


class FakeGh:
    """A release store + workflow registry over the gh CLI arg shapes."""

    def __init__(self, workflow_present: bool = True):
        self.assets: dict[str, bytes] = {}
        self.release = False
        self.workflows = {"mirror-hf-set.yml"} if workflow_present else set()
        self.dispatches: list[list[str]] = []
        self.seeded: list[str] = []
        self.empty = False  # a just-created repo has no commit to cut a release on

    def run(self, args):
        a = list(args)
        if a[:2] == ["repo", "view"]:
            return _proc(0, "https://github.com/o/r")
        if a[:2] == ["release", "view"]:
            if not self.release:
                return _proc(1, "not found")
            if "--jq" in a and ".assets[].name" in a:
                return _proc(0, "\n".join(self.assets))
            if "--jq" in a:
                return _proc(0, "\n".join(f"{n}\t{len(b)}" for n, b in self.assets.items()))
            return _proc(0)
        if a[:2] == ["release", "create"]:
            if self.empty:
                return _proc(1, "", "HTTP 422: Validation Failed" + chr(10) + "Repository is empty.")
            self.release = True
            return _proc(0)
        if a[:2] == ["release", "upload"]:
            p = Path(a[3])
            self.assets[p.name] = p.read_bytes()
            return _proc(0)
        if a[:2] == ["release", "download"]:
            name = a[a.index("-p") + 1]
            dest = a[a.index("-O") + 1]
            if name not in self.assets:
                return _proc(1, "", "no asset")
            Path(dest).write_bytes(self.assets[name])
            return _proc(0)
        if a[:2] == ["workflow", "view"]:
            return _proc(0 if a[2] in self.workflows else 1)
        if a[:2] == ["workflow", "run"]:
            self.dispatches.append(a)
            return _proc(0)
        if a[:3] == ["api", "-X", "PUT"]:
            path = a[3].split("/contents/", 1)[1]
            self.seeded.append(path)
            self.empty = False
            if path.startswith(".github/workflows/"):
                self.workflows.add(path.rsplit("/", 1)[1])
            return _proc(0)
        raise AssertionError(f"unexpected gh {a}")


@pytest.fixture
def fake_gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh, "_run", fake.run)
    return fake


def _serve_hub_bytes(monkeypatch):
    def dl(url, dest, offset, size, token=None, attempts=3):
        path = url.split("/resolve/deadbeefcafe/", 1)[1]
        from urllib.parse import unquote
        data = _FILES[unquote(path)][offset:offset + size]
        Path(dest).write_bytes(data)
    monkeypatch.setattr(mirror, "_download_range", dl)


def test_plan_lane_touches_nothing(fake_gh):
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="plan", fetch=FakeHub().fetch)
    assert rep["status"] == "planned"
    assert rep["release"] == "hf-org--name-deadbee"
    assert rep["files"] == 3 and rep["items_total"] == 2  # empty file has no asset
    assert fake_gh.assets == {} and not fake_gh.release


def test_cloud_lane_lands_manifest_then_dispatches(fake_gh, tmp_path):
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="cloud",
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["status"] == "dispatched" and rep["lane"] == "cloud"
    assert "org--name.mirrorset.json" in fake_gh.assets
    manifest = json.loads(fake_gh.assets["org--name.mirrorset.json"])
    assert manifest["source"]["commit"] == "deadbeefcafe"
    assert len(fake_gh.dispatches) == 1
    d = fake_gh.dispatches[0]
    assert "manifest=org--name.mirrorset.json" in d and "release=hf-org--name-deadbee" in d
    assert rep["workflow"] == "present"


def test_auto_lane_seeds_missing_workflow(monkeypatch, tmp_path):
    fake = FakeGh(workflow_present=False)
    monkeypatch.setattr(gh, "_run", fake.run)
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="auto",
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["workflow"] == "created" and rep["lane"] == "cloud"
    assert ".github/workflows/mirror-hf-set.yml" in fake.seeded
    assert "mirror-hf-set.yml" in fake.workflows


def test_cloud_lane_refuses_when_seeding_is_off(monkeypatch, tmp_path):
    fake = FakeGh(workflow_present=False)
    monkeypatch.setattr(gh, "_run", fake.run)
    with pytest.raises(ValueError, match="seeding is off"):
        mirror.mirror_hf_repo("hf://org/name", "o/r", lane="cloud", seed_workflow=False,
                              fetch=FakeHub().fetch, work_dir=tmp_path)
    assert fake.dispatches == []


def test_cloud_lane_complete_release_does_not_dispatch(fake_gh, tmp_path, monkeypatch):
    _serve_hub_bytes(monkeypatch)
    mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local",
                          fetch=FakeHub().fetch, work_dir=tmp_path)
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="cloud",
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["status"] == "complete" and fake_gh.dispatches == []


def test_local_lane_uploads_and_fills_sha256(fake_gh, tmp_path, monkeypatch):
    _serve_hub_bytes(monkeypatch)
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local",
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["status"] == "verified" and rep["lane"] == "local"
    assert sorted(rep["uploaded"]) == ["config.json", "onnx__model.onnx"]
    assert fake_gh.assets["onnx__model.onnx"] == _FILES["onnx/model.onnx"]
    manifest = mirrorset.validate(json.loads(fake_gh.assets["org--name.mirrorset.json"]))
    by = {f["path"]: f for f in manifest["files"]}
    assert by["config.json"]["sha256"] == hashlib.sha256(_FILES["config.json"]).hexdigest()
    assert rep["sha_unknown"] == 1  # the empty file has no bytes to hash on this lane
    assert not (tmp_path / "config.json").exists(), "staging not cleaned"


def test_local_lane_refuses_hub_hash_mismatch(fake_gh, tmp_path, monkeypatch):
    _serve_hub_bytes(monkeypatch)
    with pytest.raises(ValueError, match="sha256"):
        mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local",
                              fetch=FakeHub(wrong_hash_for="onnx/model.onnx").fetch,
                              work_dir=tmp_path)
    assert "onnx__model.onnx" not in fake_gh.assets


def test_local_lane_chunks_over_part_size(fake_gh, tmp_path, monkeypatch):
    _serve_hub_bytes(monkeypatch)
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local", part_size=1024,
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["status"] == "verified"
    parts = sorted(n for n in fake_gh.assets if n.startswith("onnx__model.onnx.part"))
    assert parts == ["onnx__model.onnx.part0", "onnx__model.onnx.part1", "onnx__model.onnx.part2"]
    assert b"".join(fake_gh.assets[p] for p in parts) == _FILES["onnx/model.onnx"]


def test_missing_repo_is_refused_without_create(monkeypatch, tmp_path):
    fake = FakeGh()
    orig = fake.run

    def run(args):
        if list(args)[:2] == ["repo", "view"]:
            return _proc(1, "not found")
        return orig(args)
    monkeypatch.setattr(gh, "_run", run)
    with pytest.raises(ValueError, match="does not exist"):
        mirror.mirror_hf_repo("hf://org/name", "o/r", lane="cloud",
                              fetch=FakeHub().fetch, work_dir=tmp_path)


def test_seeded_workflows_are_real_package_assets():
    for wf in ("mirror-hf-set.yml", "mirror-hf-to-release.yml"):
        text = mirror.workflow_source(wf)
        assert "workflow_dispatch" in text
    assert "GITHUB_REPOSITORY" in mirror.workflow_source("mirror-hf-set.yml")
    assert "aitherkvcache" not in mirror.workflow_source("mirror-hf-set.yml").split("jobs:")[1], \
        "the set workflow must be repo-agnostic"
    with pytest.raises(ValueError):
        mirror.workflow_source("nope.yml")


def test_empty_repo_gets_an_initial_commit_before_the_release(fake_gh, tmp_path):
    fake_gh.empty = True
    rep = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="cloud",
                                fetch=FakeHub().fetch, work_dir=tmp_path)
    assert rep["status"] == "dispatched"
    assert "README.md" in fake_gh.seeded and fake_gh.release


def test_resume_keeps_hashes_from_the_previous_manifest(fake_gh, tmp_path, monkeypatch):
    _serve_hub_bytes(monkeypatch)
    first = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local",
                                  fetch=FakeHub().fetch, work_dir=tmp_path)
    assert first["sha_known"] == 2
    # simulate a lost asset: the resumed run must re-upload ONLY it and keep
    # every sha256 the first run computed (config.json is non-LFS)
    del fake_gh.assets["onnx__model.onnx"]
    second = mirror.mirror_hf_repo("hf://org/name", "o/r", lane="local",
                                   fetch=FakeHub().fetch, work_dir=tmp_path)
    assert second["uploaded"] == ["onnx__model.onnx"]
    assert second["sha_merged"] == 1 and second["sha_known"] == 2
    manifest = json.loads(fake_gh.assets["org--name.mirrorset.json"])
    by = {f["path"]: f for f in manifest["files"]}
    assert by["config.json"]["sha256"] == hashlib.sha256(_FILES["config.json"]).hexdigest()
