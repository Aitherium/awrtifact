"""seal-archive / unseal-restore: the frozen-tier primitive.

What these tests pin (every one can fail):

- a synthetic 1 GiB file seals and restores byte-identical with a keypair made
  IN the test, while the most ciphertext ever on local disk stays <= 1.2x the
  file and Python allocations stay under one segment's order of magnitude;
- no plaintext byte (a random 64 KiB canary), no file name and no plaintext
  sha256 reaches any uploaded asset;
- a public, internal, missing, unreadable or deny-listed repo is refused with
  exit 2 and nothing is created or uploaded;
- a wrong private key, a tampered part, a missing part and an interrupted seal
  (no manifest) all fail closed: exit 1 and NOTHING at --out;
- an archive forged with a traversal or absolute name writes nothing outside
  --out; an archive copied to another tag, or one holding other bytes than
  --expect-sha256, is refused (the eviction canary cannot be fooled);
- key material never appears in argv, stdout, stderr, or a child's env; the
  generated private key is passphrase-protected and owner-only, and only that
  protected form may go to an Actions secret in a repo the holder controls.

GitHub is a fake (`FakeGh`): every gh function the module calls is replaced, so
no test can reach the network or a real repo.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tracemalloc
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("nacl")

from awrtifact import archive, cli, gh, ghapi  # noqa: E402

MiB = 1024 * 1024
PASS = "correct horse battery staple"


class FakeGh:
    """An in-memory GitHub: repos with a visibility, releases holding files."""

    def __init__(self, root: Path):
        self.root = root
        self.visibility: dict[str, object] = {}
        self.releases: dict[tuple[str, str], dict[str, Path]] = {}
        self.calls: list[tuple] = []
        self.secrets: dict[tuple[str, str], str] = {}
        self.login = "jason"
        self.orgs: dict[str, str] = {}
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

    def auth_login(self):
        return self.login

    def org_role(self, org):
        return self.orgs.get(org)

    def uploads(self):
        return [c for c in self.calls if c[0] == "upload"]


@pytest.fixture(autouse=True)
def _fast_scrypt(monkeypatch):
    monkeypatch.setattr(archive, "SCRYPT_N", 2 ** 14)
    monkeypatch.delenv(archive.DEFAULT_KEY_ENV, raising=False)
    monkeypatch.delenv(archive.DEFAULT_PASSPHRASE_ENV, raising=False)


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    fake = FakeGh(tmp_path / "github")
    for fn in ("repo_private", "release_exists", "create_release", "release_assets",
               "asset_sizes", "upload", "download_asset", "secret_set", "auth_login",
               "org_role"):
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


def _manifest(fake, repo="jason/vault", tag="t"):
    rel = fake.releases[(repo, tag)]
    (mname,) = [n for n in rel if n.endswith(archive.MANIFEST_SUFFIX)]
    return rel[mname], json.loads(rel[mname].read_text(encoding="utf-8"))


def _rewrite_meta(fake, priv, change, repo="jason/vault", tag="t"):
    """What anyone holding the PUBLIC key could do to their own archive -- or the
    key holder to any: re-encrypt the sealed metadata with edited fields."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from nacl.public import PrivateKey, SealedBox
    mpath, m = _manifest(fake, repo, tag)
    sk = PrivateKey(base64.b64decode(priv))
    dek = SealedBox(sk).decrypt(base64.b64decode(m["wraps"]["pubkey"]["blob"]))
    key = AESGCM(archive._meta_key(dek))
    aad = archive.META_AAD_PREFIX + m["archive_id"].encode("ascii")
    meta = json.loads(key.decrypt(base64.b64decode(m["meta"]["nonce"]),
                                  base64.b64decode(m["meta"]["blob"]), aad))
    change(meta)
    nonce = os.urandom(12)
    m["meta"]["nonce"] = base64.b64encode(nonce).decode("ascii")
    m["meta"]["blob"] = base64.b64encode(
        key.encrypt(nonce, json.dumps(meta).encode("utf-8"), aad)).decode("ascii")
    m["manifest_sha256"] = archive._manifest_digest(m)
    mpath.write_text(json.dumps(m), encoding="utf-8")


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
    # the manifest is uploaded LAST: a release without it is an interrupted seal
    assert fake_gh.uploads()[-1][3].endswith(archive.MANIFEST_SUFFIX)
    for path in assets.values():
        blob = path.read_bytes()
        assert canary[:4096] not in blob, f"plaintext reached {path.name}"
        # metadata stays sealed: no file name, no plaintext digest in the clear
        assert b"Big_Scene" not in blob and b"Big Scene" not in blob
        assert digest.encode() not in blob
        assert "Big" not in path.name
    head = assets[parts[0]].read_bytes()[:5]
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
                                     work_dir=work, archive_id=sealed["archive_id"],
                                     expect_plain_sha256=digest)
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


