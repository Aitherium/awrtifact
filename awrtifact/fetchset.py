"""`awrtifact fetch-set` — restore a mirrored Hugging Face repo from a release.

Reads `<set>.mirrorset.json` from the release, then fetches every file back
into its ORIGINAL path (the asset name is a flattened alias, never the
destination), stitching `.partN` slices for the chunked ones. Every file is
size-checked; every file whose sha256 the set knows (the Hub's LFS oid, or
the local lane's read) is hash-checked — against the ORIGIN's digest, so a
"verified" here means "identical to what Hugging Face served", not merely
"identical to what we uploaded".

A file with no known sha256 is reported `unverified`, counted, and printed.
The report never says verified on a size match alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import fetch as fetch_mod
from . import mirrorset, sealing

_CHUNK = 8 * 1024 * 1024


def release_asset_url(repo: str, release: str, asset: str) -> str:
    return (f"https://github.com/{repo}/releases/download/"
            f"{urllib.parse.quote(release, safe='')}/{urllib.parse.quote(asset)}")


def _download(url: str, dest: Path, expected: int, token: str | None = None) -> None:
    """Resumable download to `dest` at exactly `expected` bytes.

    The public browser_download_url is tried first (no credential needed for
    a public mirror). A PRIVATE repo answers 404 there; then the same asset is
    fetched through `gh` / the API with the operator's credential -- a private
    target must not read as "asset missing".
    """
    last: Exception | None = None
    for _ in range(3):
        try:
            have = fetch_mod._download_once(url, dest, expected)
        except fetch_mod.FetchError as exc:
            last = exc
            if "HTTP 404" in str(exc) or "HTTP 403" in str(exc):
                break
            continue
        if have == expected:
            return
        if have > expected:
            dest.unlink(missing_ok=True)
    repo, release, name = _split_asset_url(url)
    if repo:
        from . import gh  # noqa: PLC0415
        try:
            got = gh.download_asset(repo, release, name, str(dest))
        except gh.GhError as exc:
            raise fetch_mod.FetchError(f"{name}: {last}; and via gh: {exc}") from exc
        if got == expected:
            return
        raise fetch_mod.FetchError(f"{name}: got {got} bytes via gh, wanted {expected}")
    raise fetch_mod.FetchError(f"{dest.name}: {last or 'short read'}")


def _split_asset_url(url: str) -> tuple[str, str, str]:
    """github.com/<owner>/<repo>/releases/download/<tag>/<asset> -> parts."""
    m = re.match(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$", url)
    if not m:
        return "", "", ""
    return m.group(1), urllib.parse.unquote(m.group(2)), urllib.parse.unquote(m.group(3))


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_asset(repo: str, release: str, asset: str, opener=None, cap: int = 64 << 20) -> bytes:
    url = release_asset_url(repo, release, asset)
    req = urllib.request.Request(url, headers={"User-Agent": fetch_mod.UA})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=60) as resp:  # noqa: S310
            return resp.read(cap)
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 404) or opener is not None:
            raise
    # Private repo: the public URL refuses; go through gh / the API.
    import tempfile  # noqa: PLC0415

    from . import gh  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="awrtifact-set-") as td:
        dest = Path(td) / asset
        gh.download_asset(repo, release, asset, str(dest))
        return dest.read_bytes()[:cap]


def load_set_from_release(repo: str, release: str, name: str | None = None,
                          opener=None, expect_key: str | None = None) -> dict:
    """Fetch the mirror set manifest. `name` is the set name (asset stem) —
    when omitted, the release must carry exactly one *.mirrorset.json.

    When the release also carries `<set>.awseal.json` the seal is verified
    and the verdict attached as `data["_seal"]`; with `expect_key` a seal
    that does not verify against that key REFUSES the set (raises). A set
    with no seal and an `expect_key` also refuses — "unsigned" is not
    "signed by you".
    """
    from . import gh
    assets = gh.release_assets(repo, release)
    if name is None:
        cands = sorted(a for a in assets if a.endswith(mirrorset.SET_SUFFIX))
        if len(cands) != 1:
            raise mirrorset.MirrorSetError(
                f"release {release} carries {len(cands)} mirror sets — pass --name: {cands}")
        asset = cands[0]
    else:
        asset = name if name.endswith(mirrorset.SET_SUFFIX) else name + mirrorset.SET_SUFFIX
    raw = _read_asset(repo, release, asset, opener)
    data = mirrorset.validate(json.loads(raw.decode("utf-8")))
    seal_name = sealing.seal_asset(data)
    if seal_name in assets:
        verdict = sealing.verify_set(data, _read_asset(repo, release, seal_name, opener),
                                     expect_key=expect_key)
        data["_seal"] = verdict
        if expect_key is not None and not verdict.get("ok"):
            raise mirrorset.MirrorSetError(
                f"seal on {asset} does not verify against the expected key: "
                f"{verdict.get('error') or verdict}")
    elif expect_key is not None:
        raise mirrorset.MirrorSetError(f"{asset} carries no seal — refusing with --expect-key")
    return data


def fetch_set(data: dict, dest: Path, *, only: list[str] | None = None,
              downloader=_download) -> dict:
    """Materialize the set under `dest`. Returns a per-file report:
    {"fetched": n, "up_to_date": n, "unverified": [paths], "failed": [(path, why)],
     "bytes": n, "files": n}."""
    repo = data["target"]["repo"]
    release = data["target"]["release"]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    report = {"fetched": 0, "up_to_date": 0, "unverified": [], "failed": [],
              "bytes": 0, "files": 0, "dest": str(dest)}
    wanted = set(only or [])
    for f in data["files"]:
        if wanted and f["path"] not in wanted:
            continue
        report["files"] += 1
        out = dest / Path(*f["path"].split("/"))
        if ".." in f["path"].split("/") or out.resolve().parent != out.parent.resolve() \
                or not str(out.resolve()).startswith(str(dest.resolve())):
            report["failed"].append((f["path"], "path escapes destination"))
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_file() and out.stat().st_size == f["size"] and f.get("sha256") \
                and _sha256_of(out) == f["sha256"]:
            report["up_to_date"] += 1
            report["bytes"] += f["size"]
            continue
        try:
            if f["size"] == 0:
                out.write_bytes(b"")
            elif f["parts"]:
                tmp = out.with_name(out.name + ".partial")
                with open(tmp, "wb") as sink:
                    for p in f["parts"]:
                        part_path = out.parent / p["name"]
                        downloader(release_asset_url(repo, release, p["name"]),
                                   part_path, p["size"])
                        with open(part_path, "rb") as src:
                            while True:
                                chunk = src.read(_CHUNK)
                                if not chunk:
                                    break
                                sink.write(chunk)
                        part_path.unlink()
                tmp.replace(out)
            else:
                downloader(release_asset_url(repo, release, f["asset"]), out, f["size"])
        except (fetch_mod.FetchError, OSError) as exc:
            report["failed"].append((f["path"], str(exc)))
            continue
        got = out.stat().st_size
        if got != f["size"]:
            report["failed"].append((f["path"], f"size {got} != {f['size']}"))
            continue
        if f.get("sha256"):
            have = _sha256_of(out)
            if have != f["sha256"]:
                report["failed"].append((f["path"], f"sha256 {have[:12]}… != {f['sha256'][:12]}…"))
                out.unlink(missing_ok=True)
                continue
        else:
            report["unverified"].append(f["path"])
        report["fetched"] += 1
        report["bytes"] += f["size"]
    report["ok"] = not report["failed"]
    return report
