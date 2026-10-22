"""Sealed archives -- a file the platform can WRITE to a private repo but never READ.

`seal-archive` streams one file into AES-256-GCM ciphertext under a fresh
per-object data key (DEK), wraps that DEK to the KEY HOLDER's Curve25519 public
key (libsodium SealedBox -- the same recipient mechanism and key format as the
GargBot tenant backup, `backup_envelope.seal_dek_pubkey`, and the generated
tenant repo's `recipient.pub`), and uploads the ciphertext as release assets of
a PRIVATE GitHub repo. Only the holder of the private key can run
`unseal-restore`; whoever sealed it cannot, because the DEK leaves this process
only in its wrapped form. There is no platform key, no passphrase fallback and
no escrow wrap.

Ciphertext layout (AWSH1, byte-compatible with `AitherOS/lib/share/envelope.py`
with `share_id = archive_id`, so its ranged decryptor can read a part range):

    header   b"AWSH1" | segment_size (u32 BE) | nonce_prefix (8 random bytes)
    segment  AES-256-GCM(dek, nonce_prefix || index (u32 BE), plaintext slice,
                         aad = b"aither-share/v1|" + archive_id + b"|"
                               + index (u32 BE) + final (1 byte))

The index in nonce AND aad refuses reordering; `final` refuses a ciphertext
truncated at a segment boundary. The ciphertext STREAM is then cut into release
assets of `part_size` bytes (< 2 GiB, GitHub's per-asset cap), so a segment may
straddle two parts -- the reader chains them.

Resource contract (critic #1 of the tiered-storage design):

- RAM: one segment (1 MiB) plus its tag, on both sides.
- Disk while sealing: ONE part file at a time -- it is written, uploaded and
  deleted before the next starts, so peak extra disk is min(part_size,
  ciphertext) -- <= 1.0x the file plus the 17-byte header and 16 bytes a MiB.
- Disk while restoring: one downloaded part plus the `.partial` output, which
  is renamed into place only after the plaintext sha256 matches the manifest.

Manifest `<name>.sealed.json` is uploaded LAST, so a release without it is an
interrupted seal and restore refuses it. It carries per-part ciphertext sha256,
the whole-ciphertext sha256 and the plaintext sha256 + size. The plaintext
sha256 lets anyone with READ access to the repo confirm a guess of the file's
exact bytes -- one reason the repo must be private.

Never used here: `fetch --url`, artifact.aitherium.com or any public URL.
Every byte moves through `gh.upload` / `gh.download_asset`, i.e. the caller's
own GitHub auth.

Dependencies: `cryptography` (AES-GCM) and `PyNaCl` (SealedBox) -- extra
`awrtifact[archive]`. Without them every call raises CryptoUnavailableError
before any GitHub write; nothing is ever uploaded in plaintext as a fallback.
Key material is a parameter or a file path only: never logged, never in an
exception message, never in argv.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from . import gh

FORMAT = "awrtifact-sealed/v1"
MAGIC = b"AWSH1"
HEADER_LEN = len(MAGIC) + 4 + 8
TAG_LEN = 16
KEY_LEN = 32
SEGMENT_SIZE = 1024 * 1024
AAD_PREFIX = b"aither-share/v1|"
DEFAULT_PART_SIZE = 1_900_000_000
MAX_PART_SIZE = 2_000_000_000  # strictly under GitHub's 2 GiB asset cap
MIN_PART_SIZE = 1024 * 1024
MANIFEST_SUFFIX = ".sealed.json"
PART_INFIX = ".sealed.part"
WRAP_SCHEME = "nacl-sealedbox"
DEFAULT_KEY_ENV = "AWRTIFACT_ARCHIVE_KEY"
# Repos that SERVE public bytes. Never a sealed-archive target even if one
# were flipped private for a moment.
DENY_REPOS = frozenset({"aitherium/aitherkvcache"})
FREE_SPACE_MARGIN = 64 * 1024 * 1024
INSTALL_HINT = "pip install 'awrtifact[archive]'  (cryptography + PyNaCl)"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CryptoUnavailableError(RuntimeError):
    """cryptography / PyNaCl are not importable -- nothing can be sealed."""


class RefusedError(ValueError):
    """A precondition refused the operation (public repo, bad key, bad input)."""


class ArchiveError(RuntimeError):
    """An operation failed (hash mismatch, wrong key, missing part)."""


def _deps():
    try:
        from cryptography.exceptions import InvalidTag  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
        from nacl.exceptions import CryptoError  # noqa: PLC0415
        from nacl.public import PrivateKey, PublicKey, SealedBox  # noqa: PLC0415
    except ImportError as exc:
        raise CryptoUnavailableError(f"sealed archives need {INSTALL_HINT}") from exc
    return SimpleNamespace(AESGCM=AESGCM, InvalidTag=InvalidTag, PrivateKey=PrivateKey,
                           PublicKey=PublicKey, SealedBox=SealedBox, CryptoError=CryptoError)


def require_crypto() -> None:
    _deps()


# -- keys --------------------------------------------------------------------

def key_id(public_key_b64: str) -> str:
    """Same fingerprint as backup_envelope.public_key_id: sha256(raw)[:16]."""
    return hashlib.sha256(_decode_key(public_key_b64, "public")).hexdigest()[:16]


def _decode_key(text: str, kind: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RefusedError(f"{kind} key is not base64") from exc
    if len(raw) != KEY_LEN:
        raise RefusedError(f"{kind} key must be {KEY_LEN} bytes (Curve25519), got {len(raw)}")
    return raw


def _first_key_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def default_recipients_dir() -> Path:
    env = os.getenv("AWRTIFACT_RECIPIENTS_DIR")
    return Path(env) if env else Path.home() / ".aither" / "recipients"


def load_recipient(spec: str, recipients_dir: Optional[Path] = None) -> tuple[str, str]:
    """(public_key_b64, key_id) from a .pub file path, a 16-hex key id found in
    the recipients dir as `<id>.pub`, or the base64 key itself."""
    spec = (spec or "").strip()
    if not spec:
        raise RefusedError("--recipient is required (a .pub path, key id, or base64 key)")
    path = Path(spec)
    if _KEY_ID_RE.match(spec) and not path.is_file():
        path = (recipients_dir or default_recipients_dir()) / f"{spec}.pub"
        if not path.is_file():
            raise RefusedError(f"no public key for key id {spec} at {path}")
    if path.is_file():
        pub = _first_key_line(path.read_text(encoding="utf-8"))
    else:
        pub = spec
    kid = key_id(pub)
    if _KEY_ID_RE.match(spec) and kid != spec:
        raise RefusedError(f"{path} holds key id {kid}, not {spec}")
    return pub.strip(), kid


def load_private_key(key_file: str = "", key_env: str = DEFAULT_KEY_ENV) -> str:
    """The private key text from a file (preferred) or an env var. Never argv."""
    if key_file:
        text = _first_key_line(Path(key_file).read_text(encoding="utf-8"))
        source = f"key file {key_file}"
    else:
        text = (os.getenv(key_env) or "").strip() if key_env else ""
        source = f"${key_env}"
    if not text:
        raise RefusedError(
            f"no private key: pass --key-file PATH or set {key_env or DEFAULT_KEY_ENV}"
        )
    try:
        _decode_key(text, "private")
    except RefusedError as exc:
        raise RefusedError(f"{source}: {exc}") from None
    return text


def generate_keypair() -> tuple[str, str]:
    """(public_b64, private_b64). Run this on the KEY HOLDER's machine."""
    sk = _deps().PrivateKey.generate()
    return (base64.b64encode(bytes(sk.public_key)).decode("ascii"),
            base64.b64encode(bytes(sk)).decode("ascii"))


