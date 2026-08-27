"""`awrtifact plan` — the missing-part matrix.

Same question the mirror workflow's plan job asks of the release: which
`.partN` slices are NOT already present? Re-running after any failure uploads
only what is missing (the resumable property that made the 20-runner fan-out
safe).

A part whose name exists but whose SIZE differs is treated as MISSING, not
present — a half-uploaded or stale part must never look done. (The
mirror-hf-to-release.yml plan job compares names only; the size check here is
the deliberate improvement, and `upload --clobber` makes the re-upload safe.)
"""

from __future__ import annotations

from . import gh


def plan_parts(
    manifest: dict, repo: str, release: str
) -> dict:
    """Return {"present": [...], "missing": [...], "stale": [...]} indexes."""
    assets = gh.asset_sizes(repo, release)
    present: list[int] = []
    missing: list[int] = []
    stale: list[int] = []
    for idx, part in enumerate(manifest["parts"]):
        size = assets.get(part["name"])
        if size is None:
            missing.append(idx)
        elif size != part["size"]:
            stale.append(idx)
        else:
            present.append(idx)
    return {
        "present": present,
        "missing": missing,
        "stale": stale,
        "need_upload": missing + stale,
    }
