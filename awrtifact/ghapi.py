"""GitHub REST backend — the same operations `gh.py` shells, over urllib.

Why a second backend exists: server-side callers (routers, background
workers) run in containers that carry no `gh` binary, and the "mirror
anything from Hugging Face to any GitHub repo" story has to work from
there — a tool that can only run on the operator's laptop is not a
platform capability. Everything here is keyed on ONE token
(`GH_TOKEN`, then `GITHUB_TOKEN`, or an explicit `token_override` for a
server process that must never write into `os.environ`).

`gh.py` decides which backend a call takes (`AWRTIFACT_GH_BACKEND`:
`cli` | `api` | `auto`). Nothing else imports this module directly.

Every write returns only after GitHub has acknowledged it; a 4xx/5xx is a
`GhApiError` carrying the status and GitHub's message — never a silent
`None`, because a mirror whose release "exists" only in a swallowed error
uploads parts into nothing (the SILENCE class the rest of this package is
written against).
"""

from __future__ import annotations

import contextvars
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
_USER_AGENT = "awrtifact-ghapi/1"

token_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "awrtifact_gh_token", default=None
)


class GhApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def token() -> str | None:
    return token_override.get() or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")


def _request(method: str, url: str, body: bytes | None = None,
             content_type: str = "application/json",
             accept: str = "application/vnd.github+json") -> tuple[int, dict, object]:
    tok = token()
    if not tok:
        raise GhApiError(0, "no GitHub token (GH_TOKEN / GITHUB_TOKEN) for the API backend")
    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": accept,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
            raw = resp.read()
            status = resp.status
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(2048)
        except Exception:  # noqa: BLE001 — the status is the finding; the body is detail
            raw = b""
        msg = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("message", msg)
        except ValueError:
            msg = msg.strip()  # GitHub sent a non-JSON body; keep it verbatim
        raise GhApiError(exc.code, f"{method} {url}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise GhApiError(0, f"{method} {url}: unreachable ({exc.reason})") from exc
    parsed: object = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            parsed = raw
    return status, hdrs, parsed


def _get(url: str) -> object:
    return _request("GET", url)[2]


_STREAM_CHUNK = 1024 * 1024
_ASSET_HOSTS = ("api.github.com", "uploads.github.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends EVERY header on a redirect, Authorization included, to
    whatever host the Location names. Asset downloads 302 to a presigned
    storage URL: follow those by hand, without the token."""

    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003 — urllib signature
        return None


def _auth_headers(accept: str) -> dict[str, str]:
    tok = token()
    if not tok:
        raise GhApiError(0, "no GitHub token (GH_TOKEN / GITHUB_TOKEN) for the API backend")
    return {"Authorization": f"Bearer {tok}", "Accept": accept,
            "User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}


def _stream_to(resp, dest: str) -> int:
    written = 0
    with open(dest, "wb") as fh:
        for block in iter(lambda: resp.read(_STREAM_CHUNK), b""):
            fh.write(block)
            written += len(block)
    return written


def _download_stream(url: str, dest: str) -> int:
    """GET `url` into `dest` in 1 MiB blocks. The bearer goes ONLY to a GitHub
    API host; a redirect to storage is followed without it."""
    opener = urllib.request.build_opener(_NoRedirect)
    headers = _auth_headers("application/octet-stream")
    location = url
    for _ in range(5):
        host = urllib.parse.urlsplit(location).hostname or ""
        hdrs = dict(headers) if host in _ASSET_HOSTS else {"User-Agent": _USER_AGENT}
        req = urllib.request.Request(location, method="GET", headers=hdrs)
        try:
            with opener.open(req, timeout=300) as resp:  # noqa: S310
                return _stream_to(resp, dest)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                location = urllib.parse.urljoin(location, exc.headers["Location"])
                continue
            where = location.split("?", 1)[0]
            raise GhApiError(exc.code, f"GET {where}: download failed") from exc
        except urllib.error.URLError as exc:
            raise GhApiError(0, f"GET {location.split('?', 1)[0]}: unreachable "
                                f"({exc.reason})") from exc
    raise GhApiError(0, f"GET {url}: too many redirects")


def _upload_stream(url: str, path: Path) -> None:
    """POST a file as the request body without reading it into memory."""
    headers = _auth_headers("application/vnd.github+json")
    headers["Content-Type"] = "application/octet-stream"
    headers["Content-Length"] = str(path.stat().st_size)
    with path.open("rb") as fh:
        req = urllib.request.Request(url, data=fh, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:  # noqa: S310
                resp.read()
        except urllib.error.HTTPError as exc:
            try:
                msg = exc.read(2048).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — the status is the finding; the body is detail
                msg = ""
            raise GhApiError(exc.code, f"POST {url}: {msg.strip()}") from exc
        except urllib.error.URLError as exc:
            raise GhApiError(0, f"POST {url}: unreachable ({exc.reason})") from exc


def _paged(url: str) -> list:
    """Follow `Link: rel="next"` to the end; a paged list read once is a lie."""
    out: list = []
    nxt: str | None = url
    for _ in range(1000):
        if not nxt:
            break
        _, hdrs, data = _request("GET", nxt)
        if isinstance(data, list):
            out.extend(data)
        nxt = None
        for part in hdrs.get("link", "").split(","):
            seg = part.strip()
            if 'rel="next"' in seg and seg.startswith("<"):
                nxt = seg[1:seg.index(">")]
    return out


# ---------------------------------------------------------------------------
# releases
# ---------------------------------------------------------------------------

def _release(repo: str, release: str) -> dict | None:
    try:
        data = _get(f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(release)}")
    except GhApiError as exc:
        if exc.status == 404:
            return None
        raise
    return data if isinstance(data, dict) else None


def list_releases(repo: str) -> list[str]:
    """Every release tag in the repo, in GitHub's order (newest first)."""
    rows = _paged(f"{API}/repos/{repo}/releases?per_page=100")
    return [r["tag_name"] for r in rows if isinstance(r, dict) and r.get("tag_name")]


def release_exists(repo: str, release: str) -> bool:
    return _release(repo, release) is not None


def _assets(repo: str, release: str) -> list[dict]:
    rel = _release(repo, release)
    if rel is None:
        return []
    rid = rel["id"]
    return [a for a in _paged(f"{API}/repos/{repo}/releases/{rid}/assets?per_page=100")
            if isinstance(a, dict)]


def release_assets(repo: str, release: str) -> set[str]:
    return {a["name"] for a in _assets(repo, release) if "name" in a}


def asset_sizes(repo: str, release: str) -> dict[str, int]:
    return {a["name"]: int(a.get("size", 0)) for a in _assets(repo, release) if "name" in a}


def create_release(repo: str, release: str, title: str, notes: str) -> None:
    body = json.dumps({"tag_name": release, "name": title, "body": notes,
                       "draft": False, "prerelease": False}).encode("utf-8")
    _request("POST", f"{API}/repos/{repo}/releases", body)


def upload(repo: str, release: str, path: str) -> None:
    """Upload one asset, replacing a same-named one (gh's --clobber)."""
    rel = _release(repo, release)
    if rel is None:
        raise GhApiError(404, f"release {release} does not exist in {repo}")
    p = Path(path)
    name = p.name
    for a in _assets(repo, release):
        if a.get("name") == name:
            _request("DELETE", f"{API}/repos/{repo}/releases/assets/{a['id']}")
            break
    upload_url = rel.get("upload_url", "").split("{", 1)[0]
    if not upload_url:
        upload_url = f"{UPLOADS}/repos/{repo}/releases/{rel['id']}/assets"
    url = f"{upload_url}?name={urllib.parse.quote(name)}"
    _upload_stream(url, p)


def download_asset(repo: str, release: str, name: str, dest: str) -> int:
    """Download one asset through the API (works for PRIVATE repos, where the
    browser_download_url answers 404 without auth), streamed to `dest`; the
    token never follows the redirect to storage. Returns bytes written."""
    for a in _assets(repo, release):
        if a.get("name") == name:
            return _download_stream(f"{API}/repos/{repo}/releases/assets/{a['id']}", dest)
    raise GhApiError(404, f"asset {name} not in release {release} of {repo}")


# ---------------------------------------------------------------------------
# workflows / repos / contents
# ---------------------------------------------------------------------------

def workflow_exists(repo: str, workflow: str) -> bool:
    try:
        _get(f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}")
    except GhApiError as exc:
        if exc.status == 404:
            return False
        raise
    return True


def workflow_dispatch(repo: str, workflow: str, inputs: dict[str, str],
                      ref: str | None = None) -> None:
    if ref is None:
        info = repo_view(repo) or {}
        ref = info.get("default_branch") or "main"
    body = json.dumps({"ref": ref, "inputs": {k: str(v) for k, v in inputs.items()}})
    _request("POST",
             f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/dispatches",
             body.encode("utf-8"))


def repo_view(repo: str) -> dict | None:
    try:
        data = _get(f"{API}/repos/{repo}")
    except GhApiError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data, dict):
        return None
    # `visibility` is authoritative (internal is NOT private); a bare `private`
    # counts only when it is literally true/false; neither = unknown (None),
    # which a privacy guard must treat as a refusal.
    vis = str(data.get("visibility") or "").lower()
    if vis:
        private = vis == "private"
    elif isinstance(data.get("private"), bool):
        private = data["private"]
    else:
        private = None
    return {"url": data.get("html_url", f"https://github.com/{repo}"),
            "default_branch": data.get("default_branch", "main"),
            "private": private}


def auth_login() -> str:
    me = _get(f"{API}/user")
    return str(me.get("login") or "") if isinstance(me, dict) else ""


def org_role(org: str) -> str | None:
    try:
        data = _get(f"{API}/user/memberships/orgs/{urllib.parse.quote(org)}")
    except GhApiError as exc:
        if exc.status in (403, 404):
            return None
        raise
    if not isinstance(data, dict) or data.get("state") not in (None, "active"):
        return None
    return str(data.get("role") or "") or None


def repo_create(repo: str, public: bool) -> None:
    owner, name = repo.split("/", 1)
    me = _get(f"{API}/user")
    login = me.get("login", "") if isinstance(me, dict) else ""
    payload = json.dumps({"name": name, "private": not public, "auto_init": True})
    if login and login.lower() == owner.lower():
        _request("POST", f"{API}/user/repos", payload.encode("utf-8"))
    else:
        _request("POST", f"{API}/orgs/{owner}/repos", payload.encode("utf-8"))


def get_file(repo: str, path: str) -> dict | None:
    """{"sha", "content"(bytes)} or None when absent."""
    try:
        data = _get(f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}")
    except GhApiError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data, dict) or "sha" not in data:
        return None
    import base64
    content = base64.b64decode(data.get("content", "") or b"")
    return {"sha": data["sha"], "content": content}


def put_file(repo: str, path: str, content: str, message: str,
             only_if_absent: bool = True) -> str:
    """Create (or update) a file via the contents API. Returns
    created | present | updated."""
    import base64
    existing = get_file(repo, path)
    if existing is not None and only_if_absent:
        return "present"
    payload: dict = {"message": message,
                     "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
    if existing is not None:
        payload["sha"] = existing["sha"]
    _request("PUT", f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}",
             json.dumps(payload).encode("utf-8"))
    return "updated" if existing is not None else "created"


def enable_pages(repo: str, branch: str = "main", path: str = "/") -> bool:
    try:
        _request("POST", f"{API}/repos/{repo}/pages",
                 json.dumps({"source": {"branch": branch, "path": path}}).encode("utf-8"))
    except GhApiError as exc:
        if exc.status == 409:  # already enabled
            return True
        return False
    return True
