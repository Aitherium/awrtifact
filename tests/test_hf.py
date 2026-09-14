"""hf.py — reference parsing and the paginated tree walk, offline.

The walk is the part with a silent failure mode (a reader that stops at the
first page mirrors a partial repo and reports success), so the fake Hub
here serves TWO pages behind a Link header and the test asserts both landed.
"""

from __future__ import annotations

import pytest

from awrtifact import hf


@pytest.mark.parametrize("src,kind,repo,rev,sub", [
    ("hf://org/name", "model", "org/name", "main", ""),
    ("hf://org/name@v1.2", "model", "org/name", "v1.2", ""),
    ("hf://datasets/org/name@abc/sub/dir", "dataset", "org/name", "abc", "sub/dir"),
    ("hf://spaces/org/name", "space", "org/name", "main", ""),
    ("https://huggingface.co/org/name", "model", "org/name", "main", ""),
    ("https://huggingface.co/org/name/", "model", "org/name", "main", ""),
    ("https://huggingface.co/datasets/org/name/tree/main/data", "dataset", "org/name",
     "main", "data"),
    ("https://huggingface.co/org/name/tree/refs%2Fpr%2F3", "model", "org/name",
     "refs/pr/3", ""),
    ("org/name", "model", "org/name", "main", ""),
])
def test_parse_ref_forms(src, kind, repo, rev, sub):
    ref = hf.parse_ref(src)
    assert (ref.kind, ref.repo_id, ref.revision, ref.subpath) == (kind, repo, rev, sub)


@pytest.mark.parametrize("src", [
    "https://huggingface.co/org/name/resolve/main/model.gguf",
    "https://huggingface.co/org/name/blob/main/config.json",
    "https://example.com/org/name",
    "./local/file.bin",
    "hf://org",
    "hf://org/name@",
])
def test_parse_ref_refuses_non_repo(src):
    with pytest.raises(hf.HfError):
        hf.parse_ref(src)


def test_is_repo_ref_keeps_resolve_urls_on_the_single_file_lane():
    assert hf.is_repo_ref("hf://org/name")
    assert hf.is_repo_ref("https://huggingface.co/datasets/org/name")
    assert hf.is_repo_ref("org/name")
    assert not hf.is_repo_ref("https://huggingface.co/org/name/resolve/main/x.gguf")
    assert not hf.is_repo_ref("https://example.com/x.gguf")
    assert not hf.is_repo_ref("model.gguf")
    assert not hf.is_repo_ref("C:/models/x.gguf")


def test_resolve_url_pins_commit_and_quotes_paths():
    ref = hf.parse_ref("hf://datasets/org/name@main")
    url = ref.resolve_url("sub dir/file name.txt", revision="abc123")
    assert url == "https://huggingface.co/datasets/org/name/resolve/abc123/sub%20dir/file%20name.txt"


def _fake_hub(pages: dict, info: dict):
    calls = []

    def fetch(url, token=None):
        calls.append(url)
        if url in pages:
            body, nxt = pages[url]
            return body, ({"link": f"<{nxt}>; rel=\"next\""} if nxt else {})
        if url.endswith("/api/models/org/name") or "/revision/" in url:
            return info, {}
        raise AssertionError(f"unexpected url {url}")
    return fetch, calls


def test_list_tree_walks_every_page():
    base = "https://huggingface.co/api/models/org/name/tree/deadbeef?recursive=true&expand=false&limit=1000"
    p2 = base + "&cursor=XYZ"
    pages = {
        base: ([{"type": "file", "oid": "a" * 40, "size": 10, "path": "config.json"},
                {"type": "directory", "oid": "d" * 40, "path": "onnx"},
                {"type": "file", "oid": "b" * 40, "size": 3000,
                 "lfs": {"oid": "c" * 64, "size": 3000}, "path": "onnx/model.onnx"}], p2),
        p2: ([{"type": "file", "oid": "e" * 40, "size": 0, "path": "README.md"}], None),
    }
    fetch, calls = _fake_hub(pages, {"sha": "deadbeef", "private": False})
    ref = hf.parse_ref("hf://org/name")
    files = hf.list_tree(ref, fetch=fetch, revision="deadbeef")
    assert [f.path for f in files] == ["README.md", "config.json", "onnx/model.onnx"]
    by = {f.path: f for f in files}
    assert by["onnx/model.onnx"].sha256 == "c" * 64
    assert by["config.json"].sha256 is None
    assert len(calls) == 2, "the cursor page was not followed"


def test_list_tree_refuses_malformed_entries():
    base = "https://huggingface.co/api/models/org/name/tree/main?recursive=true&expand=false&limit=1000"
    fetch, _ = _fake_hub({base: ([{"type": "file", "path": "x"}], None)}, {"sha": "x"})
    with pytest.raises(hf.HfError):
        hf.list_tree(hf.parse_ref("hf://org/name"), fetch=fetch)


def test_repo_info_requires_commit_sha():
    fetch, _ = _fake_hub({}, {"id": "org/name"})
    with pytest.raises(hf.HfError):
        hf.repo_info(hf.parse_ref("hf://org/name"), fetch=fetch)
    fetch, _ = _fake_hub({}, {"sha": "abc", "private": True, "gated": "auto"})
    info = hf.repo_info(hf.parse_ref("hf://org/name"), fetch=fetch)
    assert info["commit"] == "abc" and info["private"] is True and info["gated"] == "auto"
