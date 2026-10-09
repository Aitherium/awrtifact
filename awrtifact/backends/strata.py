"""A split artifact's parts in an AitherStrata pool instead of a GitHub release.

The manifest is the same byte contract `split` writes (`manifest.py`); only the
place the parts live changes. Each part is stored under its OWN sha256:

    <tier>/__t__/<tenant>/awrtifact/parts/<ab>/<sha256>
    <tier>/__t__/<tenant>/awrtifact/manifests/<name>.manifest.json

Content addressing is the dedupe. A new version of an artifact whose parts are
mostly unchanged sends only the changed parts: an unchanged part has the same
digest, so it is already present -- the pool equivalent of the release lane's
`link_previous`, which serves an unchanged file from the release that already
holds it. (The release lane and its `link_previous` are untouched; this is an
additional backend, and GitHub remains the default.)

What has to be PROVEN before a call returns, in both directions:

- upload: each local part's size AND sha256 are checked against the manifest
  before it is sent, and the target's `upload_verified` reads the object's size
  (and digest, when the service reports one) back after the write.
- presence: a part counts as present only when the stat answers the manifest's
  size and, when the service reports a digest, the manifest's digest. An outage
  RAISES -- it never reads as "absent" (re-send everything) or "present" (skip
  bytes that were never stored).
- fetch: every part's bytes are hashed against the manifest BEFORE they are
  written, the whole file's size and sha256 are checked at the end, and nothing
  appears at the destination name unless all of it held.

The transport is any object with `upload_verified(rel, data, sha256)`,
`stat_or_none(rel)` and `get(rel)` -- `awstorage.StrataTarget` is one
(`pip install awrtifact[strata]`). This module never opens a socket itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .. import manifest as manifest_mod

NS_MARK = "__t__"
#: One part is one object in one HTTP request; Strata's write API carries it
#: base64 in JSON. A bigger part is refused up front -- re-split with a smaller
#: `--part-size` -- rather than half-sent.
DEFAULT_MAX_PART_BYTES = 256 * 1024 * 1024
TENANT_ENV = "AWSTORAGE_STRATA_TENANT"
_HEX = frozenset("0123456789abcdef")
CHUNK = 8 * 1024 * 1024


class StrataBackendError(RuntimeError):
    """The pool backend could not do what was asked. Raised, never swallowed."""


def safe_tenant(tenant: Optional[str]) -> str:
    t = str(tenant or "").strip()
    if (not t or len(t) > 128 or t.startswith(".")
            or not all(c.isalnum() or c in "-_." for c in t)):
        raise StrataBackendError(f"tenant {tenant!r} is not a plain tenant id; every pooled "
                                 "part lives under exactly one tenant")
    return t


def _is_sha(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 64 and set(s) <= _HEX


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class StrataPartStore:
    """One tenant's awrtifact parts in one Strata tier."""

    def __init__(self, target: Any, tenant: str, *,
                 max_part_bytes: int = DEFAULT_MAX_PART_BYTES) -> None:
        self.target = target
        self.tenant = safe_tenant(tenant)
        self.max_part_bytes = int(max_part_bytes)
        self.prefix = f"{NS_MARK}/{self.tenant}/awrtifact"

    # -- keys -----------------------------------------------------------------------

    def part_rel(self, sha256: str) -> str:
        if not _is_sha(sha256):
            raise StrataBackendError(f"not a sha256 part digest: {sha256!r}")
        return f"{self.prefix}/parts/{sha256[:2]}/{sha256}"

    def manifest_rel(self, name: str) -> str:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise StrataBackendError(f"manifest name must be a bare filename: {name!r}")
        return f"{self.prefix}/manifests/{name}.manifest.json"

    # -- presence ---------------------------------------------------------------------

    def _present(self, part: Mapping[str, Any]) -> bool:
        try:
            st = self.target.stat_or_none(self.part_rel(part["sha256"]))
        except StrataBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - an outage is not an answer
            raise StrataBackendError(f"cannot ask the pool about {part['name']}: {exc}") from exc
        if st is None:
            return False
        size = st.get("size", st.get("size_bytes"))
        if size is None or int(size) != int(part["size"]):
            return False
        remote = st.get("hash") or st.get("content_hash") or st.get("sha256")
        return not remote or str(remote).lower() == part["sha256"]

    def plan(self, manifest: dict) -> Dict[str, list]:
        """{"present": [idx], "need_upload": [idx]} against the pool."""
        m = manifest_mod.validate(manifest)
        present, need = [], []
        for idx, part in enumerate(m["parts"]):
            (present if self._present(part) else need).append(idx)
        return {"present": present, "need_upload": need}

    # -- upload -----------------------------------------------------------------------

    def upload(self, manifest: dict, dir: Path) -> Dict[str, Any]:
        """Send the parts the pool lacks, then the manifest. Same report shape as
        `upload.upload_manifest`: {"uploaded", "skipped_present", "manifest", "failed"}."""
        m = manifest_mod.validate(manifest)
        too_big = [p["name"] for p in m["parts"] if p["size"] > self.max_part_bytes]
        if too_big:
            raise StrataBackendError(
                f"{len(too_big)} part(s) exceed {self.max_part_bytes} bytes for the pool "
                f"({too_big[0]}...); re-split with a smaller --part-size")
        dir = Path(dir)
        planned = self.plan(m)
        uploaded: list = []
        failed: list = []
        for idx in planned["need_upload"]:
            part = m["parts"][idx]
            local = dir / part["name"]
            try:
                if not local.is_file():
                    raise StrataBackendError(f"missing local part: {local}")
                data = local.read_bytes()
                if len(data) != part["size"]:
                    raise StrataBackendError(
                        f"local part {part['name']} is {len(data)} bytes, manifest says "
                        f"{part['size']}")
                if hashlib.sha256(data).hexdigest() != part["sha256"]:
                    raise StrataBackendError(
                        f"local part {part['name']} does not hash to the manifest's sha256")
                self.target.upload_verified(self.part_rel(part["sha256"]), data,
                                            part["sha256"],
                                            {"awrtifact": m["name"], "part": idx})
                uploaded.append(part["name"])
            except Exception as exc:  # noqa: BLE001 - reported per part, like the release lane
                failed.append(f"{part['name']}: {exc}")
        manifest_state = "skipped"
        if not failed:
            raw = (json.dumps(m, indent=2, sort_keys=True) + "\n").encode("utf-8")
            try:
                self.target.upload_verified(self.manifest_rel(m["name"]), raw,
                                            hashlib.sha256(raw).hexdigest(),
                                            {"awrtifact": m["name"], "kind": "manifest"})
                manifest_state = "uploaded"
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{m['name']}.manifest.json: {exc}")
        return {
            "uploaded": uploaded,
            "skipped_present": [m["parts"][i]["name"] for i in planned["present"]],
            "manifest": manifest_state,
            "failed": failed,
        }

    # -- fetch ------------------------------------------------------------------------

    def load_manifest(self, name: str) -> dict:
        try:
            raw = self.target.get(self.manifest_rel(name))
        except Exception as exc:  # noqa: BLE001
            raise StrataBackendError(f"no manifest for {name!r} in the pool: {exc}") from exc
        try:
            data = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StrataBackendError(f"manifest for {name!r} is not JSON") from exc
        try:
            return manifest_mod.validate(data)
        except ValueError as exc:
            raise StrataBackendError(f"manifest for {name!r} is invalid: {exc}") from exc

    def fetch(self, manifest: dict, dest_dir: Path, *,
              expected_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Stitch the artifact back from its parts into `dest_dir/<name>`.

        `expected_sha256` pins the whole-file digest from somewhere other than the
        pool (a lockfile, a spec): the manifest came from the same store as the
        parts, so it alone cannot catch a store that rewrote both.
        """
        m = manifest_mod.validate(manifest)
        if expected_sha256 is not None and expected_sha256.lower() != m["sha256"]:
            raise StrataBackendError(
                f"{m['name']}: the pool's manifest names sha256 {m['sha256'][:16]}..., "
                f"expected {expected_sha256.lower()[:16]}...")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / m["name"]
        tmp = dest_dir / f".{m['name']}.awrtifact-partial"
        whole = hashlib.sha256()
        written = 0
        try:
            with open(tmp, "wb") as out:
                for part in m["parts"]:
                    try:
                        data = bytes(self.target.get(self.part_rel(part["sha256"])))
                    except Exception as exc:  # noqa: BLE001
                        raise StrataBackendError(
                            f"{part['name']}: could not read it from the pool: {exc}") from exc
                    if len(data) != part["size"] or \
                            hashlib.sha256(data).hexdigest() != part["sha256"]:
                        raise StrataBackendError(
                            f"{part['name']}: the pool returned bytes that do not match "
                            "the manifest (size or sha256); nothing was written")
                    out.write(data)
                    whole.update(data)
                    written += len(data)
            if written != m["total"] or whole.hexdigest() != m["sha256"]:
                raise StrataBackendError(
                    f"{m['name']}: stitched {written} bytes whose sha256 is not the "
                    "manifest's; nothing was written")
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"path": str(dest), "bytes": written, "sha256": m["sha256"],
                "status": "fetched", "parts": len(m["parts"])}


def from_env(tenant: Optional[str] = None, tier: str = "warm", *,
             env: Optional[Mapping[str, str]] = None, **target_kw: Any) -> StrataPartStore:
    """A part store over `awstorage.StrataTarget` (its env vars: AWSTORAGE_STRATA_BEARER
    or AWSTORAGE_STRATA_KEY, AITHERSTRATA_URL, AITHER_CA_BUNDLE). The tenant is
    required -- argument or AWSTORAGE_STRATA_TENANT. No credential = refused."""
    e = os.environ if env is None else env
    try:
        from awstorage.strata import StrataTarget  # noqa: PLC0415 -- optional extra
    except ImportError as exc:
        raise StrataBackendError("the strata backend needs awstorage: "
                                 "pip install 'awrtifact[strata]'") from exc
    target = StrataTarget(tier, env=e, **target_kw)
    if not target.key and not target.bearer:
        raise StrataBackendError("neither AWSTORAGE_STRATA_BEARER nor AWSTORAGE_STRATA_KEY "
                                 "is set; the pool needs a credential")
    return StrataPartStore(target, tenant if tenant is not None else e.get(TENANT_ENV, ""))
