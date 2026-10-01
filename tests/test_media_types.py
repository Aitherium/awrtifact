"""Media served from the store carries a media Content-Type (2026-10-01).

The worker answered `application/octet-stream` for everything that was not JS, WASM or
JSON, so a film published to artifact.aitherium.com would download instead of play in a
`<video>` / direct link. Executed under Node against the rendered worker: an .mp4 is
`video/mp4`, a .jpg is `image/jpeg`, an .srt is text, and a GGUF stays octet-stream.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from awrtifact import serve_spec


def _spec() -> dict:
    return {
        "store": {"name": "awrtifact", "repo": "Aitherium/aitherkvcache"},
        "workers": [{"name": "awrtifact", "dir": "awrtifact", "route": "artifact.aitherium.com/*",
                     "r2_binding": "WEIGHTS", "r2_bucket": "aither-artifacts"}],
        "upstreams": [{"release": "media-v1"}],
        "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.(gguf|mp4|jpg|srt)$"},
        "artifacts": [],
    }


_HARNESS = r"""
import worker from './index.mjs';
globalThis.fetch = async () => new Response(new Uint8Array([1, 2, 3, 4]),
  {status: 200, headers: {'Content-Length': '4', 'Content-Type': 'application/octet-stream'}});
const out = {};
for (const n of ['film.mp4', 'thumb.jpg', 'film.srt', 'model.gguf']) {
  const r = await worker.fetch(new Request('https://artifact.aitherium.com/' + n), {});
  out[n] = [r.status, r.headers.get('Content-Type')];
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_media_files_get_media_types(tmp_path):
    js, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    (tmp_path / "index.mjs").write_text(js, encoding="utf-8")
    (tmp_path / "h.mjs").write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", "h.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["film.mp4"] == [200, "video/mp4"]
    assert got["thumb.jpg"] == [200, "image/jpeg"]
    assert got["film.srt"][1].startswith("text/plain")
    assert got["model.gguf"] == [200, "application/octet-stream"]
