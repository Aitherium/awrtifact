"""manifest.json — the byte-level contract of a split artifact.

Produced by `awrtifact split`, consumed by plan/upload/verify/fetch and by the
worker generator. Every field is load-bearing:

    name       served asset base name (the worker stitches behind this name)
    total      exact source size in bytes — the client checks this to detect
               truncation (the TP010 failure class: a 90 GB download that
               finishes and then reports a corrupt model)
    sha256     whole-file digest (streamed at split time)
    part_size  nominal slice size (must stay under GitHub's 2 GiB asset cap)
    parts      [{name, size, sha256}] — size AND digest per slice; the last
               part may be short; Σ parts[].size MUST equal total

A manifest that fails any invariant is refused, never silently repaired —
a repaired manifest would describe bytes that do not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

# GitHub's hard per-asset cap. Defaults stay at 1.9 GB for slack.
GITHUB_ASSET_CAP = 2 * 1024 * 1024 * 1024
DEFAULT_PART_SIZE = 1900000000

REQUIRED = {"name", "total", "sha256", "part_size", "parts"}


def _die(msg: str, code: int = 2) -> None:
    raise ValueError(msg)


def validate(data: dict) -> dict:
    """Validate a parsed manifest; returns it unchanged when valid."""
    missing = REQUIRED - set(data)
    if missing:
        _die(f"manifest missing fields: {sorted(missing)}")
    name: str = data["name"]
    if not isinstance(name, str) or "/" in name or "\\" in name:
        _die(f"manifest name must be a bare filename: {name!r}")
    total: int = data["total"]
    part_size: int = data["part_size"]
    if not isinstance(total, int) or total <= 0:
        _die(f"manifest total must be a positive int: {total!r}")
    if not isinstance(part_size, int) or part_size > GITHUB_ASSET_CAP:
        _die(f"manifest part_size must be <= {GITHUB_ASSET_CAP}: {part_size!r}")
    if not isinstance(data["sha256"], str) or len(data["sha256"]) != 64:
        _die("manifest sha256 must be a 64-hex whole-file digest")
    parts = data["parts"]
    if not isinstance(parts, list) or not parts:
        _die("manifest parts must be a non-empty list")
    seen_names: set[str] = set()
    total_seen = 0
    for idx, part in enumerate(parts):
        pname: str = part.get("name", "")
        psize: int = part.get("size", 0)
        psha: str = part.get("sha256", "")
        if pname != f"{name}.part{idx}":
            _die(f"part {idx} named {pname!r}, expected {name}.part{idx}")
        if pname in seen_names:
            _die(f"duplicate part name: {pname}")
        seen_names.add(pname)
        if not isinstance(psize, int) or psize <= 0 or psize > GITHUB_ASSET_CAP:
            _die(f"part {pname} size invalid: {psize!r}")
        if not isinstance(psha, str) or len(psha) != 64:
            _die(f"part {pname} sha256 must be 64 hex")
        total_seen += psize
    if total_seen != total:
        _die(f"Σ parts ({total_seen}) != total ({total})")
    return data


def load(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"cannot read manifest {path}: {exc}")
    if not isinstance(raw, dict):
        _die(f"manifest {path} is not a JSON object")
    return validate(raw)


def write(data: dict, path: Path) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def derive_parts(name: str, total: int, part_size: int) -> list[dict]:
    """Uniform split layout: N full slices + one tail.

    The layout `awrtifact split` produces, and the mirror workflow's matrix
    math assumes. Non-uniform splits must supply explicit parts instead.
    """
    if total <= part_size:
        return [{"name": f"{name}.part0", "size": total}]
    parts = []
    pos = 0
    idx = 0
    while pos < total:
        size = min(part_size, total - pos)
        parts.append({"name": f"{name}.part{idx}", "size": size})
        pos += size
        idx += 1
    return parts
