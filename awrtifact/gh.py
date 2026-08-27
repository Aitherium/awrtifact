"""`gh` CLI wrapper — the smallest shell around GitHub's release API.

Deliberately shells the ambient `gh` CLI (like the lanes it productizes —
`seed-q1-mirror.ps1`, `build_webml_cdn.mjs`, `mirror-hf-to-release.yml` all
use it): the operator's existing auth is the auth, no token handling here.
Every call is size-bounded (release asset lists) and text-decoded explicitly.
"""

from __future__ import annotations

import json
import subprocess
from typing import Sequence


class GhError(RuntimeError):
    """A gh invocation failed — message carries gh's stderr."""


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def release_assets(repo: str, release: str) -> set[str]:
    """Asset names currently in the release. Empty set on a missing release."""
    proc = _run(
        [
            "release",
            "view",
            release,
            "--repo",
            repo,
            "--json",
            "assets",
            "--jq",
            ".assets[].name",
        ]
    )
    if proc.returncode != 0:
        return set()
    names = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return names


def release_exists(repo: str, release: str) -> bool:
    proc = _run(["release", "view", release, "--repo", repo])
    return proc.returncode == 0


def create_release(repo: str, release: str, title: str, notes: str) -> None:
    proc = _run(
        [
            "release",
            "create",
            release,
            "--repo",
            repo,
            "--title",
            title,
            "--notes",
            notes,
        ]
    )
    if proc.returncode != 0:
        raise GhError(f"gh release create {release}: {proc.stderr.strip()}")


def upload(repo: str, release: str, path: str) -> None:
    """Upload one asset with --clobber (idempotent re-upload)."""
    proc = _run(
        ["release", "upload", release, path, "--repo", repo, "--clobber"]
    )
    if proc.returncode != 0:
        raise GhError(f"gh release upload {path}: {proc.stderr.strip()}")


def workflow_dispatch(
    repo: str, workflow: str, inputs: dict[str, str]
) -> None:
    """Fire a workflow_dispatch with string inputs (mirror lane)."""
    args = ["workflow", "run", workflow, "--repo", repo]
    for key, value in sorted(inputs.items()):
        args.extend(["-f", f"{key}={value}"])
    proc = _run(args)
    if proc.returncode != 0:
        raise GhError(f"gh workflow run {workflow}: {proc.stderr.strip()}")


def asset_sizes(repo: str, release: str) -> dict[str, int]:
    """name -> size for the release's assets (verify/plan use this)."""
    proc = _run(
        [
            "release",
            "view",
            release,
            "--repo",
            repo,
            "--json",
            "assets",
            "--jq",
            '.assets[] | "\\(.name)\t\\(.size)"',
        ]
    )
    if proc.returncode != 0:
        return {}
    out: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        name, size = line.split("\t", 1)
        try:
            out[name] = int(size)
        except ValueError:
            continue
    return out


def workflow_exists(repo: str, workflow: str) -> bool:
    """Is the named workflow present in the repo (backup-catalog gate)?"""
    proc = _run(["workflow", "view", workflow, "--repo", repo])
    return proc.returncode == 0


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
