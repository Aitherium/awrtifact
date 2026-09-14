"""Sealing — the manifest is signed after it is final, a tampered manifest
fails verification, a foreign key is refused with --expect-key, and asking
to seal without awseal is a hard error (never a silently unsigned mirror)."""

from __future__ import annotations

import json

import pytest

from awrtifact import hf, mirrorset, sealing

awseal_keys = pytest.importorskip("awseal.keys")

REF = hf.HfRef("model", "org/name", "main", "")


def _set():
    files = [hf.HfFile("config.json", 7, None, ""), hf.HfFile("w.bin", 10, "a" * 64, "")]
    return mirrorset.build(files, ref=REF, commit="c", repo="o/r", release="rel")


@pytest.fixture
def key(tmp_path):
    return awseal_keys.generate(tmp_path / "k.key")


def test_sign_then_verify_roundtrip(tmp_path, key):
    data = _set()
    out = sealing.sign_set(data, tmp_path, key_path=key)
    assert out.name == "org--name.awseal.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["subject"] == "org--name" and doc["meta"]["release"] == "rel"
    verdict = sealing.verify_set(data, out.read_bytes())
    assert verdict["ok"] and verdict["signature_ok"] and verdict["content_ok"]
    pub = awseal_keys.public_key_hex(path=key)
    assert sealing.verify_set(data, out.read_bytes(), expect_key=pub)["ok"]


def test_tampered_manifest_fails(tmp_path, key):
    data = _set()
    seal = sealing.sign_set(data, tmp_path, key_path=key).read_bytes()
    data["files"][1]["sha256"] = "b" * 64
    verdict = sealing.verify_set(data, seal)
    assert not verdict["ok"] and not verdict["content_ok"]


def test_foreign_key_is_refused(tmp_path, key):
    data = _set()
    seal = sealing.sign_set(data, tmp_path, key_path=key).read_bytes()
    other = awseal_keys.generate(tmp_path / "other.key")
    verdict = sealing.verify_set(data, seal, expect_key=awseal_keys.public_key_hex(path=other))
    assert verdict["signature_ok"] and verdict["key_trusted"] is False and not verdict["ok"]


def test_garbage_seal_is_a_verdict_not_an_exception(tmp_path):
    verdict = sealing.verify_set(_set(), b"{not json")
    assert verdict["ok"] is False and "error" in verdict


def test_seal_without_awseal_is_loud(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("awseal"):
            raise ImportError("no awseal")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(sealing.SealUnavailableError):
        sealing._awseal()


def test_check_key_fails_before_any_write(tmp_path, key):
    assert sealing.check_key(key) == awseal_keys.public_key_hex(path=key)
    with pytest.raises(ValueError, match="no signing key"):
        sealing.check_key(tmp_path / "absent.key")