def test_two_archives_in_one_tag_need_a_selector(tmp_path, fake_gh, keypair):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    da, db = _write_random(a, 1000), _write_random(b, 2000)
    sa = archive.seal_archive(a, "jason/vault", "t", keypair["pub"])
    sb = archive.seal_archive(b, "jason/vault", "t", keypair["pub"])
    assert sa["archive_id"] != sb["archive_id"]
    with pytest.raises(archive.RefusedError, match="several archives"):
        archive.unseal_restore("jason/vault", "t", None, keypair["priv"], verify_only=True)
    assert archive.unseal_restore("jason/vault", "t", None, keypair["priv"], name="b.bin",
                                  verify_only=True)["plain_sha256"] == db
    assert archive.unseal_restore("jason/vault", "t", None, keypair["priv"],
                                  archive_id=sa["archive_id"],
                                  verify_only=True)["plain_sha256"] == da
    assert archive.unseal_restore("jason/vault", "t", None, keypair["priv"],
                                  expect_plain_sha256=da,
                                  verify_only=True)["archive_id"] == sa["archive_id"]


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

def _sealed(tmp_path, keypair, size=3 * MiB + 7, part=MiB + 3, tag="t", name="obj.bin"):
    src = tmp_path / name
    digest = _write_random(src, size)
    res = archive.seal_archive(src, "jason/vault", tag, keypair["pub"],
                               part_size=max(part, MiB))
    return src, res, digest


def _part(fake, res, i, tag="t"):
    return fake.releases[("jason/vault", tag)][f"{res['archive_id']}{archive.PART_INFIX}{i}"]


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
    mpath, m = _manifest(fake_gh)
    m["wraps"]["pubkey"]["recipient_key_id"] = archive.key_id(other_pub)
    m["manifest_sha256"] = archive._manifest_digest(m)
    mpath.write_text(json.dumps(m), encoding="utf-8")
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="does not open"):
        archive.unseal_restore("jason/vault", "t", out, other)
    assert not out.exists()


def test_tampered_part_fails_closed(tmp_path, fake_gh, keypair):
    _, res, _ = _sealed(tmp_path, keypair)
    part = _part(fake_gh, res, 1)
    blob = bytearray(part.read_bytes())
    blob[100] ^= 0x01
    part.write_bytes(bytes(blob))
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="sha256 mismatch"):
        archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert not out.exists() and not (tmp_path / "out.bin.partial").exists()


def test_tampered_part_with_rewritten_hashes_fails_gcm(tmp_path, fake_gh, keypair):
    """An attacker who also fixes the manifest hashes still hits the GCM tag."""
    _, res, _ = _sealed(tmp_path, keypair)
    part = _part(fake_gh, res, 1)
    blob = bytearray(part.read_bytes())
    blob[100] ^= 0x01
    part.write_bytes(bytes(blob))
    mpath, m = _manifest(fake_gh)
    m["parts"][1]["sha256"] = hashlib.sha256(bytes(blob)).hexdigest()
    m["manifest_sha256"] = archive._manifest_digest(m)
    mpath.write_text(json.dumps(m), encoding="utf-8")
    out = tmp_path / "out.bin"
    with pytest.raises(archive.ArchiveError, match="failed authentication"):
        archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert not out.exists() and not (tmp_path / "out.bin.partial").exists()


def test_missing_part_and_interrupted_seal_fail_closed(tmp_path, fake_gh, keypair):
    _, res, _ = _sealed(tmp_path, keypair)
    rel = fake_gh.releases[("jason/vault", "t")]
    del rel[f"{res['archive_id']}{archive.PART_INFIX}2"]
    with pytest.raises(gh.GhError):
        archive.unseal_restore("jason/vault", "t", tmp_path / "o.bin", keypair["priv"])
    assert not (tmp_path / "o.bin").exists()
    del rel[res["manifest_asset"]]
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
    assert not [n for n in fake_gh.releases[("jason/vault", "t")]
                if n.endswith(archive.MANIFEST_SUFFIX)]


