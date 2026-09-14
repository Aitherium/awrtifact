"""Optional awseal integration — sign the mirror set so a stranger can verify
WHO published it, not merely that the bytes match the manifest.

The manifest already makes the bytes checkable (sizes + origin sha256). What
it cannot say is that the manifest itself came from the publisher you think:
anyone with write access to the release can clobber `<set>.mirrorset.json`.
`awseal` closes that: the manifest is signed with an Ed25519 key, the seal is
uploaded beside it as `<set>.awseal.json`, and a fetch that carries
`--expect-key <hex>` refuses a set whose seal does not verify against THAT
key. Without the key a fetch still reports the seal's verdict (trusted or not)
— it never silently ignores a seal that is present.

Guarded import: awseal is an optional extra (`pip install awrtifact[seal]`).
Asking to seal without it is a hard error, not a warning — a mirror the
operator believes is signed and is not is worse than an unsigned one.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from . import mirrorset

SEAL_SUFFIX = ".awseal.json"


class SealUnavailableError(RuntimeError):
    """awseal is not installed — pip install awseal (or awrtifact[seal])."""


def _awseal():
    try:
        from awseal import seal as _seal  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the unavailable test
        raise SealUnavailableError("awseal is not installed: pip install awseal") from exc
    return _seal


def check_key(key_path: Optional[Path] = None) -> str:
    """Load the signing key NOW and return its public hex -- called before the
    first GitHub write, so a missing key fails before a release exists with
    no seal beside it. awseal's own error text is kept (it explains why
    there is no default key)."""
    aw = _awseal()
    from awseal import keys as _keys  # noqa: PLC0415
    from awseal.digest import SealError  # noqa: PLC0415
    try:
        return _keys.public_key_hex(path=key_path)
    except SealError as exc:
        raise ValueError(f"awseal: {exc}") from exc
    finally:
        del aw


def seal_asset(data: dict) -> str:
    return f"{data['name']}{SEAL_SUFFIX}"


def _stage(data: dict, root: Path) -> Path:
    """The signed tree is exactly one file: the manifest, under its asset name."""
    path = root / mirrorset.manifest_asset(data)
    mirrorset.write(data, path)
    return path


def sign_set(data: dict, work: Path, *, key_path: Optional[Path] = None) -> Path:
    """Write `<set>.awseal.json` into `work` covering the manifest. Returns it."""
    aw = _awseal()
    work = Path(work)
    with tempfile.TemporaryDirectory(prefix="awrtifact-seal-") as td:
        root = Path(td)
        _stage(data, root)
        sealed = aw.sign(root, key_path=key_path, subject=data["name"],
                         meta={"kind": "awrtifact-mirrorset",
                               "repo_id": data["source"]["repo_id"],
                               "commit": data["source"]["commit"],
                               "release": data["target"]["release"]})
        out = work / seal_asset(data)
        out.write_text(json.dumps(sealed.to_dict(), indent=2, sort_keys=True),
                       encoding="utf-8")
    return out


def verify_set(data: dict, seal_json: bytes | str, *,
               expect_key: Optional[str] = None) -> dict:
    """Verify a seal document against the manifest as fetched.

    Returns awseal's verdict dict plus `ok` (signature + digest valid) and,
    when `expect_key` is given, `key_trusted`. A malformed seal is `ok: False`
    with the reason — never an exception a caller could swallow into "no
    seal".
    """
    aw = _awseal()
    raw = seal_json.decode("utf-8") if isinstance(seal_json, bytes) else seal_json
    with tempfile.TemporaryDirectory(prefix="awrtifact-verify-") as td:
        root = Path(td)
        _stage(data, root)
        (root / aw.SEAL_NAME).write_text(raw, encoding="utf-8")
        try:
            result = aw.verify(root, expect_key=expect_key)
        except Exception as exc:  # noqa: BLE001 — the verdict IS the report
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "key_trusted": False if expect_key else None}
    ok = bool(result.get("ok", result.get("valid", False)))
    if expect_key is not None:
        ok = ok and bool(result.get("key_trusted"))
    return {**result, "ok": ok}
