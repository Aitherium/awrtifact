"""`awrtifact verify` — prove local parts match the manifest byte-for-byte.

Three checks, in ascending order of cost:

1. SIZE per part — cheap; catches truncation before any hashing.
2. SHA256 per part — the per-part digests split() recorded; catches a
   corrupted or replaced slice without re-reading the other twenty.
3. WHOLE-FILE digest by concatenating the parts in order — the manifest's
   `sha256` is the same digest the client will compute, so this proves the
   parts STITCH back into exactly the original artifact (the load-time
   "corrupt model" failure is this check, run a day early).

The report carries ok/fail per check so a caller can distinguish "part 3 is
corrupt, re-upload it" from "parts disagree with the manifest, re-split".
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import hashes


def _digest(path: Path) -> str:
    return hashes.sha256_file(path)


def verify_manifest(manifest: dict, dir: Path) -> dict:
    """Verify parts under `dir` against `manifest`; returns a report dict.

    Report shape: {"ok": bool, "checks": {per-part: [...]}, "whole": {...}}
    """
    name = manifest["name"]
    dir = Path(dir)
    checks: list[dict] = []
    for part in manifest["parts"]:
        p = dir / part["name"]
        result = {"part": part["name"], "ok": True, "errors": []}
        if not p.is_file():
            result["ok"] = False
            result["errors"].append("missing")
            checks.append(result)
            continue
        size = p.stat().st_size
        if size != part["size"]:
            result["ok"] = False
            result["errors"].append(f"size {size} != {part['size']}")
        if result["ok"]:
            got = _digest(p)
            if got != part["sha256"]:
                result["ok"] = False
                result["errors"].append(f"sha256 {got[:16]}… != {part['sha256'][:16]}…")
        checks.append(result)

    whole = {"ok": True, "sha256": "", "errors": []}
    if all(c["ok"] for c in checks):
        h = hashlib.sha256()
        for part in manifest["parts"]:
            with open(dir / part["name"], "rb") as f:
                while True:
                    chunk = f.read(hashes.BUFFER)
                    if not chunk:
                        break
                    h.update(chunk)
        whole["sha256"] = h.hexdigest()
        if whole["sha256"] != manifest["sha256"]:
            whole["ok"] = False
            whole["errors"].append("concatenated parts do not match whole-file digest")

    ok = all(c["ok"] for c in checks) and whole["ok"]
    return {"name": name, "ok": ok, "checks": checks, "whole": whole}