def test_verify_only_proves_the_key_and_writes_nothing(tmp_path, fake_gh, keypair, capsys):
    _, res, digest = _sealed(tmp_path, keypair)
    code = cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "t",
                     "--verify-only", "--key-file", str(keypair["key_file"]),
                     "--archive-id", res["archive_id"], "--expect-sha256", digest, "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "verified" and report["path"] == ""
    assert report["recipient_key_id"] == archive.key_id(keypair["pub"])


def test_no_key_is_a_usage_error(tmp_path, fake_gh, keypair, monkeypatch, capsys):
    _sealed(tmp_path, keypair)
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


# -- forged / moved archives (review findings 1 and 4) -------------------------------

@pytest.mark.parametrize("evil", ["../../evil.cmd", "C:/Windows/evil.cmd", "/etc/evil",
                                  "..", "sub/dir.bin"])
def test_forged_unsafe_name_writes_nothing_outside_out(tmp_path, fake_gh, keypair, evil):
    _sealed(tmp_path, keypair, size=4096)
    _rewrite_meta(fake_gh, keypair["priv"], lambda meta: meta.__setitem__("name", evil))
    out = tmp_path / "restore"
    out.mkdir()
    before = {p for p in tmp_path.rglob("*")}
    with pytest.raises(archive.ArchiveError, match="unsafe file name"):
        archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert {p for p in tmp_path.rglob("*")} - before == set()


def test_archive_copied_to_another_tag_is_refused(tmp_path, fake_gh, keypair):
    """Seal A to t1 and B to t2, then replace t2's assets with t1's: the canary
    for B must NOT pass (B's ciphertext is gone)."""
    _, ra, _ = _sealed(tmp_path, keypair, size=5000, tag="t1", name="a.bin")
    _, rb, db = _sealed(tmp_path, keypair, size=6000, tag="t2", name="b.bin")
    fake_gh.releases[("jason/vault", "t2")] = dict(fake_gh.releases[("jason/vault", "t1")])
    with pytest.raises(archive.ArchiveError, match="copied or replayed"):
        archive.unseal_restore("jason/vault", "t2", None, keypair["priv"], verify_only=True)
    with pytest.raises(archive.ArchiveError, match="no archive"):
        archive.unseal_restore("jason/vault", "t2", None, keypair["priv"],
                               archive_id=rb["archive_id"], verify_only=True)


def test_forged_archive_with_other_bytes_fails_the_expected_sha(tmp_path, fake_gh, keypair):
    """Anyone with the PUBLIC key can seal a whole new archive bound to this
    repo/tag; --expect-sha256 is what the eviction guard relies on."""
    _, real, digest = _sealed(tmp_path, keypair, size=5000)
    rel = fake_gh.releases[("jason/vault", "t")]
    for n in list(rel):
        del rel[n]
    fake_src = tmp_path / "obj.bin"
    _write_random(fake_src, 5000)
    archive.seal_archive(fake_src, "jason/vault", "t", keypair["pub"])
    with pytest.raises(archive.ArchiveError, match="different file|match"):
        archive.unseal_restore("jason/vault", "t", None, keypair["priv"], verify_only=True,
                               expect_plain_sha256=digest)
    assert cli.main(["unseal-restore", "--repo", "jason/vault", "--tag", "t",
                     "--verify-only", "--key-file", str(keypair["key_file"]),
                     "--expect-sha256", digest]) == 1


def test_sealed_meta_bound_to_repo_and_tag_cannot_be_edited_without_the_dek(
        tmp_path, fake_gh, keypair):
    _sealed(tmp_path, keypair, size=4096)
    mpath, m = _manifest(fake_gh)
    blob = bytearray(base64.b64decode(m["meta"]["blob"]))
    blob[0] ^= 1
    m["meta"]["blob"] = base64.b64encode(bytes(blob)).decode("ascii")
    m["manifest_sha256"] = archive._manifest_digest(m)
    mpath.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(archive.ArchiveError, match="metadata failed authentication"):
        archive.unseal_restore("jason/vault", "t", None, keypair["priv"], verify_only=True)


