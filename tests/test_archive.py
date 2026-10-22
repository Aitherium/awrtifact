"""seal-archive / unseal-restore: the frozen-tier primitive.

What these tests pin (every one can fail):

- a synthetic 1 GiB file seals and restores byte-identical with a keypair made
  IN the test, while the most ciphertext ever on local disk stays <= 1.2x the
  file and Python allocations stay under one segment's order of magnitude;
- no plaintext byte (a random 64 KiB canary) reaches any uploaded asset;
- a public, missing, unreadable or deny-listed repo is refused with exit 2 and
  nothing is created or uploaded;
- a wrong private key, a tampered part, a missing part and an interrupted seal
  (no manifest) all fail closed: exit 1 and NOTHING at --out;
- key material never appears in argv, stdout or stderr.

GitHub is a fake (`FakeGh`): every gh function the module calls is replaced, so
no test can reach the network or a real repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tracemalloc
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("nacl")

from awrtifact import archive, cli, gh  # noqa: E402

MiB = 1024 * 1024


class FakeGh:
    """An in-memory GitHub: repos with a visibility, releases holding files."""

    def __init__(self, root: Path):
        self.root = root
        self.visibility: dict[str, object] = {}
        self.releases: dict[tuple[str, str], dict[str, Path]] = {}
        self.calls: list[tuple] = []
        self.secrets: dict[tuple[str, str], str] = {}
        self.on_upload = None
        self.on_download = None

    def repo_private(self, repo):
        self.calls.append(("repo_private", repo))
        vis = self.visibility.get(repo)
        if isinstance(vis, Exception):
            raise vis
        return vis

    def release_exists(self, repo, tag):
        return (repo, tag) in self.releases

    def create_release(self, repo, tag, title, notes):
        self.calls.append(("create_release", repo, tag))
        self.releases[(repo, tag)] = {}

    def release_assets(self, repo, tag):
        return set(self.releases.get((repo, tag), {}))

    def asset_sizes(self, repo, tag):
        return {n: p.stat().st_size for n, p in self.releases.get((repo, tag), {}).items()}

    def upload(self, repo, tag, path):
        src = Path(path)
        if self.on_upload:
            self.on_upload(src)
        dest_dir = self.root / repo.replace("/", "__") / tag
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copyfile(src, dest)
        self.releases[(repo, tag)][src.name] = dest
        self.calls.append(("upload", repo, tag, src.name))

    def download_asset(self, repo, tag, name, dest):
        assets = self.releases.get((repo, tag), {})
        if name not in assets:
            raise gh.GhError(f"gh release download {name}: no asset")
        shutil.copyfile(assets[name], dest)
        if self.on_download:
            self.on_download(Path(dest))
        return Path(dest).stat().st_size

    def secret_set(self, repo, name, value):
        self.secrets[(repo, name)] = value

    def uploads(self):
        return [c for c in self.calls if c[0] == "upload"]


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    fake = FakeGh(tmp_path / "github")
    for fn in ("repo_private", "release_exists", "create_release", "release_assets",
               "asset_sizes", "upload", "download_asset", "secret_set"):
        monkeypatch.setattr(gh, fn, getattr(fake, fn))
    fake.visibility["jason/vault"] = True
    return fake


@pytest.fixture
def keypair(tmp_path):
    pub, priv = archive.generate_keypair()
    key_file = tmp_path / "keys" / "archive.key"
    pub_file = tmp_path / "keys" / "archive.pub"
    key_file.parent.mkdir()
    key_file.write_text(priv + "\n", encoding="utf-8")
    pub_file.write_text(pub + "\n", encoding="utf-8")
    return {"pub": pub, "priv": priv, "key_file": key_file, "pub_file": pub_file}


def _write_random(path: Path, size: int, canary: bytes = b"") -> str:
    h = hashlib.sha256()
    with path.open("wb") as fh:
        if canary:
            fh.write(canary)
            h.update(canary)
        left = size - len(canary)
        while left:
            block = os.urandom(min(left, MiB))
            fh.write(block)
            h.update(block)
            left -= len(block)
    return h.hexdigest()


# -- round trips ---------------------------------------------------------------

def test_multipart_roundtrip_is_byte_identical_and_ships_no_plaintext(
        tmp_path, fake_gh, keypair, capsys):
    canary = os.urandom(64 * 1024)
    src = tmp_path / "Big Scene v2.fbx"
    digest = _write_random(src, 5 * MiB + 12345, canary)

    code = cli.main(["seal-archive", str(src), "--repo", "jason/vault", "--tag", "frozen-1",
                     "--recipient", str(keypair["pub_file"]), "--part-size", str(2 * MiB)])
    assert code == 0, capsys.readouterr().err
    assets = fake_gh.releases[("jason/vault", "frozen-1")]
    parts = sorted(n for n in assets if archive.PART_INFIX in n)
    assert len(parts) == 3
    assert "Big_Scene_v2.fbx.sealed.json" in assets
    # the manifest is uploaded LAST: a release without it is an interrupted seal
    assert fake_gh.uploads()[-1][3].endswith(archive.MANIFEST_SUFFIX)
    for path in assets.values():
        blob = path.read_bytes()
        assert canary[:4096] not in blob, f"plaintext reached {path.name}"
    head = assets["Big_Scene_v2.fbx.sealed.part0"].read_bytes()[:5]
    assert head == archive.MAGIC

    out = tmp_path / "restored"
    out.mkdir()
    code = cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "frozen-1",
                     "--out", str(out), "--key-file", str(keypair["key_file"])])
    assert code == 0, capsys.readouterr().err
    restored = out / "Big_Scene_v2.fbx"
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest
    assert not list(out.glob("*.partial"))
    text = capsys.readouterr()
    assert keypair["priv"] not in text.out + text.err


def test_empty_file_roundtrips(tmp_path, fake_gh, keypair):
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")
    archive.seal_archive(src, "jason/vault", "t-empty", keypair["pub"])
    res = archive.unseal_restore("jason/vault", "t-empty", tmp_path / "back.bin",
                                 keypair["priv"])
    assert res["bytes"] == 0 and (tmp_path / "back.bin").read_bytes() == b""


def test_one_gib_roundtrip_peak_disk_and_memory(tmp_path, fake_gh, keypair):
    """The critic #1 contract on a real-size object: 1 GiB, default part size."""
    size = 1024 * MiB
    src = tmp_path / "one-gib.bin"
    digest = _write_random(src, size)
    work = tmp_path / "work"
    peaks = {"seal": 0, "restore": 0}

    def du(root: Path) -> int:
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    fake_gh.on_upload = lambda _p: peaks.__setitem__("seal", max(peaks["seal"], du(work)))
    tracemalloc.start()
    try:
        sealed = archive.seal_archive(src, "jason/vault", "frozen-gib", keypair["pub"],
                                      work_dir=work)
        _, seal_mem = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(sealed["parts"]) == 1 and sealed["plain_sha256"] == digest
    assert peaks["seal"] <= 1.2 * size, f"seal peak extra disk {peaks['seal']} > 1.2x"
    assert sealed["peak_work_bytes"] <= 1.2 * size
    assert seal_mem < 32 * MiB, f"seal held {seal_mem} bytes in Python memory"
    assert not any(work.rglob("*")) or du(work) == 0

    out = tmp_path / "restored.bin"
    fake_gh.on_download = lambda _p: peaks.__setitem__("restore",
                                                       max(peaks["restore"], du(work)))
    tracemalloc.start()
    try:
        res = archive.unseal_restore("jason/vault", "frozen-gib", out, keypair["priv"],
                                     work_dir=work)
        _, restore_mem = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert res["status"] == "restored" and res["bytes"] == size
    with out.open("rb") as fh:
        h = hashlib.sha256()
        for block in iter(lambda: fh.read(8 * MiB), b""):
            h.update(block)
    assert h.hexdigest() == digest
    assert peaks["restore"] <= 1.2 * size
    assert restore_mem < 32 * MiB, f"restore held {restore_mem} bytes in Python memory"


