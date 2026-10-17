"""`awrtifact companion-backup` / `companion-restore` -- a companion never forgets.

A companion's memory is a folder of append-only JSONL (`utterances.jsonl`,
`vectors.jsonl`) plus its card. This module tars that folder, encrypts it with
the GobboNet envelope (`envelope.py`, the same bytes the browser gate page
decrypts), splits it into `.partN` assets and publishes each run as a NEW
tagged release `companion-<persona_id>-<UTC stamp>` in a PRIVATE repo -- so
history accumulates and a wiped machine restores from GitHub.

Rules that hold on every path:

- the passphrase is a parameter only -- never argv, never stored, never logged;
- a PUBLIC repo is refused unless the caller opts in (`allow_public`): the
  bytes are ciphertext, but release tags and asset names carry persona ids;
- visibility that cannot be read is refused too (fail closed);
- restore never rewrites an existing row: backup rows the local store lacks
  are APPENDED with fresh `seq` numbers (their vectors follow the renumbering);
  a row whose `event_id` matches a local row but whose text differs is kept
  alongside it -- both survive a conflict.

The recall root is resolved the way the companion recall store resolves it
(explicit dir env, data-dir env, `/data`, then `~/.aither/character-recall`);
callers inside a platform pass `recall_root` explicitly instead.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import envelope, gh

UTTERANCES_FILE = "utterances.jsonl"
VECTORS_FILE = "vectors.jsonl"
CARD_FILES = ("card_v2.json", "card.json", "character.json", "persona.json")
AWM_EXPORT_FILE = "awm-export.json"
BUNDLE_META = "companion-bundle.json"
TAG_PREFIX = "companion-"
DEFAULT_PASSPHRASE_ENV = "AWRTIFACT_COMPANION_PASSPHRASE"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

RECALL_DIR_ENV = "AITHER_CHARACTER_RECALL_DIR"
DATA_DIR_ENV = "AITHER_DATA_DIR"

Log = Callable[[str], None]


class CompanionBackupError(ValueError):
    """A refusal or a failed check. Never carries the passphrase."""


class PublicRepoRefusedError(CompanionBackupError):
    """The target repo is public and the caller did not pass allow_public."""


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def validate_persona_id(persona_id: str) -> str:
    pid = (persona_id or "").strip() if isinstance(persona_id, str) else ""
    if not PERSONA_ID_RE.match(pid):
        raise CompanionBackupError(
            f"persona_id {persona_id!r} is not a persona id (letters, digits, dot, dash, "
            "underscore; starts alphanumeric; 1-128 chars)")
    return pid


def resolve_recall_root(env: Optional[Mapping[str, str]] = None,
                        data_volume: str = "/data",
                        home: Optional[Path] = None) -> Path:
    """The companion recall root, same precedence as the recall store."""
    source = os.environ if env is None else env
    explicit = (source.get(RECALL_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_dir = (source.get(DATA_DIR_ENV) or "").strip()
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "avatars" / "recall"
    vol = Path(data_volume)
    if vol.is_dir() and os.access(str(vol), os.W_OK):
        return vol.resolve() / "avatars" / "recall"
    base = Path(home) if home is not None else Path.home()
    return base.resolve() / ".aither" / "character-recall"


def utc_stamp(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))


# ---------------------------------------------------------------------------
# passphrase
# ---------------------------------------------------------------------------

def passphrase_from_env_or_prompt(env_var: str = DEFAULT_PASSPHRASE_ENV,
                                  confirm: bool = False,
                                  prompt: Optional[Callable[[str], str]] = None) -> str:
    """The passphrase from `env_var`, else an interactive no-echo prompt.

    Never from argv. A non-interactive run with the variable unset is a refusal.
    """
    if not ENV_NAME_RE.match(env_var or ""):
        # Never echo it: a value that is not an env-var NAME may be the secret.
        raise CompanionBackupError("--passphrase-env must name an environment variable")
    value = os.environ.get(env_var, "")
    if value:
        return value
    if prompt is None:
        if not sys.stdin or not sys.stdin.isatty():
            raise CompanionBackupError(
                f"no passphrase: set ${env_var} or run interactively (it is never read from argv)")
        import getpass  # noqa: PLC0415

        prompt = getpass.getpass
    first = prompt("Backup passphrase: ")
    if confirm and prompt("Repeat passphrase: ") != first:
        raise CompanionBackupError("passphrases did not match")
    return first


# ---------------------------------------------------------------------------
# repo gate
# ---------------------------------------------------------------------------

def ensure_private_repo(repo: str, allow_public: bool = False, provision: bool = True,
                        log: Log = _stderr) -> bool:
    """Return the repo's privacy after making sure it exists; refuse public.

    A missing repo is provisioned PRIVATE (`provision-repo`), whatever
    `allow_public` says.
    """
    if "/" not in repo:
        raise CompanionBackupError("repo must be OWNER/NAME")
    private = gh.repo_private(repo)
    if private is None:
        if not provision:
            raise CompanionBackupError(f"repo {repo} does not exist")
        from . import provision as provision_mod  # noqa: PLC0415

        log(f"repo {repo} not found -- provisioning it PRIVATE")
        provision_mod.provision_repo(repo, public=False, pages=False)
        private = gh.repo_private(repo)
        if private is None:
            raise CompanionBackupError(f"repo {repo} still missing after provisioning")
    if not private and not allow_public:
        raise PublicRepoRefusedError(
            f"refusing to back up to PUBLIC repo {repo}: release tags and asset names "
            "carry the persona id even though the bytes are encrypted "
            "(pass --allow-public to accept that)")
    return private


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------

def awm_export(scope: Optional[str]) -> tuple[Optional[bytes], str]:
    """(json bytes, note) for an awm scope export, or (None, why it was skipped)."""
    if not scope:
        return None, "awm: no scope requested -- skipped"
    try:
        import awm  # noqa: PLC0415
    except ImportError:
        return None, "awm: not installed here -- skipped"
    exporter = getattr(awm, "export_scope", None)
    if not callable(exporter):
        version = getattr(awm, "__version__", "?")
        return None, (f"awm {version}: exposes no per-scope export -- skipped "
                      "(its memories stay in awm's own `awm backup`)")
    data = exporter(scope)
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), \
        f"awm: exported scope {scope}"


def build_bundle(persona_id: str, recall_root: Path, out_path: Path,
                 extra_files: Optional[Mapping[str, Path]] = None,
                 awm_scope: Optional[str] = None,
                 log: Log = _stderr) -> dict:
    """Write `out_path` (tar.gz) holding the persona folder; return its meta.

    Members live under `<persona_id>/`. `extra_files` maps an archive basename
    (e.g. `card_v2.json`) to a local file -- a card kept elsewhere.
    """
    pid = validate_persona_id(persona_id)
    pdir = Path(recall_root) / pid
    members: dict[str, Path | bytes] = {}
    for name in (UTTERANCES_FILE, VECTORS_FILE, *CARD_FILES):
        p = pdir / name
        if p.is_file():
            members[name] = p
    for name, path in (extra_files or {}).items():
        envelope._check_asset_name(name)
        if Path(path).is_file():
            members[name] = Path(path)
        else:
            log(f"note: {name} source {path} not found -- skipped")
    data, note = awm_export(awm_scope)
    log(note)
    if data is not None:
        members[AWM_EXPORT_FILE] = data
    if UTTERANCES_FILE not in members:
        raise CompanionBackupError(
            f"persona {pid} has no {UTTERANCES_FILE} under {pdir} -- nothing to back up")
    meta = {"persona_id": pid, "created": utc_stamp(), "files": sorted(members),
            "rows": _count_lines(members.get(UTTERANCES_FILE))}
    with tarfile.open(out_path, "w:gz") as tar:
        blob = json.dumps(meta, indent=2).encode("utf-8")
        _add_bytes(tar, f"{pid}/{BUNDLE_META}", blob)
        for name in sorted(members):
            src = members[name]
            _add_bytes(tar, f"{pid}/{name}",
                       src if isinstance(src, bytes) else Path(src).read_bytes())
    return meta


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _count_lines(src) -> int:
    if src is None:
        return 0
    data = src if isinstance(src, bytes) else Path(src).read_bytes()
    return sum(1 for ln in data.splitlines() if ln.strip())


def read_bundle(tar_path: Path, persona_id: str) -> dict[str, bytes]:
    """Members of a bundle as {basename: bytes}; refuses anything but plain
    files directly under `<persona_id>/` (no links, no traversal)."""
    pid = validate_persona_id(persona_id)
    out: dict[str, bytes] = {}
    with tarfile.open(tar_path, "r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if (not m.isfile() or len(parts) != 2 or parts[0] != pid
                    or parts[1] in ("", ".", "..") or "\\" in m.name):
                raise CompanionBackupError(f"bundle member refused: {m.name!r}")
            fh = tar.extractfile(m)
            out[parts[1]] = fh.read() if fh else b""
    return out


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def _rows(data: bytes) -> list[dict]:
    rows = []
    for ln in data.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _identity(row: dict) -> tuple:
    if row.get("event_id"):
        return ("e", row.get("event_id"), row.get("text"))
    return ("r", row.get("ts"), row.get("role"), row.get("speaker"), row.get("text"))


def _append_rows(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.is_file() and path.stat().st_size:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                prefix = "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(prefix + "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows))


def merge_into(persona_dir: Path, members: Mapping[str, bytes], tag: str) -> dict:
    """Merge a bundle into a persona folder without clobbering anything local."""
    persona_dir = Path(persona_dir)
    utt_path = persona_dir / UTTERANCES_FILE
    vec_path = persona_dir / VECTORS_FILE
    local = _rows(utt_path.read_bytes()) if utt_path.is_file() else []
    local_ids = {_identity(r) for r in local}
    local_events = {r.get("event_id"): r.get("text") for r in local if r.get("event_id")}
    next_seq = max([r["seq"] for r in local if isinstance(r.get("seq"), int)] or [0]) + 1
    remap: dict[int, int] = {}
    new_rows: list[dict] = []
    present = conflicts = 0
    for row in _rows(members.get(UTTERANCES_FILE, b"")):
        ident = _identity(row)
        if ident in local_ids:
            present += 1
            continue
        if row.get("event_id") in local_events:
            conflicts += 1  # same event, different text: keep both
        old = row.get("seq")
        row = dict(row, seq=next_seq)
        if isinstance(old, int):
            remap[old] = next_seq
        next_seq += 1
        local_ids.add(ident)
        new_rows.append(row)
    new_vecs = [dict(v, seq=remap[v["seq"]])
                for v in _rows(members.get(VECTORS_FILE, b""))
                if isinstance(v.get("seq"), int) and v["seq"] in remap]
    _append_rows(utt_path, new_rows)
    _append_rows(vec_path, new_vecs)

    files: dict[str, str] = {}
    for name, data in members.items():
        if name in (UTTERANCES_FILE, VECTORS_FILE, BUNDLE_META):
            continue
        target = persona_dir / name
        if not target.exists():
            target.write_bytes(data)
            files[name] = "restored"
        elif target.read_bytes() == data:
            files[name] = "identical"
        else:
            side = persona_dir / f"{name}.restored-{tag}"
            side.write_bytes(data)
            files[name] = f"kept local; backup copy at {side.name}"
    return {"rows_added": len(new_rows), "rows_already_present": present,
            "conflicts_kept_both": conflicts, "vectors_added": len(new_vecs),
            "files": files}


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------

def _tag_for(pid: str, repo: str, now: Optional[float]) -> str:
    base = f"{TAG_PREFIX}{pid}-{utc_stamp(now)}"
    tag, n = base, 1
    while gh.release_exists(repo, tag):
        n += 1
        tag = f"{base}-{n}"
    return tag


def backup(persona_id: str, repo: str, passphrase: str, *,
           recall_root: Optional[Path] = None,
           extra_files: Optional[Mapping[str, Path]] = None,
           awm_scope: Optional[str] = None,
           allow_public: bool = False, provision: bool = True,
           part_size: int = envelope.PART_SIZE,
           now: Optional[float] = None, log: Log = _stderr) -> dict:
    """Bundle -> encrypt -> split -> new release. Returns {tag, repo, assets, meta}."""
    envelope.require_crypto()
    envelope.check_passphrase(passphrase)
    pid = validate_persona_id(persona_id)
    root = Path(recall_root) if recall_root else resolve_recall_root()
    ensure_private_repo(repo, allow_public=allow_public, provision=provision, log=log)
    work = Path(tempfile.mkdtemp(prefix="awrtifact-companion-"))
    try:
        bundle_name = f"companion-{pid}.tar.gz"
        meta = build_bundle(pid, root, work / bundle_name, extra_files, awm_scope, log)
        tag = _tag_for(pid, repo, now)
        out = work / "out"
        manifest = envelope.encrypt_files([(bundle_name, work / bundle_name)], passphrase,
                                          out, tag, part_size=part_size)
        gh.create_release(repo, tag, title=f"Companion backup {pid} {utc_stamp(now)}",
                          notes=f"Encrypted companion backup ({meta['rows']} memory rows). "
                                "Passphrase-protected (AES-GCM).")
        assets = [*envelope.part_names(manifest), f"{tag}{envelope.MANIFEST_SUFFIX}"]
        for name in assets:
            gh.upload(repo, tag, str(out / name))
        sizes = gh.asset_sizes(repo, tag)
        short = [a for a in assets if sizes.get(a) != (out / a).stat().st_size]
        if short:
            raise CompanionBackupError(f"release {tag} is missing or short on: {short}")
        log(f"backed up {pid}: {meta['rows']} rows, {len(meta['files'])} file(s) "
            f"-> {repo} release {tag}")
        return {"tag": tag, "repo": repo, "assets": assets, "meta": meta}
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: The per-user companions index awsettings syncs (its ``companions`` domain).
COMPANIONS_INDEX_ENV = "AWSETTINGS_COMPANIONS_FILE"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
TAG_RE = re.compile(r"^companion-[A-Za-z0-9._-]{1,200}$")


def companions_index_path() -> Path:
    override = (os.environ.get(COMPANIONS_INDEX_ENV) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".aither" / "companions.json"


def record_backup(persona_id: str, repo: str, tag: str,
                  index_path: Optional[Path] = None) -> Path:
    """Note WHERE a companion's backup lives in the companions index, so the next
    `awsettings push` carries it and a wiped machine can restore from it.

    Only `backup_repo` and `last_backup_tag` are written (plus a minimal record
    when the companion is not indexed yet); every other entry and field is kept.
    Never the passphrase. An unreadable index is refused, not overwritten.
    """
    pid = validate_persona_id(persona_id)
    if not REPO_RE.match(repo or "") or not TAG_RE.match(tag or ""):
        raise CompanionBackupError("repo or tag is not in the shape the index carries")
    path = Path(index_path) if index_path else companions_index_path()
    index: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            raise CompanionBackupError(f"{path} is unreadable; not overwriting it: {exc}") from exc
        if not isinstance(loaded, dict):
            raise CompanionBackupError(f"{path} is not a JSON object; not overwriting it")
        index = loaded
    companions = index.get("companions") if isinstance(index.get("companions"), dict) else {}
    prior = companions.get(pid) if isinstance(companions.get(pid), dict) else {}
    companions[pid] = {**prior, "persona_id": pid, "backup_repo": repo, "last_backup_tag": tag}
    index.update({"version": index.get("version", 1), "companions": companions})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


_STAMP_RE = re.compile(r"(\d{8}T\d{6}Z)(?:-(\d+))?")


def latest_tag(repo: str, persona_id: str) -> str:
    """Newest `companion-<pid>-<stamp>[-n]` release, by the stamp in the tag."""
    pid = validate_persona_id(persona_id)
    prefix = f"{TAG_PREFIX}{pid}-"
    ranked = []
    for t in gh.list_releases(repo, prefix):
        m = _STAMP_RE.fullmatch(t[len(prefix):])
        if m:
            ranked.append(((m.group(1), int(m.group(2) or 1)), t))
    if not ranked:
        raise CompanionBackupError(f"no {prefix}* releases in {repo}")
    return max(ranked)[1]


def restore(persona_id: str, repo: str, passphrase: str, *, tag: Optional[str] = None,
            recall_root: Optional[Path] = None, log: Log = _stderr) -> dict:
    """Download -> sha256 verify -> decrypt -> merge (append-only) into the recall dir."""
    envelope.require_crypto()
    pid = validate_persona_id(persona_id)
    root = Path(recall_root) if recall_root else resolve_recall_root()
    tag = tag or latest_tag(repo, pid)
    work = Path(tempfile.mkdtemp(prefix="awrtifact-companion-restore-"))
    try:
        man_name = f"{tag}{envelope.MANIFEST_SUFFIX}"
        gh.download_asset(repo, tag, man_name, str(work / man_name))
        manifest = envelope.validate_manifest(
            json.loads((work / man_name).read_text(encoding="utf-8")))
        for name in envelope.part_names(manifest):
            gh.download_asset(repo, tag, name, str(work / name))
        plain = work / "plain"
        files = envelope.decrypt_files(manifest, passphrase, work, plain)
        bundle = next((f for f in files if f.name.endswith(".tar.gz")), None)
        if bundle is None:
            raise CompanionBackupError(f"release {tag} carries no companion bundle")
        members = read_bundle(bundle, pid)
        report = merge_into(root / pid, members, tag)
        report.update({"tag": tag, "repo": repo, "persona_dir": str(root / pid)})
        log(f"restored {pid} from {tag}: +{report['rows_added']} rows "
            f"({report['rows_already_present']} already present, "
            f"{report['conflicts_kept_both']} conflicts kept both), "
            f"+{report['vectors_added']} vectors")
        for name, state in sorted(report["files"].items()):
            log(f"  {name}: {state}")
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
