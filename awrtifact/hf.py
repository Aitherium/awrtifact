"""Hugging Face repo enumeration — the source half of `awrtifact mirror hf://…`.

Stdlib only. Turns a repo reference into the exact list of files (path, size,
and — for LFS files — the sha256 Hugging Face already publishes as the LFS
oid) plus the commit the listing was taken at, so a mirror set is pinned to
a revision rather than to "whatever main was when the runner fetched it".

Accepted references (all resolve to the same shape):

    hf://org/name                       model, main
    hf://org/name@<rev>                 model at a branch / tag / commit
    hf://datasets/org/name[@rev]        dataset
    hf://spaces/org/name[@rev]          space
    hf://org/name@rev/sub/dir           only files under sub/dir
    https://huggingface.co/org/name
    https://huggingface.co/datasets/org/name/tree/<rev>/sub/dir
    org/name                            bare id (models only; must contain one '/')

A `/resolve/` URL is a single FILE, not a repo — `is_repo_ref` answers False
so `awrtifact mirror` keeps routing those through the single-URL lane.

Two facts about the API that decide the shape of this module (measured
2026-09-06 against hf-internal-testing/tiny-random-gpt2):

* `GET /api/{models|datasets|spaces}/{id}/tree/{rev}?recursive=true` pages by
  a `Link: <…cursor=…>; rel="next"` header, NOT by a body field. A reader that
  stops at the first body silently mirrors the first page of a big repo and
  reports success — so the cursor walk here is unconditional.
* Non-LFS files carry only a git blob `oid` (sha1). Their sha256 is unknown
  until the bytes are read; the mirror set records `sha256: null` for them
  and the LOCAL lane fills it in. A fetch that cannot verify a file says so —
  it never reports "verified" on a size match alone.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

HF_HOST = "https://huggingface.co"
_USER_AGENT = "awrtifact-hf/1"
_KINDS = ("model", "dataset", "space")
_API_KIND = {"model": "models", "dataset": "datasets", "space": "spaces"}
_URL_PREFIX = {"model": "", "dataset": "datasets/", "space": "spaces/"}
_PAGE_LIMIT = 1000
_MAX_PAGES = 10_000

_BARE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class HfError(RuntimeError):
    """The Hub answered something a mirror cannot proceed on."""


@dataclass(frozen=True)
class HfRef:
    kind: str = "model"
    repo_id: str = ""
    revision: str = "main"
    subpath: str = ""

    @property
    def api_base(self) -> str:
        return f"{HF_HOST}/api/{_API_KIND[self.kind]}/{self.repo_id}"

    @property
    def html_url(self) -> str:
        return f"{HF_HOST}/{_URL_PREFIX[self.kind]}{self.repo_id}"

    def resolve_url(self, path: str, revision: str | None = None) -> str:
        rev = urllib.parse.quote(revision or self.revision, safe="")
        return f"{self.html_url}/resolve/{rev}/{urllib.parse.quote(path)}"

    def as_string(self) -> str:
        prefix = _URL_PREFIX[self.kind]
        out = f"hf://{prefix}{self.repo_id}@{self.revision}"
        if self.subpath:
            out += "/" + self.subpath
        return out


@dataclass(frozen=True)
class HfFile:
    path: str
    size: int
    sha256: str | None = None   # LFS oid when the Hub knows it; else None
    oid: str = ""               # git blob sha1 (always present)
    extra: dict = field(default_factory=dict)


def is_repo_ref(source: str) -> bool:
    """True when `source` names a Hub REPO (not a single /resolve/ file URL)."""
    s = source.strip()
    if s.startswith("hf://"):
        return True
    if s.lower().startswith(("http://", "https://")):
        u = urllib.parse.urlparse(s)
        if u.netloc.lower() not in ("huggingface.co", "www.huggingface.co", "hf.co"):
            return False
        return "/resolve/" not in u.path and "/blob/" not in u.path
    return bool(_BARE_ID_RE.match(s))


def parse_ref(source: str) -> HfRef:
    """Parse any accepted reference form into an HfRef. Raises HfError."""
    s = source.strip()
    if s.startswith("hf://"):
        body = s[len("hf://"):]
    elif s.lower().startswith(("http://", "https://")):
        u = urllib.parse.urlparse(s)
        if u.netloc.lower() not in ("huggingface.co", "www.huggingface.co", "hf.co"):
            raise HfError(f"not a huggingface.co URL: {source}")
        body = u.path.lstrip("/")
        if "/resolve/" in body or "/blob/" in body:
            raise HfError("a /resolve/ or /blob/ URL is one file, not a repo — "
                          "pass it to the single-URL lane")
        # https://huggingface.co/[datasets/]org/name[/tree/rev[/sub]]
        m = re.match(r"^((?:datasets|spaces)/)?([^/]+/[^/]+)(?:/tree/([^/]+)(?:/(.*))?)?/?$",
                     body)
        if not m:
            raise HfError(f"cannot parse a repo id out of {source}")
        prefix, repo_id, rev, sub = m.groups()
        kind = {"datasets/": "dataset", "spaces/": "space"}.get(prefix or "", "model")
        return HfRef(kind, repo_id, urllib.parse.unquote(rev) if rev else "main",
                     (sub or "").strip("/"))
    elif _BARE_ID_RE.match(s):
        return HfRef("model", s, "main", "")
    else:
        raise HfError(f"not a Hugging Face repo reference: {source}")

    kind = "model"
    for k, prefix in _URL_PREFIX.items():
        if prefix and body.startswith(prefix):
            kind, body = k, body[len(prefix):]
            break
    parts = body.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise HfError(f"hf:// reference needs org/name: {source}")
    name = parts[1]
    revision = "main"
    if "@" in name:
        name, revision = name.split("@", 1)
        if not revision:
            raise HfError(f"empty revision in {source}")
    repo_id = f"{parts[0]}/{name}"
    if not _BARE_ID_RE.match(repo_id):
        raise HfError(f"malformed repo id {repo_id!r} in {source}")
    subpath = "/".join(p for p in parts[2:] if p)
    return HfRef(kind, repo_id, revision, subpath)


# ---------------------------------------------------------------------------
# Hub API — injectable fetcher so the walk is testable without a network
# ---------------------------------------------------------------------------

def _fetch_json(url: str, token: str | None = None) -> tuple[object, dict]:
    """GET a JSON document; returns (parsed body, lower-cased headers)."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — best-effort detail only
            detail = ""
        if exc.code in (401, 403):
            raise HfError(f"Hub refused {url} ({exc.code}) — gated/private repo? "
                          f"set HF_TOKEN. {detail}".strip()) from exc
        if exc.code == 404:
            raise HfError(f"Hub has no such repo/revision: {url} (404)") from exc
        raise HfError(f"Hub answered {exc.code} for {url}: {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise HfError(f"Hub unreachable: {exc.reason}") from exc
    try:
        return json.loads(body.decode("utf-8")), hdrs
    except ValueError as exc:
        raise HfError(f"Hub returned non-JSON for {url}") from exc


def _next_link(headers: dict) -> str | None:
    link = headers.get("link", "")
    for part in link.split(","):
        seg = part.strip()
        if 'rel="next"' in seg and seg.startswith("<"):
            return seg[1:seg.index(">")]
    return None


def repo_info(ref: HfRef, token: str | None = None, fetch=_fetch_json) -> dict:
    """The repo's metadata at `ref.revision`: {"commit", "private", "gated", …}."""
    url = ref.api_base
    if ref.revision != "main":
        url = f"{ref.api_base}/revision/{urllib.parse.quote(ref.revision, safe='')}"
    data, _ = fetch(url, token)
    if not isinstance(data, dict) or not data.get("sha"):
        raise HfError(f"Hub repo info for {ref.repo_id} carries no commit sha")
    return {
        "commit": data["sha"],
        "private": bool(data.get("private", False)),
        "gated": data.get("gated", False),
        "id": data.get("id", ref.repo_id),
        "last_modified": data.get("lastModified"),
        "card": (data.get("cardData") or {}),
        "tags": list(data.get("tags") or []),
    }


def list_tree(ref: HfRef, token: str | None = None, fetch=_fetch_json,
              revision: str | None = None) -> list[HfFile]:
    """Every FILE under `ref.subpath` at `revision` (default ref.revision).

    Walks the cursor chain to the end; a repo with more files than one page
    is the common case for datasets, and stopping early is a silent partial
    mirror. Directories are skipped (recursive=true already flattens them).
    """
    rev = urllib.parse.quote(revision or ref.revision, safe="")
    base = f"{ref.api_base}/tree/{rev}"
    if ref.subpath:
        base += "/" + urllib.parse.quote(ref.subpath)
    url: str | None = f"{base}?recursive=true&expand=false&limit={_PAGE_LIMIT}"
    files: list[HfFile] = []
    seen: set[str] = set()
    pages = 0
    while url:
        pages += 1
        if pages > _MAX_PAGES:
            raise HfError("Hub tree pagination did not terminate")
        data, hdrs = fetch(url, token)
        if not isinstance(data, list):
            raise HfError(f"Hub tree answered a non-list for {ref.repo_id}")
        for item in data:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            path = item.get("path")
            size = item.get("size")
            if not isinstance(path, str) or not isinstance(size, int) or size < 0:
                raise HfError(f"Hub tree entry malformed: {item!r}")
            if path in seen:
                continue
            seen.add(path)
            lfs = item.get("lfs") or {}
            sha = lfs.get("oid") if isinstance(lfs, dict) else None
            if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
                sha = None
            files.append(HfFile(path=path, size=size, sha256=sha,
                                oid=str(item.get("oid", "")),
                                extra={"xet": item.get("xetHash")}))
        url = _next_link(hdrs)
    files.sort(key=lambda f: f.path)
    return files