# -- refusals --------------------------------------------------------------------

@pytest.mark.parametrize("visibility", [False, None, gh.GhError("cannot read")])
def test_public_missing_or_unreadable_repo_is_refused(tmp_path, fake_gh, keypair, capsys,
                                                      visibility):
    fake_gh.visibility["jason/open"] = visibility
    src = tmp_path / "x.bin"
    _write_random(src, 1000)
    code = cli.main(["seal-archive", str(src), "--repo", "jason/open", "--tag", "t",
                     "--recipient", keypair["pub"]])
    assert code == 2
    assert not [c for c in fake_gh.calls if c[0] in ("create_release", "upload")]
    assert capsys.readouterr().err.startswith("awrtifact: ")


def test_public_repo_message_names_public(tmp_path, fake_gh, keypair, capsys):
    fake_gh.visibility["jason/open"] = False
    src = tmp_path / "x.bin"
    _write_random(src, 10)
    assert cli.main(["seal-archive", str(src), "--repo", "jason/open", "--tag", "t",
                     "--recipient", keypair["pub"]]) == 2
    assert "PUBLIC" in capsys.readouterr().err


def test_deny_listed_serving_repo_is_refused_even_if_private(tmp_path, fake_gh, keypair):
    fake_gh.visibility["Aitherium/aitherkvcache"] = True
    src = tmp_path / "x.bin"
    _write_random(src, 10)
    assert cli.main(["seal-archive", str(src), "--repo", "Aitherium/aitherkvcache",
                     "--tag", "t", "--recipient", keypair["pub"]]) == 2
    assert not fake_gh.uploads()


