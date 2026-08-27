"""`awrtifact provision-repo` — one command to a working backup repo.

The newbie half of the backup story: `awrtifact split` + `awrtifact upload`
presume a repo already exists, and a brand-new repo is the ordinary FIRST
backup — which GitHub refuses to publish releases in until it has a commit
(measured 2026-08-27: 422 "Repository is empty", uploads 404 through
api.github.com). This command makes the repo real in one shot:

    awrtifact provision-repo --repo you/backups [--public]

- creates the repo if missing (private unless --public),
- seeds an init README + the GobboNet backup mod + the gate page
  (`backup-gate.html`) into it, so the repo's GitHub Pages IS the shareable
  gate — the "set your passphrase in your GitHub page yourself" model,
- enables Pages when the plan allows (public repos always; private repos
  need a paid plan — refused there with a warning, not a failure),
- prints the repo URL and the gate URL.

The seeded mod/gate copies are snapshots of `public/gobbonet/` kept in step
by hand when the mod changes; the sha256s are pinned in the test so a
forgotten update is loud.

The passphrase model is unchanged: the repo can be public — the bytes the
mod uploads are AES-GCM ciphertext either way.
"""

from __future__ import annotations

import base64
from pathlib import Path

from . import gh

DATA_DIR = Path(__file__).parent / "data" / "gobbonet"
# (path-in-repo, local asset) — order matters: README first so the repo has
# a commit before anything else; the gate assets reference each other
# relative (backup-gate.html loads gobbonet-backup.js from the same dir).
SEED_FILES = (
    ("README.md", "README.md"),
    ("gobbonet-backup.js", "gobbonet-backup.js"),
    ("backup-gate.html", "backup-gate.html"),
)

README_TEMPLATE = """# {repo} — encrypted model backups

Created by `awrtifact provision-repo`.

This repository holds **encrypted** backups: the GobboNet backup mod
(`gobbonet-backup.js`) splits files into `.partN` slices, AES-GCM-encrypts
them with a key derived from YOUR passphrase (PBKDF2-SHA256, 600k
iterations), and uploads them to GitHub releases in this repo. The
passphrase is the key — the repo can be public, the bytes are ciphertext
either way.

- **Back up** — paste `gobbonet-backup.js` into GobboNet Settings →
  Extensions (or use `awrtifact split` + `awrtifact upload`).
- **Share** — send someone this repo's gate page URL
  (`{gate_url}`) plus the passphrase; the page decrypts in their browser.

The passphrase is never stored anywhere. Lose it, lose the backup.
"""


def _put_file(repo: str, path: str, content: str) -> None:
    """PUT one file into the repo. A 422 (content already exists) is
    idempotent — the file is already there."""
    proc = gh._run(
        [
            "api",
            "-X",
            "PUT",
            f"repos/{repo}/contents/{path}",
            "-f",
            f"message=awrtifact provision: {path}",
            "-f",
            f"content={base64.b64encode(content.encode()).decode()}",
        ]
    )
    if proc.returncode != 0 and "422" not in proc.stderr:
        raise gh.GhError(f"seeding {path}: {proc.stderr.strip()}")


def provision_repo(
    repo: str, public: bool = False, pages: bool = True
) -> dict:
    """Create (if missing) and seed a backup repo; return the URLs.

    Idempotent: an existing repo is reused, existing files are left alone,
    Pages-enable is retried silently. Returns {"repo", "html_url",
    "gate_url", "pages": bool, "seeded": [paths]}.
    """
    if "/" not in repo:
        raise ValueError("repo must be OWNER/NAME")
    owner, name = repo.split("/", 1)

    info = gh.repo_view(repo)
    if info is None:
        gh.repo_create(repo, public)
        info = gh.repo_view(repo) or {"url": f"https://github.com/{repo}"}

    gate_url = (
        f"https://{owner}.github.io/{name}/backup-gate.html"
    )
    readme = README_TEMPLATE.format(repo=repo, gate_url=gate_url)

    seeded: list[str] = []
    for path, local in SEED_FILES:
        if path == "README.md":
            content = readme
        else:
            content = (DATA_DIR / local).read_text(encoding="utf-8")
        _put_file(repo, path, content)
        seeded.append(path)

    pages_ok = True
    if pages:
        proc = gh._run(
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
        if proc.returncode != 0:
            # Private repos need a paid plan for Pages; public repos always
            # work. Refusal is a warning, not a failure — the repo and the
            # backups still work; only the share-by-gate-page half needs it.
            pages_ok = False

    return {
        "repo": repo,
        "html_url": info.get("url", f"https://github.com/{repo}"),
        "gate_url": gate_url if pages_ok else None,
        "pages": pages_ok,
        "seeded": seeded,
    }