def public_from_private(private_b64: str) -> str:
    sk = _deps().PrivateKey(_decode_key(private_b64, "private"))
    return base64.b64encode(bytes(sk.public_key)).decode("ascii")


def _write_new(path: Path, text: str, mode: int, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(str(path), flags, mode)
    except FileExistsError as exc:
        raise RefusedError(f"{path} exists; refusing to overwrite a key (--force)") from exc
    # os.open applies `mode` at creation (umask can only narrow it); on Windows
    # mode bits are advisory -- keep the file under the user's profile.
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def keygen(private_out: Path, public_out: Path, *, force: bool = False,
           github_secret: str = "", secret_repo: str = "") -> dict:
    """Make a recipient keypair. The private half goes to a mode-600 file and,
    optionally, into an Actions secret of the holder's OWN private repo (read
    from stdin by `gh secret set`, never argv). Returns ids and paths only."""
    if github_secret and not _SECRET_NAME_RE.match(github_secret):
        raise RefusedError(f"invalid GitHub secret name {github_secret!r}")
    if github_secret:
        if not secret_repo:
            raise RefusedError("--github-secret needs --secret-repo OWNER/REPO")
        guard_private_repo(secret_repo)
    pub, priv = generate_keypair()
    kid = key_id(pub)
    _write_new(private_out, f"# awrtifact archive PRIVATE key {kid} -- never share\n"
               f"{priv}\n", 0o600, force)
    _write_new(public_out, f"# awrtifact archive recipient {kid}\n{pub}\n", 0o644, force)
    stored = ""
    if github_secret:
        gh.secret_set(secret_repo, github_secret, priv)
        stored = f"{secret_repo}:{github_secret}"
    del priv
    return {"key_id": kid, "public": str(public_out), "private": str(private_out),
            "github_secret": stored}


# -- repo guard --------------------------------------------------------------

def guard_private_repo(repo: str) -> None:
    """Refuse unless GitHub itself says the repo is private. An unreadable answer
    is a refusal, never a pass."""
    if not _REPO_RE.match(repo or ""):
        raise RefusedError(f"--repo must be OWNER/NAME, got {repo!r}")
    if repo.lower() in DENY_REPOS:
        raise RefusedError(f"{repo} serves public bytes; never a sealed-archive target")
    try:
        private = gh.repo_private(repo)
    except gh.GhError as exc:
        raise RefusedError(f"cannot read the visibility of {repo}; refusing ({exc})") from exc
    if private is None:
        raise RefusedError(f"{repo} does not exist or is not visible to this auth")
    if private is not True:
        raise RefusedError(f"{repo} is PUBLIC; sealed archives go to private repos only")


# -- segment crypto ----------------------------------------------------------

def _aad(archive_id: str, index: int, final: bool) -> bytes:
    return (AAD_PREFIX + archive_id.encode("ascii") + b"|" + struct.pack(">I", index)
            + (b"\x01" if final else b"\x00"))


def _segments(plain_size: int, segment_size: int) -> int:
    return 1 if plain_size == 0 else -(-plain_size // segment_size)


def ciphertext_size(plain_size: int, segment_size: int = SEGMENT_SIZE) -> int:
    return HEADER_LEN + plain_size + _segments(plain_size, segment_size) * TAG_LEN


def safe_name(name: str) -> str:
    cleaned = _NAME_RE.sub("_", name).strip("._") or "archive"
    return cleaned[:160]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(SEGMENT_SIZE), b""):
            h.update(block)
    return h.hexdigest()


def _manifest_digest(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


class _PartWriter:
    """Writes the ciphertext stream into `<base>.sealed.partN` files, handing each
    full part to `ship` (upload + delete) before the next is opened."""

    def __init__(self, work: Path, base: str, part_size: int,
                 ship: Callable[[Path], None]):
        self.work, self.base, self.part_size, self.ship = work, base, part_size, ship
        self.parts: list[dict] = []
        self.ct_hash = hashlib.sha256()
        self.offset = 0
        self.peak_bytes = 0
        self._fh = None
        self._path: Optional[Path] = None
        self._hash = None
        self._size = 0

    def _open(self) -> None:
        self._path = self.work / f"{self.base}{PART_INFIX}{len(self.parts)}"
        self._fh = self._path.open("wb")
        self._hash = hashlib.sha256()
        self._size = 0

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self._fh is None:
                self._open()
            room = self.part_size - self._size
            chunk = view[:room]
            self._fh.write(chunk)
            self._hash.update(chunk)
            self.ct_hash.update(chunk)
            self._size += len(chunk)
            self.peak_bytes = max(self.peak_bytes, self._size)
            view = view[len(chunk):]
            if self._size == self.part_size:
                self._close()

    def _close(self) -> None:
        if self._fh is None:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        path = self._path
        self.parts.append({"name": path.name, "offset": self.offset, "size": self._size,
                           "sha256": self._hash.hexdigest()})
        self.offset += self._size
        try:
            self.ship(path)
        finally:
            path.unlink(missing_ok=True)

    def finish(self) -> None:
        self._close()

    def abort(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)


def _work_dir(work_dir: Optional[Path], prefix: str) -> Path:
    """A fresh private dir (under `work_dir` when given), removed by the caller."""
    if work_dir:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(work_dir)))
    return Path(tempfile.mkdtemp(prefix=prefix))


