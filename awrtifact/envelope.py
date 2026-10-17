"""The encrypted backup envelope -- byte-compatible with the GobboNet backup mod.

`data/gobbonet/gobbonet-backup.js` (the browser mod) and `backup-gate.html`
(the share page) encrypt and decrypt with WebCrypto. This module writes and
reads the SAME bytes from Python, so a backup made here opens in the gate page
and a backup made in the browser restores here:

    key        PBKDF2-HMAC-SHA256(passphrase as UTF-8, salt, 600 000 iterations)
               -> 32 bytes, used as an AES-256-GCM key
    salt       16 random bytes, hex, ONE per backup (in the manifest)
    part       AES-GCM(key, iv, plaintext slice), 12-byte random iv per part,
               no associated data; the asset is ciphertext || 16-byte tag
               (WebCrypto's layout, which is also `AESGCM.encrypt`'s)
    names      <file>.partN -- the awrtifact chunk contract
    manifest   <tag>.backup.json:
               {version: 1, kdf: "pbkdf2-sha256", iterations, salt, name: tag,
                files: [{name, size, parts: [{name, size, sha256, plain_sha256,
                iv}], plain_sha256}], manifest_sha256}
               `sha256` is of the CIPHERTEXT (checked before decrypting),
               `plain_sha256` of the slice and of the whole file (checked
               after). `manifest_sha256` is sha256 of the compact JSON of the
               manifest without that key, exactly as `JSON.stringify` emits it.

`cryptography` is the only dependency. Without it every call raises
CryptoUnavailableError -- nothing is ever written in plaintext as a fallback.
The passphrase is a parameter only: never stored, never logged, never echoed
in an exception message.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Sequence

ITERATIONS = 600000
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32
KDF = "pbkdf2-sha256"
VERSION = 1
MIN_PASSPHRASE = 8  # the mod refuses shorter; so does this side
# The awrtifact part contract -- under GitHub's 2 GiB per-asset cap, and
# under AESGCM's 2**31-1 single-call limit.
PART_SIZE = 1900000000
MANIFEST_SUFFIX = ".backup.json"

INSTALL_HINT = "pip install cryptography  (or: pip install 'awrtifact[crypto]')"


class CryptoUnavailableError(RuntimeError):
    """`cryptography` is not importable -- nothing can be encrypted here."""


class EnvelopeError(ValueError):
    """A manifest, part or passphrase failed a check. Never carries the passphrase."""


def _primitives():
    try:
        from cryptography.hazmat.primitives import hashes as _hashes  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # noqa: PLC0415
    except ImportError as exc:
        raise CryptoUnavailableError(
            f"the `cryptography` package is required to encrypt backups: {INSTALL_HINT}"
        ) from exc
    return _hashes, AESGCM, PBKDF2HMAC


def require_crypto() -> None:
    """Raise CryptoUnavailableError now, before any bundling work starts."""
    _primitives()


def check_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE:
        raise EnvelopeError(f"passphrase must be {MIN_PASSPHRASE}+ characters")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_salt() -> str:
    return os.urandom(SALT_BYTES).hex()


def derive_key(passphrase: str, salt_hex: str, iterations: int = ITERATIONS) -> bytes:
    """The mod's `deriveKey`: PBKDF2-SHA256 over the UTF-8 passphrase -> 32 bytes."""
    hashes_mod, _aes, pbkdf2 = _primitives()
    try:
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError("manifest salt is not hex") from exc
    kdf = pbkdf2(algorithm=hashes_mod.SHA256(), length=KEY_BYTES, salt=salt,
                 iterations=int(iterations))
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_part(key: bytes, data: bytes, iv: bytes | None = None) -> tuple[str, bytes]:
    """The mod's `encryptPart`: (iv hex, ciphertext||tag)."""
    _h, aesgcm, _k = _primitives()
    iv = iv if iv is not None else os.urandom(IV_BYTES)
    if len(iv) != IV_BYTES:
        raise EnvelopeError(f"iv must be {IV_BYTES} bytes")
    return iv.hex(), aesgcm(key).encrypt(iv, data, None)


def decrypt_part(key: bytes, iv_hex: str, cipher: bytes) -> bytes:
    """The mod's `decryptPart`. A wrong passphrase is an EnvelopeError, not a traceback."""
    _h, aesgcm, _k = _primitives()
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415

    try:
        return aesgcm(key).decrypt(bytes.fromhex(iv_hex), cipher, None)
    except InvalidTag as exc:
        raise EnvelopeError("decryption failed: wrong passphrase or corrupted part") from exc


