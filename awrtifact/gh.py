"""GitHub access — `gh` CLI by default, the REST API when there is no `gh`.

Deliberately shells the ambient `gh` CLI where one exists (like the lanes it
productizes — `seed-q1-mirror.ps1`, `build_webml_cdn.mjs`,
`mirror-hf-to-release.yml` all use it): the operator's existing auth is the
auth, no token handling here. Every call is size-bounded (release asset
lists) and text-decoded explicitly.

Backend selection (`AWRTIFACT_GH_BACKEND`):

    cli    always shell `gh` (the default when `gh` is on PATH)
    api    always `ghapi.py` over urllib with GH_TOKEN / GITHUB_TOKEN
    auto   `gh` if on PATH, else the API when a token is present, else a
           GhError that names BOTH missing things — a container with neither
           must fail at the first call, not at the first upload.

Tests pin `cli` (conftest) and monkeypatch `_run`; a CI box that happens to
carry a token but no `gh` would otherwise route the fakes to the real API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Sequence

from . import ghapi


class GhError(RuntimeError):
    """A gh invocation failed — message carries gh's stderr."""


def backend() -> str:
    mode = (os.getenv("AWRTIFACT_GH_BACKEND") or "auto").strip().lower()
    if mode == "cli":
        return "cli"
    if mode == "api":
        return "api"
    if shutil.which("gh"):
        return "cli"
    if ghapi.token():
        return "api"
    raise GhError("no `gh` on PATH and no GH_TOKEN/GITHUB_TOKEN — install gh "
                  "or export a token (AWRTIFACT_GH_BACKEND=api)")


def _api() -> bool:
    return backend() == "api"


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ghapi.GhApiError as exc:
        raise GhError(str(exc)) from exc


def release_assets(repo: str, release: str) -> set[str]:
    """Asset names currently in the release. Empty set on a missing release."""
    if _api():
        return _wrap(ghapi.release_assets, repo, release)
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
    if _api():
        return _wrap(ghapi.release_exists, repo, release)
    proc = _run(["release", "view", release, "--repo", repo])
    return proc.returncode == 0


def create_release(repo: str, release: str, title: str, notes: str) -> None:
    if _api():
        _wrap(ghapi.create_release, repo, release, title, notes)
        return
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
    if _api():
        _wrap(ghapi.upload, repo, release, path)
        return
    proc = _run(
        ["release", "upload", release, path, "--repo", repo, "--clobber"]
    )
    if proc.returncode != 0:
        raise GhError(f"gh release upload {path}: {proc.stderr.strip()}")


def workflow_dispatch(
    repo: str, workflow: str, inputs: dict[str, str]
) -> None:
    """Fire a workflow_dispatch with string inputs (mirror lane)."""
    if _api():
        _wrap(ghapi.workflow_dispatch, repo, workflow, inputs)
        return
    args = ["workflow", "run", workflow, "--repo", repo]
    for key, value in sorted(inputs.items()):
        args.extend(["-f", f"{key}={value}"])
    proc = _run(args)
    if proc.returncode != 0:
        raise GhError(f"gh workflow run {workflow}: {proc.stderr.strip()}")


def asset_sizes(repo: str, release: str) -> dict[str, int]:
    """name -> size for the release's assets (verify/plan use this)."""
    if _api():
        return _wrap(ghapi.asset_sizes, repo, release)
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
    if _api():
        return _wrap(ghapi.workflow_exists, repo, workflow)
    proc = _run(["workflow", "view", workflow, "--repo", repo])
    return proc.returncode == 0


def repo_view(repo: str) -> dict | None:
    """Repo metadata (provision-repo uses .url). None when it does not exist."""
    if _api():
        return _wrap(ghapi.repo_view, repo)
    proc = _run(
        ["repo", "view", repo, "--json", "url", "--jq", ".url"]
    )
    if proc.returncode != 0:
        return None
    return {"url": proc.stdout.strip()}


def repo_create(repo: str, public: bool) -> None:
    """Create the repo under the authed account/org. The gh CLI keeps the
    invite/confirm prompts off with --confirm; the visibility flag is the
    only decision."""
    if _api():
        _wrap(ghapi.repo_create, repo, public)
        return
    proc = _run(
        [
            "repo",
            "create",
            repo,
            "--confirm",
            "--private" if not public else "--public",
        ]
    )
    if proc.returncode != 0:
        raise GhError(f"gh repo create {repo}: {proc.stderr.strip()}")


def put_file(repo: str, path: str, content: str, message: str | None = None) -> str:
    """Seed one file (contents API). Idempotent: an existing file is left
    alone (returns "present"); otherwise "created"."""
    message = message or f"awrtifact provision: {path}"
    if _api():
        return _wrap(ghapi.put_file, repo, path, content, message, True)
    import base64
    proc = _run(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/contents/{path}",
            "-f",
            f"message={message}",
            "-f",
            f"content={base64.b64encode(content.encode()).decode()}",
        ]
    )
    if proc.returncode == 0:
        return "created"
    # A 422 is "content already exists" — the file is already there.
    if "422" in proc.stderr:
        return "present"
    raise GhError(f"seeding {path}: {proc.stderr.strip()}")


def enable_pages(repo: str) -> bool:
    """Enable Pages from main:/ — False (never raise) when the plan refuses."""
    if _api():
        return _wrap(ghapi.enable_pages, repo)
    proc = _run(
        [
            "api",
            "-X",
            "POST",
            f"repos/{repo}/pages",
            "-f",
            "source[branch]=main",
            "-f",
            "source[path]=/",
        ]
    )
    return proc.returncode == 0


def download_asset(repo: str, release: str, name: str, dest: str) -> int:
    """Download one release asset to `dest` (private repos included). Bytes."""
    if _api():
        return _wrap(ghapi.download_asset, repo, release, name, dest)
    proc = _run(["release", "download", release, "--repo", repo, "-p", name,
                 "-O", dest, "--clobber"])
    if proc.returncode != 0:
        raise GhError(f"gh release download {name}: {proc.stderr.strip()}")
    import os as _os
    return _os.path.getsize(dest)


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