def test_bad_recipient_is_refused_before_any_github_write(tmp_path, fake_gh):
    src = tmp_path / "x.bin"
    _write_random(src, 10)
    assert cli.main(["seal-archive", str(src), "--repo", "jason/vault", "--tag", "t",
                     "--recipient", "bm90LWEta2V5"]) == 2
    assert not fake_gh.calls


def test_existing_archive_is_not_overwritten(tmp_path, fake_gh, keypair):
    src = tmp_path / "x.bin"
    _write_random(src, 1000)
    archive.seal_archive(src, "jason/vault", "t", keypair["pub"])
    with pytest.raises(archive.ArchiveError, match="already holds"):
        archive.seal_archive(src, "jason/vault", "t", keypair["pub"])


def test_recipient_by_key_id(tmp_path, fake_gh, keypair):
    kid = archive.key_id(keypair["pub"])
    rdir = tmp_path / "recipients"
    rdir.mkdir()
    (rdir / f"{kid}.pub").write_text(keypair["pub"], encoding="utf-8")
    assert archive.load_recipient(kid, rdir) == (keypair["pub"], kid)
    (rdir / f"{kid}.pub").write_text(archive.generate_keypair()[0], encoding="utf-8")
    with pytest.raises(archive.RefusedError, match="not"):
        archive.load_recipient(kid, rdir)


# -- fail closed on restore --------------------------------------------------------

def _sealed(tmp_path, keypair, size=3 * MiB + 7, part=MiB + 3):
    src = tmp_path / "obj.bin"
    _write_random(src, size)
    archive.seal_archive(src, "jason/vault", "t", keypair["pub"], part_size=max(part, MiB))
    return src


def test_wrong_key_fails_closed(tmp_path, fake_gh, keypair, capsys, monkeypatch):
    _sealed(tmp_path, keypair)
    _, other = archive.generate_keypair()
    monkeypatch.setenv("SOMEONE_ELSES_KEY", other)
    out = tmp_path / "out.bin"
    code = cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "t",
                     "--out", str(out), "--key-env", "SOMEONE_ELSES_KEY"])
    assert code == 1
    err = capsys.readouterr().err
    assert "not the archive's recipient" in err and other not in err
    assert not out.exists() and not list(tmp_path.glob("out.bin*"))


def test_wrong_key_with_forged_key_id_still_fails_closed(tmp_path, fake_gh, keypair):
    """Even if the manifest's key id is rewritten to match, SealedBox refuses."""
    _sealed(tmp_path, keypair)
    other_pub, other = archive.generate_keypair()
    mpath = fake_gh.releases[("jason/vault", "t")]["obj.bin.sealed.json"]
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["wraps"]["pubkey"]["recipient_key_id"] = archive.key_id(other_pub)
    m["manifest_sha256"] = archive._manifest_digest(m)
    mpath.write_text(json.dumps(m), encoding="utf-8")
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="does not open"):
        archive.unseal_restore("jason/vault", "t", out, other)
    assert not out.exists()


def test_tampered_part_fails_closed(tmp_path, fake_gh, keypair):
    _sealed(tmp_path, keypair)
    part = fake_gh.releases[("jason/vault", "t")]["obj.bin.sealed.part1"]
    blob = bytearray(part.read_bytes())
    blob[100] ^= 0x01
    part.write_bytes(bytes(blob))
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="sha256 mismatch"):
        archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert not out.exists() and not (tmp_path / "out.bin.partial").exists()


def test_tampered_part_with_rewritten_hashes_fails_gcm(tmp_path, fake_gh, keypair):
    """An attacker who also fixes the manifest hashes still hits the GCM tag."""
    _sealed(tmp_path, keypair)
    rel = fake_gh.releases[("jason/vault", "t")]
    part = rel["obj.bin.sealed.part1"]
    blob = bytearray(part.read_bytes())
    blob[100] ^= 0x01
    part.write_bytes(bytes(blob))
    m = json.loads(rel["obj.bin.sealed.json"].read_text(encoding="utf-8"))
    m["parts"][1]["sha256"] = hashlib.sha256(bytes(blob)).hexdigest()
    m["manifest_sha256"] = archive._manifest_digest(m)
    rel["obj.bin.sealed.json"].write_text(json.dumps(m), encoding="utf-8")
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="failed authentication"):
        archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert not out.exists() and not (tmp_path / "out.bin.partial").exists()