# -- seal --------------------------------------------------------------------

def seal_archive(file: Path, repo: str, tag: str, recipient_pub_b64: str, *,
                 name: str = "", part_size: int = DEFAULT_PART_SIZE,
                 work_dir: Optional[Path] = None,
                 segment_size: int = SEGMENT_SIZE) -> dict:
    """Seal `file` into release `tag` of the PRIVATE `repo`. Returns the manifest
    plus `peak_work_bytes` (the most ciphertext ever on local disk at once)."""
    c = _deps()
    if not MIN_PART_SIZE <= part_size <= MAX_PART_SIZE:
        raise RefusedError(f"part size must be {MIN_PART_SIZE}..{MAX_PART_SIZE} bytes")
    if not 1 <= segment_size <= 64 * 1024 * 1024:
        raise RefusedError("segment size out of range")
    if not tag or "/" in tag or tag.startswith("-"):
        raise RefusedError(f"bad release tag {tag!r}")
    file = Path(file)
    if not file.is_file():
        raise RefusedError(f"{file} is not a file")
    pub_raw = _decode_key(recipient_pub_b64, "recipient public")
    kid = key_id(recipient_pub_b64)
    guard_private_repo(repo)

    base = safe_name(name or file.name)
    manifest_name = f"{base}{MANIFEST_SUFFIX}"
    if gh.release_exists(repo, tag):
        if manifest_name in gh.release_assets(repo, tag):
            raise ArchiveError(f"{repo}@{tag} already holds {manifest_name}; "
                               "seal under a new tag (archives are immutable)")
    else:
        gh.create_release(repo, tag, f"sealed archive {tag}",
                          "awrtifact sealed archive: ciphertext only, private key "
                          "held by the recipient.")

    plain_size = file.stat().st_size
    ct_size = ciphertext_size(plain_size, segment_size)
    work = _work_dir(work_dir, "awrtifact-seal-")
    try:
        need = min(part_size, ct_size) + FREE_SPACE_MARGIN
        free = shutil.disk_usage(str(work)).free
        if free < need:
            raise ArchiveError(f"{work}: {free} bytes free, a part needs {need}")

        dek = os.urandom(KEY_LEN)
        archive_id = os.urandom(16).hex()
        prefix = os.urandom(8)
        wrap = base64.b64encode(c.SealedBox(c.PublicKey(pub_raw)).encrypt(dek)).decode("ascii")
        aead = c.AESGCM(dek)
        del dek

        writer = _PartWriter(work, base, part_size,
                             lambda p: gh.upload(repo, tag, str(p)))
        plain_hash = hashlib.sha256()
        segs = _segments(plain_size, segment_size)
        try:
            writer.write(MAGIC + struct.pack(">I", segment_size) + prefix)
            with file.open("rb") as fh:
                for i in range(segs):
                    want = min(segment_size, plain_size - i * segment_size)
                    chunk = fh.read(want) if want else b""
                    if len(chunk) != want:
                        raise ArchiveError(f"{file} shrank while sealing")
                    plain_hash.update(chunk)
                    writer.write(aead.encrypt(prefix + struct.pack(">I", i), chunk,
                                              _aad(archive_id, i, i == segs - 1)))
                if fh.read(1):
                    raise ArchiveError(f"{file} grew while sealing")
            writer.finish()
        except BaseException:
            writer.abort()
            raise

        manifest = {
            "format": FORMAT,
            "version": 1,
            "name": base,
            "archive_id": archive_id,
            "cipher": "AES-256-GCM",
            "envelope": "AWSH1",
            "segment_size": segment_size,
            "plain_size": plain_size,
            "plain_sha256": plain_hash.hexdigest(),
            "ct_size": writer.offset,
            "ct_sha256": writer.ct_hash.hexdigest(),
            "parts": writer.parts,
            "wraps": {"pubkey": {"scheme": WRAP_SCHEME, "recipient_key_id": kid,
                                 "blob": wrap}},
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if manifest["ct_size"] != ct_size:
            raise ArchiveError("ciphertext size disagrees with the layout")
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        mpath = work / manifest_name
        mpath.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        try:
            gh.upload(repo, tag, str(mpath))
        finally:
            mpath.unlink(missing_ok=True)

        remote = gh.asset_sizes(repo, tag)
        short = [p["name"] for p in writer.parts if remote.get(p["name"]) != p["size"]]
        if short or manifest_name not in remote:
            raise ArchiveError(f"release does not show the uploaded sizes for: "
                               f"{short or [manifest_name]}")
        return {**manifest, "repo": repo, "tag": tag, "peak_work_bytes": writer.peak_bytes}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- restore -----------------------------------------------------------------

def _validate_manifest(m: dict) -> None:
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise ArchiveError("not an awrtifact sealed-archive manifest")
    if m.get("manifest_sha256") != _manifest_digest(m):
        raise ArchiveError("manifest digest mismatch (corrupt or edited)")
    for key in ("name", "archive_id", "segment_size", "plain_size", "plain_sha256",
                "ct_size", "ct_sha256", "parts", "wraps"):
        if key not in m:
            raise ArchiveError(f"manifest missing {key}")
    offset = 0
    for part in m["parts"]:
        pname = str(part.get("name", ""))
        if part.get("offset") != offset or not pname or _NAME_RE.search(pname):
            raise ArchiveError(f"manifest part {part.get('name')!r} is malformed")
        offset += int(part["size"])
    if offset != m["ct_size"] or m["ct_size"] != ciphertext_size(m["plain_size"],
                                                                  m["segment_size"]):
        raise ArchiveError("manifest sizes do not add up")


class _ChainReader:
    """Reads the ciphertext stream across parts, downloading + verifying each
    part on first touch and deleting it once consumed."""

    def __init__(self, parts: list[dict], fetch: Callable[[dict], Path]):
        self.parts, self.fetch = parts, fetch
        self.idx = -1
        self._fh = None
        self._path: Optional[Path] = None
        self.ct_hash = hashlib.sha256()
        self.peak_bytes = 0

    def _next(self) -> bool:
        self._drop()
        self.idx += 1
        if self.idx >= len(self.parts):
            return False
        part = self.parts[self.idx]
        self._path = self.fetch(part)
        size = self._path.stat().st_size
        self.peak_bytes = max(self.peak_bytes, size)
        if size != int(part["size"]):
            raise ArchiveError(f"{part['name']}: {size} bytes, manifest says {part['size']}")
        if _sha256_file(self._path) != part["sha256"]:
            raise ArchiveError(f"{part['name']}: ciphertext sha256 mismatch (tampered?)")
        self._fh = self._path.open("rb")
        return True

    def read(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            if self._fh is None and not self._next():
                break
            chunk = self._fh.read(n - len(out))
            if not chunk:
                self._fh.close()
                self._fh = None
                continue
            out += chunk
        self.ct_hash.update(out)
        return bytes(out)

    def _drop(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None

    def close(self) -> None:
        self._drop()


def find_manifest(repo: str, tag: str, name: str = "") -> str:
    if name:
        return f"{safe_name(name)}{MANIFEST_SUFFIX}"
    found = sorted(a for a in gh.release_assets(repo, tag) if a.endswith(MANIFEST_SUFFIX))
    if not found:
        raise ArchiveError(f"{repo}@{tag} holds no {MANIFEST_SUFFIX} (missing, or an "
                           "interrupted seal)")
    if len(found) > 1:
        raise RefusedError(f"{repo}@{tag} holds several archives; pass --name "
                           f"({', '.join(a[:-len(MANIFEST_SUFFIX)] for a in found)})")
    return found[0]


def unseal_restore(repo: str, tag: str, out: Optional[Path], private_key_b64: str, *,
                   name: str = "", work_dir: Optional[Path] = None,
                   verify_only: bool = False, force: bool = False) -> dict:
    """Download, verify and decrypt a sealed archive with the recipient's private
    key. Fails closed: nothing appears at `out` unless every part sha256, every
    GCM tag and the plaintext sha256 check out."""
    c = _deps()
    if not _REPO_RE.match(repo or ""):
        raise RefusedError(f"--repo must be OWNER/NAME, got {repo!r}")
    sk_raw = _decode_key(private_key_b64, "private")
    if not verify_only and out is None:
        raise RefusedError("--out is required unless --verify-only")

    work = _work_dir(work_dir, "awrtifact-unseal-")
    partial: Optional[Path] = None
    reader: Optional[_ChainReader] = None
    try:
        manifest_name = find_manifest(repo, tag, name)
        mpath = work / manifest_name
        gh.download_asset(repo, tag, manifest_name, str(mpath))
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ArchiveError(f"{manifest_name} is not JSON") from exc
        mpath.unlink(missing_ok=True)
        _validate_manifest(m)

        wrap = (m["wraps"] or {}).get("pubkey") or {}
        sk = c.PrivateKey(sk_raw)
        mine = key_id(base64.b64encode(bytes(sk.public_key)).decode("ascii"))
        if wrap.get("scheme") != WRAP_SCHEME or not wrap.get("blob"):
            raise ArchiveError("manifest has no recipient wrap")
        if wrap.get("recipient_key_id") != mine:
            raise ArchiveError(f"this private key ({mine}) is not the archive's "
                               f"recipient ({wrap.get('recipient_key_id')})")
        try:
            dek = c.SealedBox(sk).decrypt(base64.b64decode(wrap["blob"]))
        except (c.CryptoError, ValueError) as exc:
            raise ArchiveError("the private key does not open the data key") from exc
        aead = c.AESGCM(dek)
        del dek

        target: Optional[Path] = None
        if not verify_only:
            target = Path(out)
            if target.is_dir():
                target = target / m["name"]
            if target.exists() and not force:
                raise RefusedError(f"{target} exists (--force to replace)")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")

        def fetch(part: dict) -> Path:
            dest = work / part["name"]
            gh.download_asset(repo, tag, part["name"], str(dest))
            return dest

        reader = _ChainReader(m["parts"], fetch)
        header = reader.read(HEADER_LEN)
        if len(header) != HEADER_LEN or header[:len(MAGIC)] != MAGIC:
            raise ArchiveError("ciphertext header is not AWSH1")
        (seg,) = struct.unpack(">I", header[len(MAGIC):len(MAGIC) + 4])
        if seg != m["segment_size"]:
            raise ArchiveError("header segment size disagrees with the manifest")
        prefix = header[len(MAGIC) + 4:]
        plain_size = int(m["plain_size"])
        segs = _segments(plain_size, seg)
        plain_hash = hashlib.sha256()
        written = 0
        sink = partial.open("wb") if partial else None
        try:
            for i in range(segs):
                rec = min(seg, plain_size - i * seg) + TAG_LEN
                blob = reader.read(rec)
                if len(blob) != rec:
                    raise ArchiveError(f"ciphertext ends inside segment {i}")
                try:
                    plain = aead.decrypt(prefix + struct.pack(">I", i), blob,
                                         _aad(m["archive_id"], i, i == segs - 1))
                except c.InvalidTag as exc:
                    raise ArchiveError(f"segment {i} failed authentication") from exc
                plain_hash.update(plain)
                written += len(plain)
                if sink:
                    sink.write(plain)
            if reader.read(1):
                raise ArchiveError("trailing bytes after the final segment")
            if sink:
                sink.flush()
                os.fsync(sink.fileno())
        finally:
            if sink:
                sink.close()
        if reader.ct_hash.hexdigest() != m["ct_sha256"]:
            raise ArchiveError("whole-ciphertext sha256 mismatch")
        if written != plain_size or plain_hash.hexdigest() != m["plain_sha256"]:
            raise ArchiveError("plaintext sha256 mismatch after decrypt")
        if partial and target:
            os.replace(str(partial), str(target))
            partial = None
        return {"status": "verified" if verify_only else "restored",
                "path": str(target) if target else "", "bytes": written,
                "plain_sha256": m["plain_sha256"], "parts": len(m["parts"]),
                "recipient_key_id": mine, "peak_work_bytes": reader.peak_bytes}
    finally:
        if reader is not None:
            reader.close()
        if partial is not None:
            partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
