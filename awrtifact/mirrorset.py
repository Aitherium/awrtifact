"""The mirror set — one manifest for a whole Hugging Face repo in a release.

A single-file mirror is described by `manifest.py`'s split manifest. A REPO is
many files, most of them small, some of them over GitHub's 2 GiB asset cap;
this is the document that ties them together so a fetch can restore the
original layout and verify every byte it can:

    {
      "schema": 1,
      "kind": "hf-mirror",
      "name": "org--name",                    # the set (asset stem)
      "source": {"kind": "model", "repo_id": "org/name", "revision": "main",
                 "commit": "<sha>", "url": "https://huggingface.co/org/name",
                 "subpath": ""},
      "target": {"repo": "Owner/repo", "release": "hf-org--name-<sha7>"},
      "part_size": 1900000000,
      "total": 123456789,
      "files": [
        {"path": "config.json", "asset": "config.json", "size": 807,
         "sha256": null, "oid": "4ff6…", "parts": []},
        {"path": "model-00001-of-00002.safetensors",
         "asset": "model-00001-of-00002.safetensors", "size": 4990000000,
         "sha256": "8111d5…", "oid": "…",
         "parts": [{"name": "….part0", "size": 1900000000},
                   {"name": "….part1", "size": 1900000000},
                   {"name": "….part2", "size": 1190000000}]}
      ],
      "created": "2026-09-06T…"
    }

Asset naming: a release asset has no directories, so a path becomes an asset
name by joining its segments with `__` (`onnx/model.onnx` → `onnx__model.onnx`)
and replacing anything outside `[A-Za-z0-9._-]` with `_`. The ORIGINAL path is
kept beside it; a fetch writes to `path`, never to `asset`. Two paths mapping to
one asset name is refused up front rather than letting the second upload
clobber the first — GitHub answers 200 to a clobber.

`sha256` is the LFS oid the Hub publishes (already the whole-file sha256) or
`null` for a non-LFS file until the local lane has read the bytes. A `null`
survives into the fetch report as "unverified" — never as a pass.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from .manifest import DEFAULT_PART_SIZE, GITHUB_ASSET_CAP, derive_parts

SCHEMA_VERSION = 1
KIND = "hf-mirror"
SET_SUFFIX = ".mirrorset.json"

_ASSET_BAD = re.compile(r"[^A-Za-z0-9._-]")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class MirrorSetError(ValueError):
    """The set is malformed — nothing downstream may act on it."""


def asset_name(path: str) -> str:
    """Flatten a repo path into a legal, reversible-enough release asset name."""
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if not segs:
        raise MirrorSetError(f"empty path: {path!r}")
    flat = "__".join(segs)
    flat = _ASSET_BAD.sub("_", flat)
    if flat.startswith("."):
        # GitHub rewrites a dot-leading asset name on upload (`.gitattributes`
        # landed as `default.gitattributes` -- measured 2026-09-06), so the
        # name the manifest expects would never match the name the release
        # holds. Prefix; the original path is kept beside it regardless.
        flat = "_" + flat
    return flat


def set_name(repo_id: str) -> str:
    return _ASSET_BAD.sub("_", repo_id.replace("/", "--"))


def default_release(repo_id: str, commit: str) -> str:
    return f"hf-{set_name(repo_id)}-{commit[:7]}"


def build(files, *, ref, commit: str, repo: str, release: str,
          part_size: int = DEFAULT_PART_SIZE, prefix: str = "") -> dict:
    """Assemble a mirror set from `hf.list_tree` output.

    `files` is an iterable of objects with .path/.size/.sha256/.oid (HfFile).
    `prefix` namespaces asset names when several sets share one release.
    """
    if not isinstance(part_size, int) or part_size <= 0 or part_size > GITHUB_ASSET_CAP:
        raise MirrorSetError(f"part_size must be in (0, {GITHUB_ASSET_CAP}]: {part_size!r}")
    entries: list[dict] = []
    seen_assets: dict[str, str] = {}
    total = 0
    for f in sorted(files, key=lambda x: x.path):
        asset = (prefix + asset_name(f.path)) if prefix else asset_name(f.path)
        if asset.endswith(SET_SUFFIX):
            raise MirrorSetError(f"{f.path} would collide with the set manifest name")
        if asset in seen_assets:
            raise MirrorSetError(
                f"asset name collision: {f.path!r} and {seen_assets[asset]!r} "
                f"both flatten to {asset!r} — pass --prefix or mirror a subpath")
        seen_assets[asset] = f.path
        parts: list[dict] = []
        if f.size > GITHUB_ASSET_CAP or f.size > part_size:
            parts = [{"name": p["name"], "size": p["size"]}
                     for p in derive_parts(asset, f.size, part_size)]
        entries.append({
            "path": f.path,
            "asset": asset,
            "size": int(f.size),
            "sha256": f.sha256 if (f.sha256 and _HEX64.match(f.sha256)) else None,
            "oid": f.oid or "",
            "parts": parts,
        })
        total += int(f.size)
    data = {
        "schema": SCHEMA_VERSION,
        "kind": KIND,
        "name": set_name(ref.repo_id),
        "source": {
            "kind": ref.kind,
            "repo_id": ref.repo_id,
            "revision": ref.revision,
            "commit": commit,
            "url": ref.html_url,
            "subpath": ref.subpath,
        },
        "target": {"repo": repo, "release": release},
        "part_size": part_size,
        "total": total,
        "files": entries,
        "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return validate(data)


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise MirrorSetError("mirror set must be a JSON object")
    if data.get("schema") != SCHEMA_VERSION or data.get("kind") != KIND:
        raise MirrorSetError("not an awrtifact mirror set (schema/kind)")
    for key in ("name", "source", "target", "part_size", "total", "files"):
        if key not in data:
            raise MirrorSetError(f"mirror set missing {key}")
    src, tgt = data["source"], data["target"]
    for key in ("kind", "repo_id", "revision", "commit"):
        if not isinstance(src.get(key), str) or not src[key]:
            raise MirrorSetError(f"source.{key} missing")
    if src["kind"] not in ("model", "dataset", "space"):
        raise MirrorSetError(f"source.kind invalid: {src['kind']!r}")
    for key in ("repo", "release"):
        if not isinstance(tgt.get(key), str) or "/" not in tgt["repo"] or not tgt["release"]:
            raise MirrorSetError(f"target.{key} missing or malformed")
    part_size = data["part_size"]
    if not isinstance(part_size, int) or part_size <= 0 or part_size > GITHUB_ASSET_CAP:
        raise MirrorSetError("part_size out of range")
    files = data["files"]
    if not isinstance(files, list):
        raise MirrorSetError("files must be a list")
    seen_assets: set[str] = set()
    seen_paths: set[str] = set()
    total = 0
    for f in files:
        for key in ("path", "asset", "size", "parts"):
            if key not in f:
                raise MirrorSetError(f"file entry missing {key}: {f!r}")
        if not isinstance(f["size"], int) or f["size"] < 0:
            raise MirrorSetError(f"bad size for {f['path']}")
        if f["asset"] in seen_assets or f["path"] in seen_paths:
            raise MirrorSetError(f"duplicate entry: {f['path']}")
        if _ASSET_BAD.search(f["asset"]) or "/" in f["asset"]:
            raise MirrorSetError(f"illegal asset name {f['asset']!r}")
        seen_assets.add(f["asset"])
        seen_paths.add(f["path"])
        sha = f.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not _HEX64.match(sha)):
            raise MirrorSetError(f"sha256 for {f['path']} must be 64 hex or null")
        parts = f["parts"]
        if not isinstance(parts, list):
            raise MirrorSetError(f"parts for {f['path']} must be a list")
        if parts:
            psum = 0
            for idx, p in enumerate(parts):
                if p.get("name") != f"{f['asset']}.part{idx}":
                    raise MirrorSetError(f"part {idx} of {f['path']} misnamed: {p.get('name')!r}")
                if not isinstance(p.get("size"), int) or p["size"] <= 0 or p["size"] > part_size:
                    raise MirrorSetError(f"part {idx} of {f['path']} size invalid")
                psum += p["size"]
            if psum != f["size"]:
                raise MirrorSetError(f"Σ parts != size for {f['path']}")
        elif f["size"] > GITHUB_ASSET_CAP:
            raise MirrorSetError(f"{f['path']} exceeds the asset cap and has no parts")
        total += f["size"]
    if total != data["total"]:
        raise MirrorSetError(f"total {data['total']} != Σ files {total}")
    return data


def manifest_asset(data: dict) -> str:
    return f"{data['name']}{SET_SUFFIX}"


def write(data: dict, path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(validate(data), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def load(path: Path) -> dict:
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise MirrorSetError(f"cannot read mirror set {path}: {exc}") from exc


def expected_assets(data: dict) -> dict[str, int]:
    """Every asset name the release must carry, with its exact size."""
    out: dict[str, int] = {}
    for f in data["files"]:
        if f["parts"]:
            for p in f["parts"]:
                out[p["name"]] = p["size"]
        elif f["size"] > 0:  # GitHub refuses a 0-byte asset; fetch recreates it
            out[f["asset"]] = f["size"]
    return out


def plan_missing(data: dict, present: dict[str, int] | set[str]) -> list[dict]:
    """Items the release lacks (or holds at the wrong size): one per whole
    asset or part, carrying the byte range to fetch from the source."""
    have: dict[str, int | None]
    if isinstance(present, dict):
        have = dict(present)
    else:
        have = {n: None for n in present}
    items: list[dict] = []
    for f in data["files"]:
        if f["parts"]:
            offset = 0
            for idx, p in enumerate(f["parts"]):
                got = have.get(p["name"], -1)
                if got == -1 or (got is not None and got != p["size"]):
                    items.append({"path": f["path"], "asset": p["name"], "idx": idx,
                                  "offset": offset, "size": p["size"], "whole": False})
                offset += p["size"]
        elif f["size"] > 0:
            got = have.get(f["asset"], -1)
            if got == -1 or (got is not None and got != f["size"]):
                items.append({"path": f["path"], "asset": f["asset"], "idx": 0,
                              "offset": 0, "size": f["size"], "whole": True,
                              "sha256": f.get("sha256")})
    return items


def pack_lanes(items: list[dict], max_lanes: int = 20) -> list[list[dict]]:
    """Greedy bytes-balanced packing of work items into <= max_lanes lists —
    GitHub caps a matrix at 256 jobs, and a dataset repo has more files than
    that; one runner per FILE would refuse to schedule."""
    if max_lanes <= 0:
        raise MirrorSetError("max_lanes must be positive")
    if not items:
        return []
    n = min(max_lanes, len(items))
    lanes: list[list[dict]] = [[] for _ in range(n)]
    loads = [0] * n
    for it in sorted(items, key=lambda x: -x["size"]):
        i = loads.index(min(loads))
        lanes[i].append(it)
        loads[i] += max(1, it["size"])
    return [lane for lane in lanes if lane]
