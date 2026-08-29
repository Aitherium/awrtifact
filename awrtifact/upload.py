"""`awrtifact upload` — size-checked, parallel, resumable release upload.

Only missing (or size-mismatched) parts are uploaded — `plan` decides. Every
part is size-checked against the manifest BEFORE upload (the workflow lane's
`stat -c%s` guard): uploading a truncated local part would make the release
look complete while serving corrupt bytes.

The manifest itself is uploaded LAST as ``<name>.manifest.json`` (missing
only, same resumable rule as the parts) — a release that carries its
manifest is self-describing: the gate page, `awrtifact fetch` and any
consumer can read the part list and hashes from the release alone, with no
spec side-channel. The spec remains the SERVING truth (workers, allowlist);
the release manifest is the artifact's data truth, and the lane gate
(AW007) asserts the two agree.

`--parallel N` uploads with a thread pool; `--create` creates the release if
it does not exist (the seed lanes both did this by hand).
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

from . import gh
from . import plan as plan_mod


def _check_local(part_path: Path, part: dict) -> None:
    if not part_path.is_file():
        raise ValueError(f"missing local part: {part_path}")
    size = part_path.stat().st_size
    if size != part["size"]:
        raise ValueError(
            f"local part {part_path.name} is {size} bytes, "
            f"manifest says {part['size']} — re-split or fetch the right copy"
        )


def upload_manifest(
    manifest: dict,
    repo: str,
    release: str,
    dir: Path,
    parallel: int = 1,
    create: bool = False,
) -> dict:
    """Upload missing parts + the manifest asset; returns
    {"uploaded": [...], "skipped_present": [...], "manifest": "uploaded"|"present",
    "failed": [...]}."""
    dir = Path(dir)
    if create and not gh.release_exists(repo, release):
        gh.create_release(
            repo,
            release,
            title=f"awrtifact: {manifest['name']}",
            notes=f"Chunked release for {manifest['name']} "
            f"({manifest['total']} bytes, split by awrtifact)",
        )
    planned = plan_mod.plan_parts(manifest, repo, release)
    todo = planned["need_upload"]
    uploaded: list[str] = []
    failed: list[str] = []

    def _one(idx: int) -> None:
        part = manifest["parts"][idx]
        part_path = dir / part["name"]
        _check_local(part_path, part)
        gh.upload(repo, release, str(part_path))
        uploaded.append(part["name"])

    if parallel > 1 and len(todo) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallel
        ) as pool:
            futures = {pool.submit(_one, idx): idx for idx in todo}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 — report per part
                    failed.append(f"{manifest['parts'][futures[fut]]['name']}: {exc}")
    else:
        for idx in todo:
            try:
                _one(idx)
            except Exception as exc:  # noqa: BLE001 — report per part
                failed.append(f"{manifest['parts'][idx]['name']}: {exc}")

    # The manifest asset — the release's self-description. Missing-only like
    # the parts, so a re-run after a mid-flight failure just lands it. The
    # RELEASE asset is named <artifact>.manifest.json, so the file is copied
    # to that name locally first (gh's `file#name` rename syntax was removed
    # by gh 2.83 — measured 2026-08-27, the asset landed under the source
    # basename and had to be deleted); the asset is the same bytes as the
    # manifest.json split wrote.
    manifest_asset = f"{manifest['name']}.manifest.json"
    manifest_asset_local = dir / manifest_asset
    manifest_asset_local.write_bytes((dir / "manifest.json").read_bytes())
    manifest_state = "present"
    present = gh.asset_sizes(repo, release)
    if present.get(manifest_asset) != manifest_asset_local.stat().st_size:
        try:
            gh.upload(repo, release, str(manifest_asset_local))
            manifest_state = "uploaded"
        except Exception as exc:  # noqa: BLE001 — same per-asset reporting
            failed.append(f"{manifest_asset}: {exc}")

    return {
        "uploaded": uploaded,
        "skipped_present": [manifest["parts"][i]["name"] for i in planned["present"]],
        "manifest": manifest_state,
        "failed": failed,
    }
