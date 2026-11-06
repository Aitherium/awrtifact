"""Promote a private release to a public repo — only when it is sealed.

The shape this serves: keep a PRIVATE artifact repo you publish to freely, and
copy a release to a PUBLIC repo only when you decide to share it. The public
copy is what ``artifact.aitherium.com`` (or any reader) serves.

Promotion is a one-way door, so it is fail-closed at every step:

1. The source must be private and the destination public. Visibility that
   cannot be read is refused. A private destination is refused too — that is a
   copy, not a promotion, and the command should not pretend otherwise.
2. The release must carry an ``awseal`` seal, and the seal must verify:
   the signature, every asset's digest, and no asset missing or extra.
3. The signing key must be the one you named (``--expect-key``). A valid seal
   by a stranger's key is still refused — a signature says who, and "who" is
   the question promotion answers.
4. The public release must not already exist. Promotion never overwrites
   something people may already be reading.

Nothing is uploaded until every check above has passed. A dry run performs the
same checks and uploads nothing.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import gh

#: The seal awseal writes, uploaded beside the release's assets.
SEAL_ASSET = "awseal.json"


class PromoteError(RuntimeError):
    """Promotion refused. The message says which check failed."""


@dataclass
class PromotePlan:
    source: str
    dest: str
    release: str
    assets: list[str] = field(default_factory=list)
    signer: str = ""
    uploaded: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "dest": self.dest,
            "release": self.release,
            "assets": self.assets,
            "signer": self.signer,
            "uploaded": self.uploaded,
            "dry_run": self.dry_run,
            "url": f"https://github.com/{self.dest}/releases/tag/{self.release}",
        }


def _awseal():
    try:
        from awseal import seal as aw  # noqa: PLC0415 — optional dependency
    except ImportError as exc:
        raise PromoteError(
            "promotion requires awseal: pip install awseal (or awrtifact[seal])"
        ) from exc
    return aw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_visibility(source: str, dest: str, *,
                     repo_private: Callable[[str], Optional[bool]] = gh.repo_private) -> None:
    """Source private, destination public, both readable — or refuse."""
    src = repo_private(source)
    if src is None:
        raise PromoteError(f"cannot read the visibility of {source}; refusing")
    if src is False:
        raise PromoteError(f"{source} is public; promotion copies FROM a private repo")
    dst = repo_private(dest)
    if dst is None:
        raise PromoteError(f"cannot read the visibility of {dest}; refusing")
    if dst is True:
        raise PromoteError(f"{dest} is private; promotion publishes TO a public repo")


def verify_release(work: Path, *, expect_key: str) -> dict:
    """Verify the downloaded release in ``work`` against its seal.

    ``work`` holds every asset plus ``awseal.json``. awseal checks the
    signature and the digest of every file, and reports any file the seal does
    not cover — so an extra asset slipped into the private release fails too.
    """
    aw = _awseal()
    if not (work / SEAL_ASSET).is_file():
        raise PromoteError(
            f"the release has no {SEAL_ASSET}; seal it before promoting "
            "(awseal sign <dir>, then upload the seal beside the assets)"
        )
    try:
        verdict = aw.verify(work, expect_key=expect_key)
    except Exception as exc:  # noqa: BLE001 — a verifier crash is a refusal
        raise PromoteError(f"seal could not be verified: {type(exc).__name__}: {exc}") from exc
    if not verdict.get("ok", verdict.get("valid", False)):
        diff = verdict.get("diff") or {}
        detail = json.dumps(diff, sort_keys=True)[:400] if diff else verdict.get("error", "")
        raise PromoteError(f"seal does not verify: {detail or 'signature or content mismatch'}")
    if not verdict.get("key_trusted"):
        raise PromoteError("the seal is valid but was not made by the key you named; refusing")
    return verdict


def promote(source: str, dest: str, release: str, *, expect_key: str,
            dry_run: bool = False, title: str = "", notes: str = "",
            api=gh) -> PromotePlan:
    """Copy ``release`` from private ``source`` to public ``dest``, sealed.

    ``api`` is the GitHub surface (``gh`` by default); tests pass a fake.
    """
    if not expect_key:
        raise PromoteError("--expect-key is required: promotion must name who signed it")

    check_visibility(source, dest, repo_private=api.repo_private)

    if not api.release_exists(source, release):
        raise PromoteError(f"{source} has no release {release!r}")
    if api.release_exists(dest, release):
        raise PromoteError(
            f"{dest} already has release {release!r}; promotion never overwrites a public release"
        )

    names = sorted(api.release_assets(source, release))
    if not names:
        raise PromoteError(f"{source} release {release!r} has no assets")
    if SEAL_ASSET not in names:
        raise PromoteError(
            f"{source} release {release!r} carries no {SEAL_ASSET}; seal it before promoting"
        )

    plan = PromotePlan(source=source, dest=dest, release=release,
                       assets=names, signer=expect_key, dry_run=dry_run)

    with tempfile.TemporaryDirectory(prefix="awrtifact-promote-") as td:
        work = Path(td)
        for name in names:
            if Path(name).name != name or name in ("", ".", ".."):
                raise PromoteError(f"refusing an asset name that is a path: {name!r}")
            api.download_asset(source, release, name, str(work / name))

        verify_release(work, expect_key=expect_key)

        if dry_run:
            return plan

        api.create_release(
            dest, release, title or release,
            notes or f"Promoted from a private release; sealed by {expect_key[:16]}.")
        for name in names:
            path = work / name
            before = _sha256(path)
            api.upload(dest, release, str(path))
            if _sha256(path) != before:  # pragma: no cover - the upload must not mutate
                raise PromoteError(f"{name} changed during upload")
            plan.uploaded.append(name)

    return plan
