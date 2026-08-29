"""upload's manifest-asset behaviour — the release becomes self-describing.

The manifest rides the release LAST (missing-only, size-checked), so a
re-run after a mid-flight failure just lands it and an already-correct
asset is never re-uploaded. This is the manifest-asset resolution: a release that
carries its manifest needs no spec side-channel to be read.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from awrtifact import gh, upload


def _proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], rc, stdout=out, stderr="")


class _FakeGh:
    """Deterministic gh: release assets in a dict, uploads recorded."""

    def __init__(self, existing_assets: dict[str, int] | None = None) -> None:
        self.assets = dict(existing_assets or {})
        self.uploads: list[str] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        rest = args
        if rest[:2] == ["release", "view"]:
            if "--jq" in rest:
                jq = rest[rest.index("--jq") + 1]
                if "size" in jq:
                    out = "".join(f"{n}\t{s}\n" for n, s in self.assets.items())
                    return _proc(0, out)
                return _proc(0, "".join(n + "\n" for n in self.assets))
            return _proc(0)
        if rest[:2] == ["release", "upload"]:
            # gh.upload passes ["release", "upload", release, path, ...]
            self.uploads.append(rest[3])
            return _proc(0)
        raise AssertionError(f"unexpected gh args: {rest}")


def _write(tmp: Path) -> dict:
    parts = [{"name": "m.gguf.part0", "size": 10}]
    manifest = {
        "name": "m.gguf", "total": 10, "part_size": 1900000000, "parts": parts,
    }
    (tmp / "m.gguf.part0").write_bytes(b"0123456789")
    (tmp / "manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture(autouse=True)
def _fake(monkeypatch: pytest.MonkeyPatch) -> _FakeGh:
    fake = _FakeGh()
    monkeypatch.setattr(gh, "_run", fake.run)
    return fake


def test_uploads_manifest_asset_when_missing(
    tmp_path: Path, _fake: _FakeGh
) -> None:
    manifest = _write(tmp_path)
    res = upload.upload_manifest(manifest, "you/store", "backup-1", tmp_path)
    assert res["manifest"] == "uploaded"
    assert any("m.gguf.manifest.json" in u for u in _fake.uploads)


def test_skips_present_manifest(tmp_path: Path) -> None:
    manifest = _write(tmp_path)
    size = (tmp_path / "manifest.json").stat().st_size
    fake = _FakeGh({"m.gguf.part0": 10, "m.gguf.manifest.json": size})
    import awrtifact.gh as gh_mod  # noqa: PLC0415
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(gh_mod, "_run", fake.run)
    try:
        res = upload.upload_manifest(manifest, "you/store", "backup-1", tmp_path)
    finally:
        mp.undo()
    assert res["manifest"] == "present"
    assert not any("manifest" in u for u in fake.uploads)


def test_resume_lands_only_the_manifest(tmp_path: Path) -> None:
    """Parts already up + manifest missing = the exact mid-flight re-run."""
    manifest = _write(tmp_path)
    size = (tmp_path / "manifest.json").stat().st_size
    fake = _FakeGh({"m.gguf.part0": 10})  # part present, manifest not
    import awrtifact.gh as gh_mod  # noqa: PLC0415
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(gh_mod, "_run", fake.run)
    try:
        res = upload.upload_manifest(manifest, "you/store", "backup-1", tmp_path)
    finally:
        mp.undo()
    assert res["manifest"] == "uploaded"
    assert "m.gguf.part0" not in [u.split("\\")[-1] for u in fake.uploads]
    assert any("m.gguf.manifest.json" in u for u in fake.uploads)
    assert size > 0  # sanity: the size check had something to compare