def test_restore_partial_is_owner_only_and_a_stale_one_is_replaced(
        tmp_path, fake_gh, keypair, monkeypatch):
    _sealed(tmp_path, keypair, size=4096)
    out = tmp_path / "out.bin"
    stale = tmp_path / "out.bin.partial"
    stale.write_bytes(b"left over by a crashed run")
    seen = {}
    real_fdopen = os.fdopen

    def spy(fd, mode="r", *a, **kw):
        if "w" in mode and "b" in mode:
            seen["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
        return real_fdopen(fd, mode, *a, **kw)

    monkeypatch.setattr(archive.os, "fdopen", spy)
    archive.unseal_restore("jason/vault", "t", out, keypair["priv"])
    assert out.stat().st_size == 4096 and not stale.exists()
    if os.name != "nt":
        assert seen["mode"] == 0o600


# -- private key handling (review findings 2, 3 and 10) -------------------------------

def test_env_key_is_removed_and_never_reaches_a_gh_child(tmp_path, fake_gh, keypair,
                                                         monkeypatch):
    monkeypatch.setenv(archive.DEFAULT_KEY_ENV, keypair["priv"])
    assert archive.load_private_key("") == keypair["priv"]
    assert archive.DEFAULT_KEY_ENV not in os.environ
    monkeypatch.setenv(archive.DEFAULT_KEY_ENV, keypair["priv"])  # set again: gh strips it
    seen = {}

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return Proc()

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    gh._run(["release", "view", "t", "--repo", "jason/vault"])
    assert seen["env"] is not None
    assert keypair["priv"] not in json.dumps(seen["env"]) + " ".join(seen["argv"])
    gh.secret_set("jason/vault", "X", "VALUE")
    assert keypair["priv"] not in json.dumps(seen["env"])


def test_keygen_protects_the_key_and_stores_only_the_protected_form(
        tmp_path, fake_gh, capsys):
    priv_out = tmp_path / "me" / "archive.key"
    pub_out = tmp_path / "me" / "archive.pub"
    pfile = tmp_path / "pass.txt"
    pfile.write_text(PASS + "\n", encoding="utf-8")
    code = cli.main(["archive-keygen", "--private-out", str(priv_out), "--public-out",
                     str(pub_out), "--passphrase-file", str(pfile),
                     "--github-secret", "AITHER_ARCHIVE_KEY", "--secret-repo", "jason/vault"])
    assert code == 0, capsys.readouterr().err
    line = archive._first_key_line(priv_out.read_text(encoding="utf-8"))
    assert archive.is_encrypted_key(line)
    priv = archive.load_private_key(str(priv_out), passphrase_file=str(pfile))
    pub, kid = archive.load_recipient(str(pub_out))
    assert archive.public_from_private(priv) == pub
    assert priv not in priv_out.read_text(encoding="utf-8")
    stored = fake_gh.secrets[("jason/vault", "AITHER_ARCHIVE_KEY")]
    assert archive.is_encrypted_key(stored) and priv not in stored
    text = capsys.readouterr()
    assert priv not in text.out + text.err and kid in text.out
    if os.name != "nt":
        assert stat.S_IMODE(priv_out.stat().st_mode) == 0o600
    bad = tmp_path / "bad.txt"
    bad.write_text("not the passphrase at all\n", encoding="utf-8")
    with pytest.raises(archive.RefusedError, match="wrong passphrase"):
        archive.load_private_key(str(priv_out), passphrase_file=str(bad))
    # never overwrites a key in place
    assert cli.main(["archive-keygen", "--private-out", str(priv_out), "--public-out",
                     str(pub_out), "--passphrase-file", str(pfile)]) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_forced_keygen_does_not_keep_a_wider_mode(tmp_path, fake_gh):
    priv_out = tmp_path / "k"
    priv_out.write_text("old", encoding="utf-8")
    os.chmod(priv_out, 0o644)
    archive.keygen(priv_out, tmp_path / "k.pub", passphrase=PASS, force=True)
    assert stat.S_IMODE(priv_out.stat().st_mode) == 0o600


@pytest.mark.parametrize("repo,login,orgs,ok", [
    ("Aitherium/backup-user-jason", "jason", {"Aitherium": "admin"}, False),
    ("someone/repo", "jason", {}, False),
    ("garginc/aither-lockbox", "jason", {"garginc": "member"}, False),
    ("garginc/aither-lockbox", "jason", {"garginc": "admin"}, True),
    ("Jason/lockbox", "jason", {}, True),
])
def test_secret_repo_must_be_controlled_by_the_key_holder(tmp_path, fake_gh, repo, login,
                                                          orgs, ok):
    fake_gh.visibility[repo] = True
    fake_gh.login, fake_gh.orgs = login, orgs
    run = lambda: archive.keygen(tmp_path / "k", tmp_path / "k.pub", passphrase=PASS,  # noqa: E731
                                 github_secret="AITHER_ARCHIVE_KEY", secret_repo=repo)
    if ok:
        assert run()["github_secret"] == f"{repo}:AITHER_ARCHIVE_KEY"
    else:
        with pytest.raises(archive.RefusedError):
            run()
        assert not fake_gh.secrets and not (tmp_path / "k").exists()


def test_raw_key_never_goes_to_a_secret(tmp_path, fake_gh):
    assert cli.main(["archive-keygen", "--private-out", str(tmp_path / "k"),
                     "--public-out", str(tmp_path / "k.pub"), "--no-passphrase",
                     "--github-secret", "K", "--secret-repo", "jason/vault"]) == 2
    assert not fake_gh.secrets and not (tmp_path / "k").exists()


def test_keygen_refuses_a_public_secret_repo(tmp_path, fake_gh):
    fake_gh.visibility["jason/open"] = False
    pfile = tmp_path / "pass.txt"
    pfile.write_text(PASS, encoding="utf-8")
    assert cli.main(["archive-keygen", "--private-out", str(tmp_path / "k"),
                     "--public-out", str(tmp_path / "k.pub"), "--passphrase-file", str(pfile),
                     "--github-secret", "K", "--secret-repo", "jason/open"]) == 2
    assert not (tmp_path / "k").exists() and not fake_gh.secrets


def test_short_passphrase_is_refused(tmp_path, fake_gh):
    with pytest.raises(archive.RefusedError, match="at least"):
        archive.keygen(tmp_path / "k", tmp_path / "k.pub", passphrase="short")


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


# -- visibility guard (review finding 8) ---------------------------------------------

@pytest.mark.parametrize("vis,expected", [("PRIVATE", True), ("INTERNAL", False),
                                          ("PUBLIC", False)])
def test_cli_visibility_internal_is_not_private(monkeypatch, vis, expected):
    class Proc:
        returncode = 0
        stdout = vis + "\n"
        stderr = ""

    monkeypatch.setattr(gh, "_run", lambda args: Proc())
    assert gh.repo_private("o/r") is expected


@pytest.mark.parametrize("data,expected", [
    ({"visibility": "internal", "private": True}, False),
    ({"visibility": "private", "private": True}, True),
    ({"private": True}, True),
    ({"private": False}, False),
    ({}, "error"),
])
def test_api_visibility_fails_closed(monkeypatch, data, expected):
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "api")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setattr(ghapi, "_get", lambda url: dict(data, html_url="h"))
    if expected == "error":
        with pytest.raises(gh.GhError):
            gh.repo_private("o/r")
    else:
        assert gh.repo_private("o/r") is expected


# -- API download: the token never follows the redirect (review finding 11) -------------

def test_api_download_streams_and_drops_auth_on_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok-should-stay-on-github")
    payload = os.urandom(3 * MiB + 5)
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, req, timeout=None):
            seen.append((req.full_url, dict(req.header_items())))
            if "api.github.com" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found",
                    {"Location": "https://objects.githubusercontent.com/x?sig=1"}, None)
            return Resp(payload)

    monkeypatch.setattr(ghapi.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(ghapi, "_assets", lambda repo, rel: [{"id": 9, "name": "p"}])
    dest = tmp_path / "p"
    assert ghapi.download_asset("o/r", "rel", "p", str(dest)) == len(payload)
    assert dest.read_bytes() == payload
    assert "Authorization" in seen[0][1]
    storage = [h for u, h in seen if "objects.githubusercontent.com" in u]
    assert storage and not any(k.lower() == "authorization" for k in storage[0])
