"""ghapi.py + the backend selector — the API lane a container without `gh`
takes. Faked at `_request` so the shapes GitHub expects are pinned: a
clobbering upload deletes the old asset first, a dispatch names the default
branch, put_file leaves an existing file alone, and a paged asset list is
walked to the end."""

from __future__ import annotations

import json

import pytest

from awrtifact import gh, ghapi


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.assets = [{"id": 1, "name": "old.bin", "size": 5}]
        self.release = {"id": 77, "tag_name": "rel",
                        "upload_url": "https://uploads.github.com/repos/o/r/releases/77/assets{?name,label}"}
        self.file_present = False

    def request(self, method, url, body=None, content_type="application/json", accept=""):
        self.calls.append((method, url, body))
        if url.endswith("/releases/tags/rel"):
            return 200, {}, self.release
        if url.endswith("/releases/tags/missing"):
            raise ghapi.GhApiError(404, "nope")
        if "/releases/77/assets?per_page=100" in url and "page=2" not in url:
            return 200, {"link": f"<{url}&page=2>; rel=\"next\""}, self.assets[:1]
        if "page=2" in url:
            return 200, {}, [{"id": 2, "name": "second.bin", "size": 9}]
        if method == "DELETE":
            return 204, {}, None
        if url.startswith("https://uploads.github.com"):
            return 201, {}, {"id": 3}
        if url.endswith("/repos/o/r"):
            return 200, {}, {"html_url": "https://github.com/o/r", "default_branch": "dev",
                             "private": True}
        if "/dispatches" in url:
            if "boom" in url:
                raise ghapi.GhApiError(422, "no such workflow")
            return 204, {}, None
        if "/contents/" in url and method == "GET":
            if self.file_present:
                return 200, {}, {"sha": "abc", "content": ""}
            raise ghapi.GhApiError(404, "absent")
        if "/contents/" in url and method == "PUT":
            return 201, {}, {"content": {"sha": "new"}}
        if url.endswith("/actions/workflows/x.yml"):
            return 200, {}, {"id": 1}
        if url.endswith("/actions/workflows/none.yml"):
            raise ghapi.GhApiError(404, "no")
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(ghapi, "_request", fake.request)
    monkeypatch.setenv("GH_TOKEN", "t")
    return fake


def test_asset_listing_walks_pages(api):
    assert ghapi.asset_sizes("o/r", "rel") == {"old.bin": 5, "second.bin": 9}
    assert ghapi.release_assets("o/r", "missing") == set()
    assert ghapi.release_exists("o/r", "rel") and not ghapi.release_exists("o/r", "missing")


def test_upload_clobbers_then_posts_bytes(api, tmp_path):
    p = tmp_path / "old.bin"
    p.write_bytes(b"hello")
    ghapi.upload("o/r", "rel", str(p))
    methods = [(m, u) for m, u, _ in api.calls]
    assert ("DELETE", "https://api.github.com/repos/o/r/releases/assets/1") in methods
    posts = [c for c in api.calls if c[0] == "POST" and c[1].startswith("https://uploads")]
    assert posts and posts[0][1].endswith("?name=old.bin") and posts[0][2] == b"hello"


def test_dispatch_targets_default_branch(api):
    ghapi.workflow_dispatch("o/r", "x.yml", {"release": "rel", "n": 3})
    body = json.loads([c for c in api.calls if "/dispatches" in c[1]][0][2])
    assert body == {"ref": "dev", "inputs": {"release": "rel", "n": "3"}}
    assert ghapi.workflow_exists("o/r", "x.yml") and not ghapi.workflow_exists("o/r", "none.yml")


def test_put_file_leaves_existing_alone(api):
    assert ghapi.put_file("o/r", "a.yml", "x", "m") == "created"
    api.file_present = True
    assert ghapi.put_file("o/r", "a.yml", "x", "m") == "present"
    assert ghapi.put_file("o/r", "a.yml", "x", "m", only_if_absent=False) == "updated"


def test_no_token_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ghapi.GhApiError, match="no GitHub token"):
        ghapi.release_exists("o/r", "rel")


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "api")
    assert gh.backend() == "api"
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "cli")
    assert gh.backend() == "cli"
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "auto")
    monkeypatch.setattr(gh.shutil, "which", lambda _: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(gh.GhError, match="no `gh` on PATH"):
        gh.backend()
    monkeypatch.setenv("GH_TOKEN", "t")
    assert gh.backend() == "api"
    monkeypatch.setattr(gh.shutil, "which", lambda _: "/usr/bin/gh")
    assert gh.backend() == "cli"


def test_gh_routes_to_api_and_wraps_errors(api, monkeypatch):
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "api")
    assert gh.asset_sizes("o/r", "rel") == {"old.bin": 5, "second.bin": 9}
    with pytest.raises(gh.GhError):
        gh.workflow_dispatch("o/r", "boom.yml", {})
