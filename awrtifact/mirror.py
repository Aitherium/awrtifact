"""`awrtifact mirror` — feed it a URL or a local file, it mirrors to GitHub.

The one-command entry point the owner asked for (2026-08-27: "feed it a
link/URL/repo/artifact and it seamlessly mirrors it to GitHub"). Auto-detects
the source:

  awrtifact mirror https://huggingface.co/.../model.gguf --release fleet-v1
      → HEAD the URL, fail loud if it does not answer Range with a
        Content-Length, then dispatch the cloud-to-cloud mirror workflow
        (origin uplink never involved; ≤20 parallel runners).

  awrtifact mirror ./model.gguf --release fleet-v1 --repo Aitherium/aitherkvcache
      → split (parts under the 2 GiB cap), create the release if needed,
        upload missing parts in parallel, verify byte-for-byte.

A URL that is not Range-serving cannot be mirrored by either lane — that is
a hard error, not a fallback to the local path (a fallback would upload the
whole file over the origin uplink, which is exactly the 100 GB mistake the
workflow exists to avoid).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import gh
from . import split as split_mod
from . import upload as upload_mod
from . import verify as verify_mod
from .manifest import write as manifest_write

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
DEFAULT_WORKFLOW = "mirror-hf-to-release.yml"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003 — urllib signature
        return None


def _head_size(url: str, expected: int | None = None) -> int:
    """HEAD a URL; returns Content-Length. Raises when not Range-servable.

    Walks the redirect chain by hand and takes the LAST Content-Length seen —
    the same semantics as the mirror workflow's `curl -sIL | tail -1`. A
    single urlopen() is NOT enough: the HF resolve → CDN chain serves the
    final Content-Length to curl's HEAD but not to urllib's (measured
    2026-08-27 — final hop 200 with no Content-Length header).
    """
    opener = urllib.request.build_opener(_NoRedirect)
    location = url
    for _ in range(10):
        req = urllib.request.Request(
            location, method="HEAD", headers={"User-Agent": "awrtifact-mirror/1"}
        )
        try:
            with opener.open(req, timeout=60) as resp:  # noqa: S310
                # Keep the raw message object: email.message.get() is
                # CASE-INSENSITIVE, and CDNs send lowercase header names
                # (measured 2026-08-27: xet-bridge answers content-range: —
                # a dict() lookup for "Content-Range" misses).
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            # With redirects disabled, urllib raises for 3xx — that IS the hop.
            if exc.code not in (301, 302, 303, 307, 308):
                raise ValueError(
                    f"source URL answered {exc.code} at {location}"
                ) from exc
            headers = exc.headers
        except urllib.error.URLError as exc:
            raise ValueError(f"source URL unreachable: {exc.reason}") from exc
        next_loc = headers.get("Location")
        if next_loc:
            location = urllib.parse.urljoin(location, next_loc)
            continue
        # Final hop: only ITS Content-Length counts — redirect pages carry
        # their own body lengths and must never be read as the artifact size.
        if "Content-Length" in headers:
            size = int(headers["Content-Length"])
        else:
            # Some CDNs answer HEAD without Content-Length but serve ranged
            # GETs (measured 2026-08-27: HF's CDN gives curl -I a length and
            # urllib nothing — same URL). A ranged GET is the real requirement
            # for mirroring anyway; probe with one.
            size = _range_probe_size(location)
        if expected is not None and size != expected:
            raise ValueError(f"source size {size} != declared {expected}")
        return size
    raise ValueError("source URL redirect chain too deep")


def _range_probe_size(url: str) -> int:
    """GET bytes=0-0 and read the total from Content-Range (or Content-Length).

    206 + Content-Range: bytes 0-0/<TOTAL> → TOTAL. A 200 means the server
    ignored Range — then Content-Length IS the full size (still mirrorable:
    the workflow's range fetch would download the whole body per runner, which
    is wrong — so a 200 answer is refused, not accepted).
    """
    req = urllib.request.Request(
        url, method="GET", headers={"Range": "bytes=0-0", "User-Agent": "awrtifact-mirror/1"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            cr = resp.headers.get("Content-Range", "")  # case-insensitive
    except urllib.error.HTTPError as exc:
        raise ValueError(f"ranged probe answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"ranged probe unreachable: {exc.reason}") from exc
    m = re.search(r"/(\d+)\s*$", cr)
    if m:
        return int(m.group(1))
    raise ValueError("source does not honor Range — it cannot be mirrored "
                     "by the cloud lane")


def mirror_url(url: str, name: str | None, release: str, repo: str,
               total: int | None, workflow: str = DEFAULT_WORKFLOW) -> dict:
    """Mirror a Range-serving URL via the cloud workflow (dispatch)."""
    if not name:
        # Derive the asset name from the URL path — a None reaching the
        # dispatch would upload parts named "None.partN" (measured 2026-08-27:
        # exactly that was dispatched once before this guard landed).
        name = urllib.parse.unquote(
            urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        )
        if not name:
            raise ValueError("cannot derive an asset name from the URL — pass --name")
    size = _head_size(url, total)
    if not gh.workflow_exists(repo, workflow):
        raise ValueError(
            f"workflow {workflow} does not exist in {repo} — the mirror "
            f"workflow must live in the TARGET repo's .github/workflows/ "
            f"before a dispatch can run it. Copy "
            f".DEPLOYMENT/workers/awrtifact/{workflow} and "
            f".DEPLOYMENT/workers/awrtifact/hash-release-object.yml "
            f"(both, from the awrtifact source tree) into your repo's "
            f".github/workflows/ and push — or use a repo that already "
            f"has them (aitherkvcache does)"
        )
    gh.workflow_dispatch(repo, workflow, {
        "hf_url": url,
        "name": name,
        "total_bytes": str(size),
        "release": release,
        "part_size": "1900000000",
    })
    return {"lane": "cloud", "url": url, "name": name, "size": size,
            "release": release, "status": "dispatched"}


def mirror_file(path: Path, release: str, repo: str, name: str | None,
                parallel: int = 4) -> dict:
    """Mirror a local file: split → release → upload → verify."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    m = split_mod.split_file(path, out_dir=path.parent)
    if name:
        m["name"] = name
    manifest_path = path.parent / "manifest.json"
    manifest_write(m, manifest_path)
    result = upload_mod.upload_manifest(
        m, repo, release, path.parent, parallel=parallel, create=True
    )
    if result["failed"]:
        raise ValueError(f"upload failed: {result['failed']}")
    report = verify_mod.verify_manifest(m, path.parent)
    if not report["ok"]:
        raise ValueError("verify failed after upload — parts disagree with the manifest")
    return {"lane": "local", "file": str(path), "release": release, "repo": repo,
            "parts": len(m["parts"]), "bytes": m["total"], "status": "verified"}
