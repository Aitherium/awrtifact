"""Sealed archives -- a file the platform can WRITE to a private repo but never READ.

`seal-archive` streams one file into AES-256-GCM ciphertext under a fresh
per-object data key (DEK), wraps that DEK to the KEY HOLDER's Curve25519 public
key (libsodium SealedBox -- the same recipient mechanism and key format as the
tenant appliance backup, `backup_envelope.seal_dek_pubkey`, and the generated
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

Manifest `<archive_id>.sealed.json` is uploaded LAST, so a release without it
is an interrupted seal and restore refuses it. Asset names are OPAQUE (the
random archive id), so a reader of the repo learns sizes and dates, never a
file name. The public manifest carries per-part ciphertext sha256 and the
whole-ciphertext sha256 only. The original name, the plaintext sha256 and the
repo + tag the archive was sealed INTO travel in `meta`, AES-256-GCM under a
key derived from the DEK: only the key holder can read them, and restore
refuses an archive whose sealed repo/tag differs from where it was found (a
copied or replayed archive).

What this cannot stop: anyone holding the PUBLIC key can seal a whole new
archive. A caller that must know an archive holds ONE specific file (the
eviction guard, before an original is deleted) passes `expect_plain_sha256`
(and `archive_id`) recorded at seal time; any other content fails.

Never used here: `fetch --url`, artifact.aitherium.com or any public URL.
Every byte moves through `gh.upload` / `gh.download_asset`, i.e. the caller's
own GitHub auth.

Dependencies: `cryptography` (AES-GCM) and `PyNaCl` (SealedBox) -- extra
`awrtifact[archive]`. Without them every call raises CryptoUnavailableError
before any GitHub write; nothing is ever uploaded in plaintext as a fallback.
Key material is a parameter, a file, stdin or an env var: never logged, never
in an exception message, never in argv, never in a child process's
environment (the env var is removed as it is read and `gh` strips it).
`archive-keygen` writes the private key passphrase-protected (scrypt +
AES-256-GCM) by default, owner-only, and an Actions secret may hold only that
protected form, in a repo the key holder controls.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from . import gh

FORMAT = "awrtifact-sealed/v2"
MAGIC = b"AWSH1"
HEADER_LEN = len(MAGIC) + 4 + 8
TAG_LEN = 16
KEY_LEN = 32
SEGMENT_SIZE = 1024 * 1024
AAD_PREFIX = b"aither-share/v1|"
META_KEY_LABEL = b"awrtifact-sealed/v2 meta key"
META_AAD_PREFIX = b"awrtifact-sealed/v2|meta|"
DEFAULT_PART_SIZE = 1_900_000_000
MAX_PART_SIZE = 2_000_000_000  # strictly under GitHub's 2 GiB asset cap
MIN_PART_SIZE = 1024 * 1024
MANIFEST_SUFFIX = ".sealed.json"
PART_INFIX = ".sealed.part"
WRAP_SCHEME = "nacl-sealedbox"
DEFAULT_KEY_ENV = "AWRTIFACT_ARCHIVE_KEY"
DEFAULT_PASSPHRASE_ENV = "AWRTIFACT_ARCHIVE_PASSPHRASE"
# A passphrase-protected private key file line: scrypt -> AES-256-GCM.
ENC_KEY_PREFIX = "awrtifact-key/v1:"
ENC_KEY_AAD = b"awrtifact-key/v1|"
SCRYPT_N = 2 ** 17  # 128 MiB of scrypt memory: slow to brute force, fine once
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE = 12
# Repos that SERVE public bytes. Never a sealed-archive target even if one
# were flipped private for a moment.
DENY_REPOS = frozenset({"aitherium/aitherkvcache"})
# Owners whose admins are not the key holder: a recipient private key (even
# passphrase-wrapped) never goes into an Actions secret there.
DENY_SECRET_OWNERS = frozenset({"aitherium"})
FREE_SPACE_MARGIN = 64 * 1024 * 1024
INSTALL_HINT = "pip install 'awrtifact[archive]'  (cryptography + PyNaCl)"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Key material must never reach a child process (`gh`, icacls): gh strips
# these names from every environment it spawns.
gh.never_inherit(DEFAULT_KEY_ENV)
gh.never_inherit(DEFAULT_PASSPHRASE_ENV)


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
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: PLC0415
        from nacl.exceptions import CryptoError  # noqa: PLC0415
        from nacl.public import PrivateKey, PublicKey, SealedBox  # noqa: PLC0415
    except ImportError as exc:
        raise CryptoUnavailableError(f"sealed archives need {INSTALL_HINT}") from exc
    return SimpleNamespace(AESGCM=AESGCM, InvalidTag=InvalidTag, PrivateKey=PrivateKey,
                           PublicKey=PublicKey, SealedBox=SealedBox, CryptoError=CryptoError,
                           Scrypt=Scrypt)


def require_crypto() -> None:
    _deps()


# -- file permissions --------------------------------------------------------

def _owner_only(path: Path) -> bool:
    """Narrow `path` to its owner. POSIX: chmod 600 (also after a reopen, where
    the open mode is ignored). Windows: mode bits are advisory, so drop the
    inherited ACEs and grant only the current user. True on success."""
    if os.name != "nt":
        os.chmod(str(path), 0o600)
        return True
    user = os.environ.get("USERNAME", "")
    icacls = shutil.which("icacls")
    if not user or not icacls:
        return False
    domain = os.environ.get("USERDOMAIN", "")
    who = f"{domain}\\{user}" if domain else user
    proc = subprocess.run([icacls, str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
                          capture_output=True, check=False, env=gh.child_env())
    return proc.returncode == 0


def _create_private(path: Path, *, replace: bool) -> int:
    """A NEW file at mode 600, opened O_EXCL (never follows a planted link).
    `replace` removes an existing file first instead of failing."""
    if replace:
        path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.open(str(path), flags, 0o600)


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


def _pop_env(name: str) -> str:
    """Read AND remove an env var so no child process inherits it. (The value
    is still in the launching shell and in this process's initial environment
    block; a key file is the safer source.)"""
    if not name:
        return ""
    gh.never_inherit(name)
    return (os.environ.pop(name, None) or "").strip()


def read_passphrase(passphrase_file: str = "", env: str = DEFAULT_PASSPHRASE_ENV, *,
                    confirm: bool = False, prompt: str = "archive key passphrase: ") -> str:
    """From a file (first line), else the env var (removed as it is read), else
    an interactive prompt. Never argv. Non-interactive with no source = refusal."""
    if passphrase_file:
        lines = Path(passphrase_file).read_text(encoding="utf-8").splitlines()
        value = lines[0] if lines else ""
    else:
        value = _pop_env(env)
        if not value:
            if not sys.stdin or not sys.stdin.isatty():
                raise RefusedError("a passphrase is needed: --passphrase-file PATH, "
                                   f"${env}, or run interactively")
            value = getpass.getpass(prompt)
            if confirm and getpass.getpass("again: ") != value:
                raise RefusedError("the two passphrases differ")
    if not value:
        raise RefusedError("empty passphrase")
    return value


def _scrypt(c, passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return c.Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def encrypt_private_key(private_b64: str, passphrase: str) -> str:
    """The `awrtifact-key/v1:` line: scrypt(passphrase) -> AES-256-GCM(raw key),
    AAD bound to the key id so a wrapped blob cannot be relabelled."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise RefusedError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    c = _deps()
    raw = _decode_key(private_b64, "private")
    kid = key_id(public_from_private(private_b64))
    salt, nonce = os.urandom(16), os.urandom(12)
    kek = _scrypt(c, passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ct = c.AESGCM(kek).encrypt(nonce, raw, ENC_KEY_AAD + kid.encode("ascii"))
    body = {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "key_id": kid,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii")}
    return ENC_KEY_PREFIX + base64.b64encode(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")


def is_encrypted_key(text: str) -> bool:
    return text.strip().startswith(ENC_KEY_PREFIX)


def decrypt_private_key(line: str, passphrase: str) -> str:
    c = _deps()
    try:
        body = json.loads(base64.b64decode(line.strip()[len(ENC_KEY_PREFIX):], validate=True))
        n, r, p = int(body["n"]), int(body["r"]), int(body["p"])
        kid = str(body["key_id"])
        salt, nonce, ct = (base64.b64decode(body[k]) for k in ("salt", "nonce", "ct"))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise RefusedError("the encrypted key file is malformed") from exc
    if n & (n - 1) or not 2 ** 14 <= n <= 2 ** 20 or not 1 <= r <= 32 or not 1 <= p <= 16 \
            or not _KEY_ID_RE.match(kid):
        raise RefusedError("the encrypted key file has out-of-range parameters")
    kek = _scrypt(c, passphrase, salt, n, r, p)
    try:
        raw = c.AESGCM(kek).decrypt(nonce, ct, ENC_KEY_AAD + kid.encode("ascii"))
    except c.InvalidTag:
        raise RefusedError("wrong passphrase for the archive key") from None
    priv = base64.b64encode(raw).decode("ascii")
    if key_id(public_from_private(priv)) != kid:
        raise RefusedError("the encrypted key file does not hold the key it names")
    return priv


def load_private_key(key_file: str = "", key_env: str = DEFAULT_KEY_ENV, *,
                     key_stdin: bool = False, passphrase_file: str = "",
                     passphrase_env: str = DEFAULT_PASSPHRASE_ENV) -> str:
    """The raw private key (base64) from a key file (preferred; raw or
    passphrase-protected), stdin, or an env var -- never argv. The env var is
    REMOVED from this process's environment as it is read, so `gh` and every
    other child process never inherits it."""
    env_value = _pop_env(key_env) if key_env else ""
    if key_file:
        text = _first_key_line(Path(key_file).read_text(encoding="utf-8"))
        source = f"key file {key_file}"
    elif key_stdin:
        text = _first_key_line(sys.stdin.read())
        source = "stdin"
    else:
        text, source = env_value, f"${key_env}"
    del env_value
    if not text:
        raise RefusedError(
            f"no private key: pass --key-file PATH, --key-stdin, or set "
            f"{key_env or DEFAULT_KEY_ENV}")
    if is_encrypted_key(text):
        return decrypt_private_key(text, read_passphrase(passphrase_file, passphrase_env))
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


def _write_new(path: Path, text: str, *, private: bool, force: bool) -> bool:
    """Write a key file; refuse to overwrite unless `force`. A private file is
    created fresh (O_EXCL, mode 600) and then narrowed to the owner, so a forced
    overwrite never keeps the old file's wider mode. Returns whether the
    owner-only restriction took."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise RefusedError(f"{path} exists; refusing to overwrite a key (--force)")
    if private:
        fd = _create_private(path, replace=force)
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return _owner_only(path) if private else True


def _check_secret_repo(secret_repo: str) -> None:
    """The protected private key may go only into an Actions secret of a
    private repo the KEY HOLDER controls: under his own account, or an org he
    administers. Anyone with write on a repo can read its secrets through a
    workflow, so an Aitherium repo would be escrow by the back door."""
    if not _REPO_RE.match(secret_repo or ""):
        raise RefusedError(f"--secret-repo must be OWNER/NAME, got {secret_repo!r}")
    owner = secret_repo.split("/", 1)[0]
    if owner.lower() in DENY_SECRET_OWNERS:
        raise RefusedError(f"{secret_repo}: never store your archive key in an "
                           f"{owner} repo -- its admins could read it (escrow)")
    try:
        login = gh.auth_login()
        mine = bool(login) and login.lower() == owner.lower()
        if not mine:
            mine = gh.org_role(owner) == "admin"
    except gh.GhError as exc:
        raise RefusedError(f"cannot tell who controls {owner} ({exc})") from exc
    if not mine:
        raise RefusedError(f"{secret_repo} is not under YOUR account ({login or '?'}) or an "
                           "org you administer: an Actions secret is readable by anyone "
                           "who can push a workflow there")
    guard_private_repo(secret_repo)


def keygen(private_out: Path, public_out: Path, *, passphrase: Optional[str],
           force: bool = False, github_secret: str = "", secret_repo: str = "") -> dict:
    """Make a recipient keypair on the KEY HOLDER's machine.

    The private half is written passphrase-protected (scrypt + AES-256-GCM)
    unless `passphrase` is None (explicit --no-passphrase). With
    `github_secret`, the PROTECTED form -- never the raw key -- also goes into
    an Actions secret of a private repo the holder controls (stdin, never
    argv). Returns ids and paths only."""
    if github_secret and not _SECRET_NAME_RE.match(github_secret):
        raise RefusedError(f"invalid GitHub secret name {github_secret!r}")
    if github_secret:
        if passphrase is None:
            raise RefusedError("--github-secret stores the passphrase-protected key only; "
                               "drop --no-passphrase")
        if not secret_repo:
            raise RefusedError("--github-secret needs --secret-repo OWNER/REPO")
        _check_secret_repo(secret_repo)
    if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
        raise RefusedError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    for path in (private_out, public_out):
        if Path(path).exists() and not force:
            raise RefusedError(f"{path} exists; refusing to overwrite a key (--force)")
    pub, priv = generate_keypair()
    kid = key_id(pub)
    if passphrase is None:
        stored_line, label = priv, "UNPROTECTED (no passphrase)"
    else:
        stored_line, label = encrypt_private_key(priv, passphrase), "passphrase-protected"
    del priv
    owner_only = _write_new(Path(private_out),
                            f"# awrtifact archive PRIVATE key {kid} -- {label}; never share\n"
                            f"{stored_line}\n", private=True, force=force)
    _write_new(Path(public_out), f"# awrtifact archive recipient {kid}\n{pub}\n",
               private=False, force=force)
    stored = ""
    if github_secret:
        gh.secret_set(secret_repo, github_secret, stored_line)
        stored = f"{secret_repo}:{github_secret}"
    del stored_line
    return {"key_id": kid, "public": str(public_out), "private": str(private_out),
            "protected": passphrase is not None, "owner_only": owner_only,
            "github_secret": stored}


# -- repo guard --------------------------------------------------------------

def guard_private_repo(repo: str) -> None:
    """Refuse unless GitHub itself says the repo is PRIVATE (an internal repo is
    readable across the whole enterprise). An unreadable answer is a refusal."""
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
        raise RefusedError(f"{repo} is PUBLIC or INTERNAL; sealed archives go to "
                           "private repos only")


# -- segment crypto ----------------------------------------------------------

def _aad(archive_id: str, index: int, final: bool) -> bytes:
    return (AAD_PREFIX + archive_id.encode("ascii") + b"|" + struct.pack(">I", index)
            + (b"\x01" if final else b"\x00"))


def _meta_key(dek: bytes) -> bytes:
    return hmac.new(dek, META_KEY_LABEL, hashlib.sha256).digest()


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
    """Seal `file` into release `tag` of the PRIVATE `repo`.

    Returns the public manifest plus what the SEALER must record to guard an
    eviction later (`archive_id`, `ct_sha256`, `plain_sha256`), the sealed-in
    `name`, and `peak_work_bytes` (the most ciphertext ever on local disk)."""
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

    display = safe_name(name or file.name)
    archive_id = os.urandom(16).hex()
    manifest_name = f"{archive_id}{MANIFEST_SUFFIX}"
    if not gh.release_exists(repo, tag):
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
        prefix = os.urandom(8)
        wrap = base64.b64encode(c.SealedBox(c.PublicKey(pub_raw)).encrypt(dek)).decode("ascii")
        aead = c.AESGCM(dek)
        meta_aead = c.AESGCM(_meta_key(dek))
        del dek

        writer = _PartWriter(work, archive_id, part_size,
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

        plain_sha = plain_hash.hexdigest()
        meta = {"archive_id": archive_id, "name": display, "plain_sha256": plain_sha,
                "plain_size": plain_size, "repo": repo.lower(), "tag": tag}
        nonce = os.urandom(12)
        meta_blob = meta_aead.encrypt(
            nonce, json.dumps(meta, sort_keys=True).encode("utf-8"),
            META_AAD_PREFIX + archive_id.encode("ascii"))
        manifest = {
            "format": FORMAT,
            "version": 2,
            "archive_id": archive_id,
            "cipher": "AES-256-GCM",
            "envelope": "AWSH1",
            "segment_size": segment_size,
            "plain_size": plain_size,
            "ct_size": writer.offset,
            "ct_sha256": writer.ct_hash.hexdigest(),
            "parts": writer.parts,
            "wraps": {"pubkey": {"scheme": WRAP_SCHEME, "recipient_key_id": kid,
                                 "blob": wrap}},
            "meta": {"cipher": "AES-256-GCM",
                     "nonce": base64.b64encode(nonce).decode("ascii"),
                     "blob": base64.b64encode(meta_blob).decode("ascii")},
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
        return {**manifest, "name": display, "plain_sha256": plain_sha, "repo": repo,
                "tag": tag, "manifest_asset": manifest_name,
                "peak_work_bytes": writer.peak_bytes}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- restore -----------------------------------------------------------------

def _validate_manifest(m: dict) -> None:
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise ArchiveError("not an awrtifact sealed-archive v2 manifest")
    if m.get("manifest_sha256") != _manifest_digest(m):
        raise ArchiveError("manifest digest mismatch (corrupt or edited)")
    for key in ("archive_id", "segment_size", "plain_size", "ct_size", "ct_sha256",
                "parts", "wraps", "meta"):
        if key not in m:
            raise ArchiveError(f"manifest missing {key}")
    if not _ARCHIVE_ID_RE.match(str(m["archive_id"])):
        raise ArchiveError("manifest archive_id is malformed")
    offset = 0
    for i, part in enumerate(m["parts"]):
        pname = str(part.get("name", ""))
        if part.get("offset") != offset or pname != f"{m['archive_id']}{PART_INFIX}{i}":
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


def list_manifests(repo: str, tag: str) -> list[str]:
    return sorted(a for a in gh.release_assets(repo, tag) if a.endswith(MANIFEST_SUFFIX))


def _load_manifest(repo: str, tag: str, manifest_name: str, work: Path) -> dict:
    mpath = work / manifest_name
    gh.download_asset(repo, tag, manifest_name, str(mpath))
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ArchiveError(f"{manifest_name} is not JSON") from exc
    finally:
        mpath.unlink(missing_ok=True)
    _validate_manifest(m)
    if manifest_name != f"{m['archive_id']}{MANIFEST_SUFFIX}":
        raise ArchiveError(f"{manifest_name} names archive {m['archive_id']} (renamed?)")
    return m


def _open_archive(c, m: dict, sk, mine: str, repo: str, tag: str):
    """(segment AEAD, sealed meta), or ArchiveError when this key is not the
    recipient, the meta does not authenticate, or the archive was sealed into a
    different repo/tag than the one it was found in."""
    wrap = (m["wraps"] or {}).get("pubkey") or {}
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
    meta_aead = c.AESGCM(_meta_key(dek))
    del dek
    sealed = m.get("meta") or {}
    try:
        meta = json.loads(meta_aead.decrypt(
            base64.b64decode(sealed.get("nonce", "")), base64.b64decode(sealed.get("blob", "")),
            META_AAD_PREFIX + str(m["archive_id"]).encode("ascii")))
    except (c.InvalidTag, ValueError, binascii.Error) as exc:
        raise ArchiveError("sealed metadata failed authentication") from exc
    if not isinstance(meta, dict) or meta.get("archive_id") != m["archive_id"]:
        raise ArchiveError("sealed metadata names a different archive")
    if meta.get("repo") != repo.lower() or meta.get("tag") != tag:
        raise ArchiveError(f"archive was sealed into {meta.get('repo')}@{meta.get('tag')}, "
                           f"found in {repo}@{tag} (copied or replayed)")
    if meta.get("plain_size") != m["plain_size"]:
        raise ArchiveError("sealed metadata disagrees with the manifest size")
    if not _SHA256_RE.match(str(meta.get("plain_sha256", ""))):
        raise ArchiveError("sealed metadata has no plaintext sha256")
    name = meta.get("name")
    if not isinstance(name, str) or not name or name != safe_name(name):
        raise ArchiveError("sealed metadata carries an unsafe file name")
    return aead, meta


def unseal_restore(repo: str, tag: str, out: Optional[Path], private_key_b64: str, *,
                   name: str = "", archive_id: str = "", expect_plain_sha256: str = "",
                   work_dir: Optional[Path] = None, verify_only: bool = False,
                   force: bool = False) -> dict:
    """Download, verify and decrypt a sealed archive with the recipient's private
    key. Fails closed: nothing appears at `out` unless every part sha256, every
    GCM tag, the sealed repo/tag binding and the plaintext sha256 check out.

    `archive_id` / `expect_plain_sha256` pin WHICH archive and WHICH bytes. The
    eviction guard passes both (recorded at seal time), because anyone holding
    the public key can seal some other archive into the release."""
    c = _deps()
    if not _REPO_RE.match(repo or ""):
        raise RefusedError(f"--repo must be OWNER/NAME, got {repo!r}")
    if archive_id and not _ARCHIVE_ID_RE.match(archive_id):
        raise RefusedError("--archive-id must be 32 lowercase hex characters")
    expect = (expect_plain_sha256 or "").strip().lower()
    if expect and not _SHA256_RE.match(expect):
        raise RefusedError("--expect-sha256 must be 64 hex characters")
    sk = c.PrivateKey(_decode_key(private_key_b64, "private"))
    mine = key_id(base64.b64encode(bytes(sk.public_key)).decode("ascii"))
    if not verify_only and out is None:
        raise RefusedError("--out is required unless --verify-only")

    work = _work_dir(work_dir, "awrtifact-unseal-")
    partial: Optional[Path] = None
    reader: Optional[_ChainReader] = None
    try:
        names = list_manifests(repo, tag)
        if archive_id:
            names = [n for n in names if n == f"{archive_id}{MANIFEST_SUFFIX}"]
            if not names:
                raise ArchiveError(f"{repo}@{tag} holds no archive {archive_id}")
        if not names:
            raise ArchiveError(f"{repo}@{tag} holds no {MANIFEST_SUFFIX} (missing, or an "
                               "interrupted seal)")
        want_name = safe_name(name) if name else ""
        if len(names) > 1 and not (want_name or expect):
            raise RefusedError(f"{repo}@{tag} holds several archives; pass --archive-id, "
                               f"--name or --expect-sha256 "
                               f"({', '.join(n[:-len(MANIFEST_SUFFIX)] for n in names)})")
        matches = []
        for mname in names:
            m = _load_manifest(repo, tag, mname, work)
            if len(names) == 1:
                aead, meta = _open_archive(c, m, sk, mine, repo, tag)
            else:
                try:
                    aead, meta = _open_archive(c, m, sk, mine, repo, tag)
                except ArchiveError:
                    continue  # not ours, or not bound here: never a candidate
                if (want_name and meta["name"] != want_name) or \
                        (expect and meta["plain_sha256"] != expect):
                    continue
            if want_name and meta["name"] != want_name:
                raise ArchiveError(f"the archive here is not named {want_name}")
            matches.append((m, aead, meta))
        if len(matches) != 1:
            raise ArchiveError(f"{len(matches)} archives in {repo}@{tag} match this key"
                               + (f" and name {want_name}" if want_name else "")
                               + (" and the expected sha256" if expect else ""))
        m, aead, meta = matches[0]
        if expect and meta["plain_sha256"] != expect:
            raise ArchiveError("archive holds a different file than expected "
                               "(--expect-sha256 mismatch)")

        target: Optional[Path] = None
        if not verify_only:
            target = Path(out)
            if target.is_dir():
                root = target.resolve()
                target = target / meta["name"]
                if target.resolve().parent != root:
                    raise ArchiveError("restored name escapes --out")
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
        sink = None
        if partial is not None:
            # A stale .partial from a crashed run is removed; the new one is
            # created O_EXCL and owner-only, so decrypted bytes are never
            # readable by another local user while the restore runs.
            sink = os.fdopen(_create_private(partial, replace=True), "wb")
            if os.name == "nt":
                _owner_only(partial)
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
        if written != plain_size or plain_hash.hexdigest() != meta["plain_sha256"]:
            raise ArchiveError("plaintext sha256 mismatch after decrypt")
        if partial and target:
            os.replace(str(partial), str(target))
            partial = None
        return {"status": "verified" if verify_only else "restored",
                "path": str(target) if target else "", "bytes": written,
                "name": meta["name"], "archive_id": m["archive_id"],
                "plain_sha256": meta["plain_sha256"], "parts": len(m["parts"]),
                "recipient_key_id": mine, "peak_work_bytes": reader.peak_bytes}
    finally:
        if reader is not None:
            reader.close()
        if partial is not None:
            partial.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
