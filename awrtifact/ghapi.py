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
    data = p.read_bytes()
    url = f"{upload_url}?name={urllib.parse.quote(name)}"
    _request("POST", url, data, content_type="application/octet-stream")


def download_asset(repo: str, release: str, name: str, dest: str) -> int:
    """Download one asset through the API (works for PRIVATE repos, where the
    browser_download_url answers 404 without auth). Returns bytes written."""
    for a in _assets(repo, release):
        if a.get("name") == name:
            url = f"{API}/repos/{repo}/releases/assets/{a['id']}"
            _, _, raw = _request("GET", url, accept="application/octet-stream")
            if not isinstance(raw, (bytes, bytearray)):
                raw = json.dumps(raw).encode("utf-8") if raw is not None else b""
            Path(dest).write_bytes(raw)
            return len(raw)
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
    return {"url": data.get("html_url", f"https://github.com/{repo}"),
            "default_branch": data.get("default_branch", "main"),
            "private": bool(data.get("private", True))}


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
