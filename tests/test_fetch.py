"""awrtifact fetch: base-URL and full-URL forms both land the named asset; a 404 is a
refusal with a non-zero exit, never a silent success.

Measured 2026-09-02 from inside a fleet container: `awrtifact fetch NAME --url
https://artifact.aitherium.com/` (the README's documented form) fetched the BASE
verbatim, got the worker's 404 for the root, and the caller saw an empty output dir.
"""
from __future__ import annotations

import hashlib
import http.server
import socketserver
import sys
import threading
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awrtifact import cli  # noqa: E402
from awrtifact import fetch as fetch_mod  # noqa: E402

BODY = b"{\"hidden_size\": 1024}\n" * 40


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def _serve(self, head_only: bool) -> None:
        if self.path != "/aither-code-embed.config.json":
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            data = BODY[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(BODY) - 1}/{len(BODY)}")
        else:
            data = BODY
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)


@pytest.fixture()
def server():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()


def test_base_url_form_lands_the_named_asset(server, tmp_path):
    r = fetch_mod.fetch("aither-code-embed.config.json", server, tmp_path, expected=len(BODY),
                        lockfile=tmp_path / "lock.json")
    assert r["status"] == "fetched"
    assert (tmp_path / "aither-code-embed.config.json").read_bytes() == BODY
    assert r["sha256"] == hashlib.sha256(BODY).hexdigest()


def test_full_url_form_still_works(server, tmp_path):
    r = fetch_mod.fetch("aither-code-embed.config.json", server + "aither-code-embed.config.json",
                        tmp_path, expected=len(BODY), lockfile=tmp_path / "lock.json")
    assert r["status"] == "fetched"
    assert (tmp_path / "aither-code-embed.config.json").read_bytes() == BODY


def test_missing_asset_is_a_refusal_not_an_empty_dir(server, tmp_path):
    with pytest.raises(fetch_mod.FetchError, match="404"):
        fetch_mod.fetch("nope.json", server, tmp_path, expected=None,
                        lockfile=tmp_path / "lock.json")
    assert not (tmp_path / "nope.json").exists()
    # and the CLI surfaces it as a non-zero exit -- a shell `set -e` must stop here
    rc = cli.main(["fetch", "nope.json", "--url", server, "--out", str(tmp_path),
                   "--lock", str(tmp_path / "lock.json")])
    assert rc != 0
