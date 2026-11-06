"""promote: private release -> public repo, only when sealed by a named key.

Promotion is a one-way door. The property these tests hold is that every
refusal happens BEFORE anything is written to the public repo: a refused
promotion leaves the destination exactly as it was.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from awrtifact import promote as pm

awseal_keys = pytest.importorskip("awseal.keys")
awseal_seal = pytest.importorskip("awseal.seal")

SRC, DST, REL = "me/artifacts-private", "me/artifacts", "report-v1"


class FakeGitHub:
    """Two repos, release assets on disk, and a ledger of every write."""

    def __init__(self, root: Path, *, src_private=True, dst_private=False):
        self.root = root
        self.visibility = {SRC: src_private, DST: dst_private}
        self.releases: dict[tuple[str, str], Path] = {}
        self.created: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str, str]] = []

    def add_release(self, repo: str, release: str, files: dict[str, bytes]) -> Path:
        path = self.root / repo.replace("/", "__") / release
        path.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (path / name).write_bytes(data)
        self.releases[(repo, release)] = path
        return path

    # -- the gh surface promote() uses ----------------------------------
    def repo_private(self, repo):
        return self.visibility.get(repo)

    def release_exists(self, repo, release):
        return (repo, release) in self.releases

    def release_assets(self, repo, release):
        path = self.releases.get((repo, release))
        return {p.name for p in path.iterdir()} if path else set()

    def download_asset(self, repo, release, name, dest):
        src = self.releases[(repo, release)] / name
        shutil.copyfile(src, dest)
        return src.stat().st_size

    def create_release(self, repo, release, title, notes):
        self.created.append((repo, release))
        self.releases[(repo, release)] = self.add_release(repo, release, {})

    def upload(self, repo, release, path):
        self.uploads.append((repo, release, Path(path).name))


@pytest.fixture
def key(tmp_path):
    return awseal_keys.generate(tmp_path / "signer.key")


@pytest.fixture
def pub(key):
    return awseal_keys.public_key_hex(path=key)


def _sealed_release(gh: FakeGitHub, tmp_path: Path, key, *, extra: dict | None = None):
    """A private release whose assets are sealed by ``key``."""
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    (stage / "report.html").write_bytes(b"<title>Report</title><p>hello</p>")
    (stage / "data.json").write_bytes(b'{"n": 1}')
    awseal_seal.write(awseal_seal.sign(stage, key_path=key, subject=REL), stage)
    files = {p.name: p.read_bytes() for p in stage.iterdir()}
    files.update(extra or {})
    return gh.add_release(SRC, REL, files)


def test_a_sealed_private_release_is_promoted_with_every_asset(tmp_path, key, pub):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key)
    plan = pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.created == [(DST, REL)]
    assert sorted(n for _, _, n in gh.uploads) == ["awseal.json", "data.json", "report.html"]
    assert plan.uploaded == plan.assets
    assert plan.to_dict()["url"].endswith(f"{DST}/releases/tag/{REL}")


def test_dry_run_checks_everything_and_writes_nothing(tmp_path, key, pub):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key)
    plan = pm.promote(SRC, DST, REL, expect_key=pub, dry_run=True, api=gh)
    assert plan.dry_run and not plan.uploaded
    assert gh.created == [] and gh.uploads == []


def test_an_unsealed_release_is_refused_before_any_write(tmp_path, pub):
    gh = FakeGitHub(tmp_path / "gh")
    gh.add_release(SRC, REL, {"report.html": b"<title>x</title>"})
    with pytest.raises(pm.PromoteError, match="awseal.json"):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.created == [] and gh.uploads == []


def test_a_tampered_asset_is_refused(tmp_path, key, pub):
    gh = FakeGitHub(tmp_path / "gh")
    path = _sealed_release(gh, tmp_path, key)
    (path / "report.html").write_bytes(b"<title>swapped after sealing</title>")
    with pytest.raises(pm.PromoteError, match="does not verify"):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.uploads == []


def test_an_extra_unsealed_asset_is_refused(tmp_path, key, pub):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key, extra={"slipped-in.exe": b"MZ"})
    with pytest.raises(pm.PromoteError, match="does not verify"):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.uploads == []


def test_a_valid_seal_by_a_strangers_key_is_refused(tmp_path, key):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key)
    stranger = awseal_keys.public_key_hex(path=awseal_keys.generate(tmp_path / "other.key"))
    with pytest.raises(pm.PromoteError):
        pm.promote(SRC, DST, REL, expect_key=stranger, api=gh)
    assert gh.uploads == []


def test_no_expect_key_is_refused(tmp_path, key):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key)
    with pytest.raises(pm.PromoteError, match="expect-key"):
        pm.promote(SRC, DST, REL, expect_key="", api=gh)


def test_an_existing_public_release_is_never_overwritten(tmp_path, key, pub):
    gh = FakeGitHub(tmp_path / "gh")
    _sealed_release(gh, tmp_path, key)
    gh.add_release(DST, REL, {"old.html": b"already public"})
    with pytest.raises(pm.PromoteError, match="never overwrites"):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.uploads == []


@pytest.mark.parametrize("src_private,dst_private,match", [
    (False, False, "is public"),        # source must be private
    (True, True, "is private"),         # destination must be public
    (None, False, "cannot read"),       # unreadable visibility fails closed
    (True, None, "cannot read"),
])
def test_visibility_is_checked_both_ways(tmp_path, key, pub, src_private, dst_private, match):
    gh = FakeGitHub(tmp_path / "gh", src_private=src_private, dst_private=dst_private)
    _sealed_release(gh, tmp_path, key)
    with pytest.raises(pm.PromoteError, match=match):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)
    assert gh.uploads == []


def test_a_missing_source_release_is_refused(tmp_path, pub):
    gh = FakeGitHub(tmp_path / "gh")
    with pytest.raises(pm.PromoteError, match="has no release"):
        pm.promote(SRC, DST, REL, expect_key=pub, api=gh)


def test_key_check_holds_even_if_awseals_ok_ignored_the_key(tmp_path, key, monkeypatch):
    """promote's own key_trusted check is a second lock, independent of awseal's ``ok``.

    awseal folds the key into ``ok`` today. If a future awseal reported ``ok``
    on content alone, a stranger's seal must still be refused here.
    """
    stage = tmp_path / "s"
    stage.mkdir()
    (stage / "a.txt").write_text("x", encoding="utf-8")
    awseal_seal.write(awseal_seal.sign(stage, key_path=key), stage)
    real_verify = awseal_seal.verify

    def lenient(root, *, expect_key=None):
        verdict = real_verify(root, expect_key=expect_key)
        return {**verdict, "ok": verdict["signature_ok"] and verdict["content_ok"]}

    monkeypatch.setattr(awseal_seal, "verify", lenient)
    stranger = awseal_keys.public_key_hex(path=awseal_keys.generate(tmp_path / "x.key"))
    with pytest.raises(pm.PromoteError, match="not made by the key you named"):
        pm.verify_release(stage, expect_key=stranger)
