"""`awrtifact backup-catalog` — dispatch the mirror workflow for gaps.

The point of the precaution: a spec with `source_url` per artifact is the
backup MANIFEST — every artifact listed has a named, size-checked origin and
a target release. This command turns the spec into workflow dispatches for
artifacts whose parts are NOT yet in the release (the resumable plan check),
so the mirror can be brought up deliberately and capped (the workflow's own
≤20-runner fan-out; the origin box's uplink is never involved).

`--dry-run` prints the dispatches; without it, dispatches via `gh workflow
run`. The workflow must exist in the store's repo — this checks, because a
dispatch to a missing workflow fails as a SILENCE (gh prints a warning and
exits 0 — measured class; `gh workflow run` on an unknown workflow does not
fail the caller).
"""

from __future__ import annotations

import sys

from . import gh
from . import spec as spec_mod

DEFAULT_WORKFLOW = "mirror-hf-to-release.yml"


def backup_catalog(
    spec: dict,
    workflow: str = DEFAULT_WORKFLOW,
    dry_run: bool = False,
) -> dict:
    """Dispatch mirror runs for artifacts missing from their releases."""
    repo = spec["store"]["repo"]
    if not dry_run and not gh.workflow_exists(repo, workflow):
        raise ValueError(
            f"workflow {workflow} does not exist in {repo} — cannot dispatch; "
            f"re-push the workflow first"
        )
    planned: list[dict] = []
    local_only: list[str] = []
    for art in spec.get("artifacts") or []:
        name = art["name"]
        if not art.get("source_url"):
            # Local-only artifact: no cloud lane can mirror it; it is a
            # `awrtifact mirror <local-path>` candidate. A None source_url
            # reaching the dispatch would upload parts named "None.partN"
            # (the measured 2026-08-27 incident) — skipped and reported.
            local_only.append(name)
            continue
        release = spec_mod.artifact_release(spec, art)
        existing = gh.release_assets(repo, release)
        parts = spec_mod.chunked_map(spec).get(name)
        if not parts:
            # Whole-file artifact: present when the asset itself exists.
            if name in existing:
                continue
            inputs = {
                "hf_url": art["source_url"],
                "name": name,
                "total_bytes": str(art["total"]),
                "release": release,
            }
            planned.append({"artifact": name, "inputs": inputs})
            continue
        missing = [p["name"] for p in parts["parts"] if p["name"] not in existing]
        if not missing:
            continue
        inputs = {
            "hf_url": art["source_url"],
            "name": name,
            "total_bytes": str(art["total"]),
            "release": release,
            "part_size": str(art.get("part_size") or 1900000000),
        }
        planned.append(
            {"artifact": name, "inputs": inputs, "missing_parts": len(missing)}
        )
    if dry_run:
        for item in planned:
            print(f"dispatch {item['artifact']}: {item['inputs']}", file=sys.stderr)
    else:
        for item in planned:
            gh.workflow_dispatch(repo, workflow, item["inputs"])
            print(f"dispatched {item['artifact']} -> {workflow} in {repo}")
    return {
        "dispatched": len(planned),
        "local_only": local_only,
        "already_present": len(
            [a for a in spec.get("artifacts") or [] if a["name"] not in
             {i["artifact"] for i in planned} and a["name"] not in local_only]
        ),
    }
