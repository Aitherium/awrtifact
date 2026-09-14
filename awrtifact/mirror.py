"""`awrtifact mirror` — feed it a URL, a file, or a Hugging Face REPO; it
mirrors to GitHub.

The one-command entry point the owner asked for (2026-08-27: "feed it a
link/URL/repo/artifact and it seamlessly mirrors it to GitHub"; 2026-09-06:
"make it possible to mirror anything from Hugging Face to any GitHub repo").
Auto-detects the source:

  awrtifact mirror https://huggingface.co/.../resolve/main/model.gguf --release fleet-v1
      → ONE FILE: HEAD the URL, fail loud if it does not answer Range with a
        Content-Length, then dispatch the cloud-to-cloud mirror workflow
        (origin uplink never involved; ≤20 parallel runners).

  awrtifact mirror ./model.gguf --release fleet-v1 --repo Aitherium/aitherkvcache
      → LOCAL FILE: split (parts under the 2 GiB cap), create the release if
        needed, upload missing parts in parallel, verify byte-for-byte.

  awrtifact mirror hf://org/name[@rev] --repo OWNER/REPO [--release TAG]
  awrtifact mirror https://huggingface.co/datasets/org/name --repo OWNER/REPO
      → WHOLE REPO: enumerate every file at a pinned commit, write ONE
        `<set>.mirrorset.json` into the release, then either dispatch the
        repo-agnostic `mirror-hf-set.yml` workflow (cloud lane — seeded into
        the target repo on first use) or stream each file through this box
        (local lane). Restore with `awrtifact fetch-set`.

A URL that is not Range-serving cannot be mirrored by either lane — that is
a hard error, not a fallback to the local path (a fallback would upload the
whole file over the origin uplink, which is exactly the 100 GB mistake the
workflow exists to avoid).
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import gh, hf, mirrorset, sealing
from . import split as split_mod
from . import upload as upload_mod
from . import verify as verify_mod
from .manifest import DEFAULT_PART_SIZE
from .manifest import write as manifest_write

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
DEFAULT_WORKFLOW = "mirror-hf-to-release.yml"
SET_WORKFLOW = "mirror-hf-set.yml"
WORKFLOW_DIR = Path(__file__).parent / "data" / "workflows"
_CHUNK = 8 * 1024 * 1024
_UA = "awrtifact-mirror/1"


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
            location, method="HEAD", headers={"User-Agent": _UA}
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
        url, method="GET", headers={"Range": "bytes=0-0", "User-Agent": _UA}
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
            f"workflow {workflow} does not exist in {repo} — re-push it first"
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


# ---------------------------------------------------------------------------
# Whole-repo lane (hf://…)
# ---------------------------------------------------------------------------

def workflow_source(workflow: str) -> str:
    """The seeded workflow text (a real package asset — ships in the wheel)."""
    path = WORKFLOW_DIR / workflow
    if not path.is_file():
        raise ValueError(f"no seedable workflow named {workflow!r} in {WORKFLOW_DIR}")
    return path.read_text(encoding="utf-8")


def ensure_workflow(repo: str, workflow: str = SET_WORKFLOW,
                    seed: bool = True) -> str:
    """present | created | absent — seeds the workflow into `repo` when
    missing (so ANY repo becomes a mirror target on first use)."""
    if gh.workflow_exists(repo, workflow):
        return "present"
    if not seed:
        return "absent"
    gh.put_file(repo, f".github/workflows/{workflow}", workflow_source(workflow),
                message=f"awrtifact: seed {workflow}")
    return "created"


def _merge_known_hashes(data: dict, repo: str, release: str,
                        present: dict[str, int], work: Path) -> int:
    """Carry sha256 forward from the manifest ALREADY in the release.

    A resumed run rebuilds the set from the Hub, where non-LFS files have no
    sha256; only the items it fetches get hashed, so re-uploading the fresh
    manifest would DROP the digests an earlier run computed (measured
    2026-09-06: 6 of 7 known hashes lost on the second run). Same commit +
    same oid + same size = same bytes; keep the hash. Returns the count.
    """
    asset = mirrorset.manifest_asset(data)
    if asset not in present:
        return 0
    prev_path = work / (asset + ".prev")
    try:
        gh.download_asset(repo, release, asset, str(prev_path))
        prev = mirrorset.load(prev_path)
    except (gh.GhError, mirrorset.MirrorSetError, OSError):
        return 0  # unreadable previous manifest: nothing to merge, nothing lost
    finally:
        prev_path.unlink(missing_ok=True)
    if prev["source"].get("commit") != data["source"].get("commit"):
        return 0
    known = {(f["path"], f.get("oid", ""), f["size"]): f.get("sha256")
             for f in prev["files"] if f.get("sha256")}
    merged = 0
    for f in data["files"]:
        if not f.get("sha256"):
            sha = known.get((f["path"], f.get("oid", ""), f["size"]))
            if sha:
                f["sha256"] = sha
                merged += 1
    return merged


def _seed_initial_commit(repo: str) -> None:
    """Give a fresh repo its first commit (a README) so releases can be cut."""
    name = repo.split('/', 1)[1]
    body = (f'# {name}' + chr(10) * 2 + 'Mirror store written by awrtifact '
            f'(`awrtifact fetch-set --repo {repo} --release <tag>`).' + chr(10))
    gh.put_file(repo, 'README.md', body, message='awrtifact: initial commit')


def _download_range(url: str, dest: Path, offset: int, size: int,
                    token: str | None = None, attempts: int = 3) -> None:
    """Fetch exactly `size` bytes at `offset` into `dest` (fresh file)."""
    headers = {"User-Agent": _UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ranged = offset > 0 or size < (1 << 62)
    if ranged:
        headers["Range"] = f"bytes={offset}-{offset + size - 1}"
    last = ""
    for _ in range(attempts):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:  # noqa: S310
                status = getattr(resp, "status", 200)
                if ranged and offset > 0 and status != 206:
                    raise ValueError("source ignored Range on a partial fetch")
                remaining = size
                while remaining > 0:
                    chunk = resp.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(getattr(exc, "reason", exc))
            continue
        got = dest.stat().st_size
        if got == size:
            return
        last = f"got {got} bytes, wanted {size}"
    raise ValueError(f"fetch failed for {dest.name}: {last}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _put_manifest(data: dict, repo: str, release: str, work: Path,
                  seal: bool = False, seal_key: Path | None = None) -> str:
    asset = mirrorset.manifest_asset(data)
    path = work / asset
    mirrorset.write(data, path)
    gh.upload(repo, release, str(path))
    if seal:
        # Signed AFTER the final manifest is written: the seal covers exactly
        # the bytes the release serves, so a later clobber of the manifest is
        # detectable by anyone holding the publisher's key.
        seal_path = sealing.sign_set(data, work, key_path=seal_key)
        gh.upload(repo, release, str(seal_path))
    return asset


def plan_hf_repo(source: str, repo: str, release: str | None = None, *,
                 token: str | None = None, part_size: int = DEFAULT_PART_SIZE,
                 prefix: str = "", fetch=hf._fetch_json) -> dict:
    """Enumerate + build the set without touching GitHub. Pure planning."""
    ref = hf.parse_ref(source)
    info = hf.repo_info(ref, token, fetch=fetch)
    files = hf.list_tree(ref, token, fetch=fetch, revision=info["commit"])
    if not files:
        raise ValueError(f"{ref.as_string()} has no files under {ref.subpath or '/'}")
    release = release or mirrorset.default_release(ref.repo_id, info["commit"])
    data = mirrorset.build(files, ref=ref, commit=info["commit"], repo=repo,
                           release=release, part_size=part_size, prefix=prefix)
    data["source"]["private"] = info["private"]
    data["source"]["gated"] = bool(info["gated"])
    return data


def mirror_hf_repo(source: str, repo: str, release: str | None = None, *,
                   lane: str = "auto", token: str | None = None,
                   part_size: int = DEFAULT_PART_SIZE, prefix: str = "",
                   max_lanes: int = 20, workflow: str = SET_WORKFLOW,
                   work_dir: Path | None = None, seed_workflow: bool = True,
                   dry_run: bool = False, fetch=hf._fetch_json,
                   log=None, create_repo: bool | None = None,
                   seal: bool = False, seal_key: Path | None = None) -> dict:
    """Mirror a whole Hub repo into `repo`'s release. Returns the report.

    lane: auto (cloud when the workflow exists or can be seeded, else local),
          cloud (dispatch only), local (stream through this box), plan (no
          GitHub writes at all — same as dry_run).
    """
    log = log or (lambda *_: None)
    if lane not in ("auto", "cloud", "local", "plan"):
        raise ValueError(f"unknown lane {lane!r}")
    data = plan_hf_repo(source, repo, release, token=token, part_size=part_size,
                        prefix=prefix, fetch=fetch)
    release = data["target"]["release"]
    items_all = mirrorset.plan_missing(data, set())
    report: dict = {
        "lane": "plan", "repo": repo, "release": release, "set": data["name"],
        "manifest": mirrorset.manifest_asset(data), "source": data["source"],
        "files": len(data["files"]), "bytes": data["total"],
        "sha_unknown": sum(1 for f in data["files"] if not f.get("sha256")),
        "items_total": len(items_all),
    }
    if seal:
        # Refuse up front: a mirror believed signed and not is worse than an
        # unsigned one, and a key that fails to load must fail BEFORE the
        # release exists (measured 2026-09-06: it failed after every upload).
        report["seal_key"] = sealing.check_key(seal_key)
        report["seal"] = sealing.seal_asset(data)
    if dry_run or lane == "plan":
        report["items"] = items_all
        report["status"] = "planned"
        return report

    if gh.repo_view(repo) is None:
        if create_repo is None:
            raise ValueError(f"{repo} does not exist — run `awrtifact provision-repo "
                             f"--repo {repo}` or pass --create-repo")
        gh.repo_create(repo, public=bool(create_repo))
        _seed_initial_commit(repo)
    if not gh.release_exists(repo, release):
        title = f"awrtifact mirror: {data['source']['repo_id']} @ {data['source']['commit'][:7]}"
        notes = (f"Mirror set of {data['source']['url']} at commit "
                 f"{data['source']['commit']} ({data['total']} bytes, "
                 f"{len(data['files'])} files), written by awrtifact.")
        try:
            gh.create_release(repo, release, title=title, notes=notes)
        except gh.GhError as exc:
            # `gh repo create` makes an EMPTY repo and GitHub refuses a release
            # on one (422 "Repository is empty" — measured 2026-09-06 on the
            # first live run). A release needs a commit to tag; seed one.
            if "empty" not in str(exc).lower():
                raise
            _seed_initial_commit(repo)
            gh.create_release(repo, release, title=title, notes=notes)
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="awrtifact-"))
    work.mkdir(parents=True, exist_ok=True)
    present = gh.asset_sizes(repo, release)
    report["sha_merged"] = _merge_known_hashes(data, repo, release, present, work)
    items = mirrorset.plan_missing(data, present)
    report["items_missing"] = len(items)

    chosen = lane
    if lane == "auto":
        state = ensure_workflow(repo, workflow, seed=seed_workflow)
        chosen = "cloud" if state in ("present", "created") else "local"
        report["workflow"] = state
    elif lane == "cloud":
        state = ensure_workflow(repo, workflow, seed=seed_workflow)
        if state == "absent":
            raise ValueError(f"workflow {workflow} is not in {repo} and seeding is off")
        report["workflow"] = state

    if chosen == "cloud":
        report["manifest_status"] = "uploaded"
        _put_manifest(data, repo, release, work, seal=seal, seal_key=seal_key)
        if not items:
            report["lane"] = "cloud"
            report["status"] = "complete"
            return report
        gh.workflow_dispatch(repo, workflow, {
            "release": release,
            "manifest": report["manifest"],
            "max_lanes": str(max_lanes),
        })
        report["lane"] = "cloud"
        report["status"] = "dispatched"
        return report

    # ---- local lane: stream each missing item through this box ----------
    uploaded: list[str] = []
    by_path = {f["path"]: f for f in data["files"]}
    ref = hf.parse_ref(source)
    commit = data["source"]["commit"]
    done_paths: set[str] = set()
    for it in items:
        f = by_path[it["path"]]
        if it["path"] in done_paths:
            continue
        url = ref.resolve_url(it["path"], revision=commit)
        if f["parts"]:
            whole = work / f["asset"]
            log(f"fetch {it['path']} ({f['size']} bytes) → split")
            _download_range(url, whole, 0, f["size"], token)
            m = split_mod.split_file(whole, part_size=data["part_size"], out_dir=work)
            f["sha256"] = m["sha256"]
            have = gh.asset_sizes(repo, release)
            for p in m["parts"]:
                ppath = work / p["name"]
                if have.get(p["name"]) != p["size"]:
                    log(f"upload {p['name']}")
                    gh.upload(repo, release, str(ppath))
                    uploaded.append(p["name"])
                ppath.unlink(missing_ok=True)
            whole.unlink(missing_ok=True)
            done_paths.add(it["path"])
        else:
            dest = work / f["asset"]
            log(f"fetch {it['path']} ({f['size']} bytes)")
            _download_range(url, dest, 0, f["size"], token)
            digest = _sha256(dest)
            if f.get("sha256") and digest != f["sha256"]:
                raise ValueError(f"{it['path']}: sha256 {digest[:12]}… != Hub's "
                                 f"{f['sha256'][:12]}… — refusing to upload")
            f["sha256"] = digest
            log(f"upload {f['asset']}")
            gh.upload(repo, release, str(dest))
            uploaded.append(f["asset"])
            dest.unlink(missing_ok=True)
    mirrorset.validate(data)
    report["manifest_status"] = "uploaded"
    _put_manifest(data, repo, release, work, seal=seal, seal_key=seal_key)
    final = gh.asset_sizes(repo, release)
    still = mirrorset.plan_missing(data, final)
    report.update({
        "lane": "local", "uploaded": uploaded,
        "sha_known": sum(1 for f in data["files"] if f.get("sha256")),
        "sha_unknown": sum(1 for f in data["files"] if not f.get("sha256")),
        "status": "verified" if not still else "incomplete",
        "items_remaining": len(still),
    })
    if still:
        raise ValueError(f"{len(still)} items still missing after upload: "
                         f"{[s['asset'] for s in still[:5]]}")
    return report