def test_missing_part_and_interrupted_seal_fail_closed(tmp_path, fake_gh, keypair):
    _sealed(tmp_path, keypair)
    rel = fake_gh.releases[("jason/vault", "t")]
    del rel["obj.bin.sealed.part2"]
    with pytest.raises(gh.GhError):
        archive.unseal_restore("jason/vault", "t", tmp_path / "o.bin", keypair["priv"])
    assert not (tmp_path / "o.bin").exists()
    del rel["obj.bin.sealed.json"]
    with pytest.raises(archive.ArchiveError, match="interrupted seal"):
        archive.unseal_restore("jason/vault", "t", tmp_path / "o.bin", keypair["priv"])


def test_seal_failure_mid_upload_leaves_no_local_ciphertext(tmp_path, fake_gh, keypair):
    src = tmp_path / "obj.bin"
    _write_random(src, 3 * MiB)
    work = tmp_path / "work"
    calls = {"n": 0}

    def boom(_p):
        calls["n"] += 1
        if calls["n"] == 2:
            raise gh.GhError("upload failed")

    fake_gh.on_upload = boom
    with pytest.raises(gh.GhError):
        archive.seal_archive(src, "jason/vault", "t", keypair["pub"], part_size=MiB,
                             work_dir=work)
    assert not [p for p in work.rglob("*") if p.is_file()]
    assert "obj.bin.sealed.json" not in fake_gh.releases[("jason/vault", "t")]


def test_verify_only_proves_the_key_and_writes_nothing(tmp_path, fake_gh, keypair, capsys):
    _sealed(tmp_path, keypair)
    code = cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "t",
                     "--verify-only", "--key-file", str(keypair["key_file"]), "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "verified" and report["path"] == ""
    assert report["recipient_key_id"] == archive.key_id(keypair["pub"])


def test_no_key_is_a_usage_error(tmp_path, fake_gh, keypair, monkeypatch, capsys):
    _sealed(tmp_path, keypair)
    monkeypatch.delenv(archive.DEFAULT_KEY_ENV, raising=False)
    assert cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "t",
                     "--verify-only"]) == 2


def test_without_crypto_deps_exit_2_and_nothing_uploaded(tmp_path, fake_gh, keypair,
                                                         monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "nacl.public", None)
    src = tmp_path / "x.bin"
    _write_random(src, 10)
    assert cli.main(["seal-archive", str(src), "--repo", "jason/vault", "--tag", "t",
                     "--recipient", keypair["pub"]]) == 2
    assert "awrtifact[archive]" in capsys.readouterr().err
    assert not fake_gh.calls


# -- keygen + the holder's own GitHub secret ------------------------------------------

def test_keygen_writes_files_and_stores_the_secret_without_printing_it(
        tmp_path, fake_gh, capsys):
    priv_out = tmp_path / "me" / "archive.key"
    pub_out = tmp_path / "me" / "archive.pub"
    code = cli.main(["archive-keygen", "--private-out", str(priv_out), "--public-out",
                     str(pub_out), "--github-secret", "AITHER_ARCHIVE_KEY",
                     "--secret-repo", "jason/vault"])
    assert code == 0
    priv = archive.load_private_key(str(priv_out))
    pub, kid = archive.load_recipient(str(pub_out))
    assert archive.public_from_private(priv) == pub
    assert fake_gh.secrets[("jason/vault", "AITHER_ARCHIVE_KEY")] == priv
    text = capsys.readouterr()
    assert priv not in text.out + text.err and kid in text.out
    # never overwrites a key in place
    assert cli.main(["archive-keygen", "--private-out", str(priv_out), "--public-out",
                     str(pub_out)]) == 2


def test_keygen_refuses_a_public_secret_repo(tmp_path, fake_gh):
    fake_gh.visibility["jason/open"] = False
    assert cli.main(["archive-keygen", "--private-out", str(tmp_path / "k"),
                     "--public-out", str(tmp_path / "k.pub"), "--github-secret", "K",
                     "--secret-repo", "jason/open"]) == 2
    assert not (tmp_path / "k").exists() and not fake_gh.secrets


def test_secret_set_sends_the_value_on_stdin_never_argv(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0

    def fake_run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw.get("input")
        return Proc()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    gh.secret_set("jason/vault", "AITHER_ARCHIVE_KEY", "VALUE-NOT-IN-ARGV")
    assert "VALUE-NOT-IN-ARGV" not in " ".join(seen["argv"])
    assert seen["input"] == "VALUE-NOT-IN-ARGV"
