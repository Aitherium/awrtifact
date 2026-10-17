"""companion-backup / companion-restore + the GobboNet-compatible envelope.

What is pinned:

- the Python envelope round-trips, and refuses a wrong passphrase or a
  tampered part;
- the Python envelope and the REAL browser mod (`gobbonet-backup.js` under
  node's WebCrypto, `tests/js/gobbonet_harness.mjs`) open each other's bytes,
  multi-part included, and the mod's `manifest_sha256` verifies here;
- restore merges append-only: local rows are never rewritten, backup rows are
  renumbered, an event_id conflict keeps both, a differing card is kept local
  with the backup copy beside it;
- a public repo (or one whose visibility cannot be read) is refused before any
  bytes leave; a missing repo is provisioned private;
- the passphrase never reaches a gh argv, stdout or stderr;
- no `cryptography` -> exit 2, nothing uploaded.

Network is never touched: `gh._run` is a deterministic in-memory fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from awrtifact import cli, companion, envelope, gh

PASS = "correct horse battery staple 7f3a"
HARNESS = Path(__file__).parent / "js" / "gobbonet_harness.mjs"
NODE = shutil.which("node")


def _proc(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], rc, stdout=out, stderr=err)


class FakeGhCli:
    """In-memory GitHub as the gh CLI sees it. Records every argv."""

    def __init__(self, visibility: str | None = "PRIVATE") -> None:
        self.visibility = visibility  # None = repo missing
        self.releases: dict[str, dict[str, bytes]] = {}
        self.calls: list[list[str]] = []
        self.provisioned = False

    def run(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["repo", "view"]:
            if self.visibility is None:
                return _proc(1, err="not found")
            if "visibility" in args:
                return _proc(0, self.visibility + "\n")
            return _proc(0, f"https://github.com/{args[2]}\n")
        if args[:2] == ["release", "view"]:
            tag = args[2]
            if tag not in self.releases:
                return _proc(1, err="release not found")
            if "--json" in args:
                jq = args[-1]
                assets = self.releases[tag]
                if "size" in jq:
                    return _proc(0, "".join(f"{n}\t{len(b)}\n" for n, b in assets.items()))
                return _proc(0, "".join(f"{n}\n" for n in assets))
            return _proc(0)
        if args[:2] == ["release", "create"]:
            self.releases[args[2]] = {}
            return _proc(0)
        if args[:2] == ["release", "upload"]:
            path = Path(args[3])
            self.releases[args[2]][path.name] = path.read_bytes()
            return _proc(0)
        if args[:2] == ["release", "list"]:
            return _proc(0, "".join(f"{t}\n" for t in reversed(list(self.releases))))
        if args[:2] == ["release", "download"]:
            tag, name, dest = args[2], args[args.index("-p") + 1], args[args.index("-O") + 1]
            if name not in self.releases.get(tag, {}):
                return _proc(1, err="no asset")
            Path(dest).write_bytes(self.releases[tag][name])
            return _proc(0)
        raise AssertionError(f"unexpected gh args: {args}")


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    fake = FakeGhCli()
    monkeypatch.setattr(gh, "_run", fake.run)

    def _provision(repo, public=False, pages=True, workflows=True):
        assert public is False
        fake.provisioned = True
        fake.visibility = "PRIVATE"
        return {"repo": repo}

    from awrtifact import provision

    monkeypatch.setattr(provision, "provision_repo", _provision)
    return fake


def _row(seq, text, event_id=None, ts=1000.0):
    return {"seq": seq, "ts": ts + seq, "role": "heard", "speaker": "owner", "text": text,
            "room": None, "event_id": event_id}


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def recall(tmp_path: Path) -> Path:
    root = tmp_path / "recall"
    pdir = root / "lyra"
    _write_jsonl(pdir / "utterances.jsonl",
                 [_row(1, "hello"), _row(2, "my cat is Miso", "ev-2"), _row(3, "good night")])
    _write_jsonl(pdir / "vectors.jsonl",
                 [{"seq": s, "dim": 2, "model": "m", "v": [1.0, 0.0]} for s in (1, 2, 3)])
    (pdir / "card_v2.json").write_text('{"spec": "chara_card_v2"}', encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------

def test_envelope_round_trip_multi_part(tmp_path: Path) -> None:
    src = tmp_path / "blob.bin"
    src.write_bytes(os.urandom(2500))
    man = envelope.encrypt_files([("blob.bin", src)], PASS, tmp_path / "enc", "t1",
                                 part_size=1000, iterations=1000)
    assert [p["name"] for p in man["files"][0]["parts"]] == [
        "blob.bin.part0", "blob.bin.part1", "blob.bin.part2"]
    assert (tmp_path / "enc" / "blob.bin.part0").read_bytes()[:1000] != src.read_bytes()[:1000]
    out = envelope.decrypt_files(man, PASS, tmp_path / "enc", tmp_path / "dec")
    assert out[0].read_bytes() == src.read_bytes()


def test_envelope_refuses_wrong_passphrase_and_tamper(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_bytes(b"memories")
    man = envelope.encrypt_files([("a.txt", src)], PASS, tmp_path / "enc", "t", iterations=1000)
    with pytest.raises(envelope.EnvelopeError, match="wrong passphrase") as exc:
        envelope.decrypt_files(man, "not the passphrase", tmp_path / "enc", tmp_path / "d")
    assert PASS not in str(exc.value)
    part = tmp_path / "enc" / "a.txt.part0"
    part.write_bytes(b"x" + part.read_bytes()[1:])
    with pytest.raises(envelope.EnvelopeError, match="failed sha256"):
        envelope.decrypt_files(man, PASS, tmp_path / "enc", tmp_path / "d")
    assert not (tmp_path / "d" / "a.txt").exists()


def test_envelope_refuses_short_passphrase(tmp_path: Path) -> None:
    src = tmp_path / "a"
    src.write_bytes(b"x")
    with pytest.raises(envelope.EnvelopeError, match="8\\+"):
        envelope.encrypt_files([("a", src)], "short", tmp_path / "e", "t")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_python_written_backup_opens_in_the_browser_mod(tmp_path: Path) -> None:
    src = tmp_path / "companion-lyra.tar.gz"
    src.write_bytes(os.urandom(2300))
    enc = tmp_path / "enc"
    envelope.encrypt_files([(src.name, src)], PASS, enc, "companion-lyra-x", part_size=1000)
    env = dict(os.environ, GOBBONET_HARNESS_PASSPHRASE=PASS)
    proc = subprocess.run([NODE, str(HARNESS), "restore", str(enc), "companion-lyra-x",
                           str(tmp_path / "out")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / src.name).read_bytes() == src.read_bytes()


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_browser_mod_backup_opens_in_python(tmp_path: Path) -> None:
    ind = tmp_path / "in"
    ind.mkdir()
    (ind / "notes.json").write_bytes(b'{"k": "\xc3\xa9"}' * 50)
    env = dict(os.environ, GOBBONET_HARNESS_PASSPHRASE=PASS)
    proc = subprocess.run([NODE, str(HARNESS), "backup", str(ind), str(tmp_path / "rel")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    tag = json.loads(proc.stdout.strip().splitlines()[-1])["tag"]
    man = json.loads((tmp_path / "rel" / f"{tag}.backup.json").read_text(encoding="utf-8"))
    # the mod's manifest_sha256 must verify under our JSON.stringify emulation
    assert man["manifest_sha256"] == envelope.manifest_digest(man)
    out = envelope.decrypt_files(man, PASS, tmp_path / "rel", tmp_path / "dec")
    assert out[0].read_bytes() == (ind / "notes.json").read_bytes()


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def test_merge_is_append_only_and_keeps_both_on_conflict(tmp_path: Path, recall: Path) -> None:
    bundle = tmp_path / "b.tar.gz"
    companion.build_bundle("lyra", recall, bundle, log=lambda m: None)
    members = companion.read_bundle(bundle, "lyra")

    # the machine was "wiped": a fresh local store with one new row at seq 1
    # and the same event as the backup but different text.
    fresh = tmp_path / "fresh"
    pdir = fresh / "lyra"
    _write_jsonl(pdir / "utterances.jsonl",
                 [_row(1, "new since wipe"), _row(2, "my cat is Mochi", "ev-2")])
    (pdir / "card_v2.json").write_text('{"spec": "edited locally"}', encoding="utf-8")
    before = (pdir / "utterances.jsonl").read_bytes()

    report = companion.merge_into(pdir, members, "companion-lyra-T")
    rows = _read_jsonl(pdir / "utterances.jsonl")
    assert (pdir / "utterances.jsonl").read_bytes().startswith(before)  # nothing rewritten
    assert [r["text"] for r in rows] == ["new since wipe", "my cat is Mochi", "hello",
                                         "my cat is Miso", "good night"]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert report["rows_added"] == 3 and report["conflicts_kept_both"] == 1
    vecs = _read_jsonl(pdir / "vectors.jsonl")
    assert sorted(v["seq"] for v in vecs) == [3, 4, 5]
    assert (pdir / "card_v2.json").read_text(encoding="utf-8") == '{"spec": "edited locally"}'
    assert (pdir / "card_v2.json.restored-companion-lyra-T").is_file()

    again = companion.merge_into(pdir, members, "companion-lyra-T")
    assert again["rows_added"] == 0 and again["rows_already_present"] == 3


def test_bundle_refuses_traversal(tmp_path: Path) -> None:
    import io
    import tarfile

    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("lyra/../../evil")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(companion.CompanionBackupError, match="refused"):
        companion.read_bundle(bad, "lyra")


def test_awm_without_export_is_skipped_with_a_note() -> None:
    data, note = companion.awm_export("aitherium:lyra")
    assert data is None and "skipped" in note


# ---------------------------------------------------------------------------
# backup / restore through the gh contract
# ---------------------------------------------------------------------------

def test_backup_then_restore_into_a_wiped_machine(tmp_path: Path, recall: Path,
                                                  fake_gh: FakeGhCli) -> None:
    r1 = companion.backup("lyra", "me/mem", PASS, recall_root=recall, now=1_700_000_000,
                          log=lambda m: None)
    assert r1["tag"] == "companion-lyra-20231114T221320Z"
    r2 = companion.backup("lyra", "me/mem", PASS, recall_root=recall, now=1_700_000_000,
                          log=lambda m: None)
    assert r2["tag"] == "companion-lyra-20231114T221320Z-2"  # history accumulates
    assets = fake_gh.releases[r1["tag"]]
    assert f"{r1['tag']}.backup.json" in assets
    assert all(b"good night" not in b for b in assets.values())  # ciphertext only

    wiped = tmp_path / "wiped"
    report = companion.restore("lyra", "me/mem", PASS, recall_root=wiped, log=lambda m: None)
    assert report["tag"] == r2["tag"]
    rows = _read_jsonl(wiped / "lyra" / "utterances.jsonl")
    assert [r["text"] for r in rows] == ["hello", "my cat is Miso", "good night"]
    assert (wiped / "lyra" / "card_v2.json").is_file()
    assert report["files"]["card_v2.json"] == "restored"


def test_missing_repo_is_provisioned_private(recall: Path, fake_gh: FakeGhCli) -> None:
    fake_gh.visibility = None
    companion.backup("lyra", "me/new", PASS, recall_root=recall, log=lambda m: None)
    assert fake_gh.provisioned


@pytest.mark.parametrize("vis", ["PUBLIC", "WEIRD"])
def test_public_or_unreadable_repo_is_refused(recall: Path, fake_gh: FakeGhCli, vis: str) -> None:
    fake_gh.visibility = vis
    with pytest.raises((companion.PublicRepoRefusedError, gh.GhError)):
        companion.backup("lyra", "me/pub", PASS, recall_root=recall, log=lambda m: None)
    assert not fake_gh.releases  # nothing created, nothing uploaded


def test_public_repo_allowed_only_on_opt_in(recall: Path, fake_gh: FakeGhCli) -> None:
    fake_gh.visibility = "PUBLIC"
    r = companion.backup("lyra", "me/pub", PASS, recall_root=recall, allow_public=True,
                         log=lambda m: None)
    assert r["tag"] in fake_gh.releases


def test_cli_passphrase_never_in_argv_or_output(tmp_path: Path, recall: Path,
                                                fake_gh: FakeGhCli, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MY_PASS", PASS)
    rc = cli.main(["companion-backup", "lyra", "--repo", "me/mem", "--passphrase-env", "MY_PASS",
                   "--recall-dir", str(recall)])
    assert rc == 0
    rc = cli.main(["companion-restore", "lyra", "--repo", "me/mem", "--passphrase-env", "MY_PASS",
                   "--recall-dir", str(tmp_path / "w")])
    assert rc == 0
    out = capsys.readouterr()
    assert PASS not in out.out and PASS not in out.err
    assert "rows_added" in out.out
    flat = " ".join(" ".join(c) for c in fake_gh.calls)
    assert PASS not in flat
    assert fake_gh.calls  # the fake really was the transport


def test_cli_has_no_passphrase_argument() -> None:
    with pytest.raises(SystemExit):
        cli.main(["companion-backup", "lyra", "--repo", "me/mem", "--passphrase", PASS])


def test_cli_without_cryptography_exits_2_and_uploads_nothing(recall: Path, fake_gh: FakeGhCli,
                                                              monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", None)
    monkeypatch.setenv("MY_PASS", PASS)
    rc = cli.main(["companion-backup", "lyra", "--repo", "me/mem", "--passphrase-env", "MY_PASS",
                   "--recall-dir", str(recall)])
    assert rc == 2
    assert "pip install cryptography" in capsys.readouterr().err
    assert not fake_gh.releases and not fake_gh.calls


def test_cli_without_passphrase_noninteractive_refuses(recall: Path, fake_gh: FakeGhCli,
                                                       monkeypatch) -> None:
    monkeypatch.delenv("UNSET_PASS", raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    rc = cli.main(["companion-backup", "lyra", "--repo", "me/mem",
                   "--passphrase-env", "UNSET_PASS", "--recall-dir", str(recall)])
    assert rc == 2 and not fake_gh.releases


def test_repo_private_reads_visibility(monkeypatch) -> None:
    fake = FakeGhCli("INTERNAL")
    monkeypatch.setattr(gh, "_run", fake.run)
    assert gh.repo_private("o/r") is True
    fake.visibility = "PUBLIC"
    assert gh.repo_private("o/r") is False
    fake.visibility = None
    assert gh.repo_private("o/r") is None


def test_passphrase_env_that_is_not_a_name_is_not_echoed(capsys) -> None:
    rc = cli.main(["companion-backup", "lyra", "--repo", "me/mem", "--passphrase-env", PASS])
    assert rc == 2
    err = capsys.readouterr().err
    assert PASS not in err and "must name an environment variable" in err