def manifest_digest(manifest: dict) -> str:
    """sha256 of `JSON.stringify(manifest)` (compact, insertion order) sans the digest."""
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return sha256_hex(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def encrypt_files(
    files: Sequence[tuple[str, Path]],
    passphrase: str,
    out_dir: Path,
    name: str,
    part_size: int = PART_SIZE,
    iterations: int = ITERATIONS,
) -> dict:
    """Encrypt each (asset name, path) into `out_dir/<name>.partN`; return the manifest.

    The manifest is also written as `out_dir/<name>.backup.json`, indented the
    way the mod writes it.
    """
    check_passphrase(passphrase)
    if part_size <= 0 or part_size > PART_SIZE:
        raise EnvelopeError(f"part_size must be in (0, {PART_SIZE}]")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    salt = new_salt()
    key = derive_key(passphrase, salt, iterations)
    manifest: dict = {"version": VERSION, "kdf": KDF, "iterations": int(iterations),
                      "salt": salt, "name": name, "files": []}
    for asset_name, path in files:
        _check_asset_name(asset_name)
        path = Path(path)
        size = path.stat().st_size
        entry: dict = {"name": asset_name, "size": size, "parts": []}
        whole = hashlib.sha256()
        with open(path, "rb") as fh:
            idx = 0
            while True:
                raw = fh.read(part_size)
                if not raw and idx > 0:
                    break
                whole.update(raw)
                iv_hex, cipher = encrypt_part(key, raw)
                part_name = f"{asset_name}.part{idx}"
                (out_dir / part_name).write_bytes(cipher)
                entry["parts"].append({"name": part_name, "size": len(cipher),
                                       "sha256": sha256_hex(cipher),
                                       "plain_sha256": sha256_hex(raw), "iv": iv_hex})
                idx += 1
                if len(raw) < part_size:
                    break
        entry["plain_sha256"] = whole.hexdigest()
        manifest["files"].append(entry)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    (out_dir / f"{name}{MANIFEST_SUFFIX}").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise EnvelopeError("backup manifest is not an object")
    for field in ("version", "kdf", "iterations", "salt", "name", "files"):
        if field not in manifest:
            raise EnvelopeError(f"backup manifest missing {field!r}")
    if manifest["kdf"] != KDF:
        raise EnvelopeError(f"unsupported kdf {manifest['kdf']!r}")
    if manifest["version"] != VERSION:
        raise EnvelopeError(f"unsupported manifest version {manifest['version']!r}")
    stated = manifest.get("manifest_sha256")
    if stated is not None and stated != manifest_digest(manifest):
        raise EnvelopeError("backup manifest failed its manifest_sha256")
    for f in manifest["files"]:
        _check_asset_name(f.get("name", ""))
        for p in f.get("parts") or []:
            _check_asset_name(p.get("name", ""))
    return manifest


def decrypt_files(manifest: dict, passphrase: str, part_dir: Path,
                  out_dir: Path) -> list[Path]:
    """Verify + decrypt every file in the manifest from `part_dir` into `out_dir`.

    Order of checks per part: ciphertext sha256, then decrypt (the GCM tag),
    then plaintext sha256; then the whole-file sha256. Any failure raises and
    leaves no partially-written output file behind.
    """
    validate_manifest(manifest)
    key = derive_key(passphrase, manifest["salt"], manifest["iterations"])
    part_dir, out_dir = Path(part_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for f in manifest["files"]:
        target = out_dir / f["name"]
        tmp = target.with_name(target.name + ".partial")
        whole = hashlib.sha256()
        try:
            with open(tmp, "wb") as dst:
                for p in f["parts"]:
                    cipher = (part_dir / p["name"]).read_bytes()
                    if sha256_hex(cipher) != p["sha256"]:
                        raise EnvelopeError(f"part {p['name']} failed sha256")
                    plain = decrypt_part(key, p["iv"], cipher)
                    if sha256_hex(plain) != p["plain_sha256"]:
                        raise EnvelopeError(
                            f"part {p['name']} failed plaintext sha256 after decrypt")
                    whole.update(plain)
                    dst.write(plain)
            if whole.hexdigest() != f["plain_sha256"]:
                raise EnvelopeError(f"file {f['name']} failed whole-file sha256")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        written.append(target)
    return written


def part_names(manifest: dict) -> Iterable[str]:
    for f in manifest["files"]:
        for p in f["parts"]:
            yield p["name"]


def _check_asset_name(name: str) -> None:
    # Asset names become local paths on restore: no separators, no dot-dirs.
    if (not isinstance(name, str) or not name or "/" in name or "\\" in name
            or name in (".", "..") or name.startswith(".")):
        raise EnvelopeError(f"unsafe asset name in backup manifest: {name!r}")
