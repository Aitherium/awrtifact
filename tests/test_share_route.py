"""The opt-in `/s/<share>` byte route in the generated worker.

Two properties, both of which a green build would hide if broken:

1. OPT-IN IS BYTE-EXACT. A worker whose spec entry has no `share_grant_url`
   renders exactly as before the route existed, so shipping the template does
   not redeploy (or change) any worker by itself.
2. THE ROUTE FAILS CLOSED. The rendered JS is executed under Node with a fake
   grant-check and a fake R2 bucket: no ticket is 401, a revoked grant is 410,
   an unreachable grant check is 503 (never a fall-through to serving), an
   object key outside the share's own prefix is refused, and a valid ticket
   streams the ciphertext with Range. Skipped only when Node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from awrtifact import serve_spec, worker_template


def _spec(share_url=None) -> dict:
    worker = {"name": "awrtifact", "dir": "awrtifact", "route": "artifact.aitherium.com/*",
              "r2_binding": "WEIGHTS", "r2_bucket": "aither-artifacts"}
    if share_url:
        worker["share_grant_url"] = share_url
    return {"store": {"name": "awrtifact", "repo": "Aitherium/aitherkvcache"},
            "workers": [worker], "upstreams": [{"release": "artifact-v1"}],
            "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.gguf$"}, "artifacts": []}


def test_absent_share_url_renders_no_share_code(tmp_path, monkeypatch):
    js, _toml = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    assert "serveShare" not in js
    assert "__SHARE" not in js
    # Byte-exact with the pre-route template (the slots removed outright).
    legacy = worker_template.JS_TEMPLATE.replace("__SHARE_ROUTE_JS__", "").replace(
        "__SHARE_ROUTE_DISPATCH__", "")
    monkeypatch.setattr(worker_template, "JS_TEMPLATE", legacy)
    js_legacy, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    assert js == js_legacy


def test_present_share_url_renders_the_route(tmp_path):
    url = "https://api.example.com/api/share/grant-check"
    js, _ = serve_spec.render(_spec(url), tmp_path / "awrtifact.yaml")
    assert f"const SHARE_GRANT_URL = {json.dumps(url)};" in js
    assert "return serveShare(request, env, shareSegs[1]);" in js
    assert "env && env.WEIGHTS" in js
    assert "__SHARE" not in js and "__R2_BINDING__" not in js


def test_non_https_share_url_is_refused(tmp_path):
    with pytest.raises(ValueError, match="https"):
        serve_spec.render(_spec("http://api.example.com/x"), tmp_path / "awrtifact.yaml")


_HARNESS = r"""
import worker from './index.mjs';
const CT = new Uint8Array(Array.from({length: 100}, (_, i) => i));
let grantMode = 'ok';
globalThis.fetch = async (url, init) => {
  if (grantMode === 'down') throw new Error('unreachable');
  const body = JSON.parse(init.body);
  if (grantMode === 'revoked') return new Response('gone', {status: 410});
  if (body.ticket !== 'good') return new Response('no', {status: 403});
  const key = grantMode === 'escape' ? 'share/shr_OTHER0000/ciphertext.bin'
                                     : `share/${body.share_id}/ciphertext.bin`;
  return new Response(JSON.stringify({ok: true, object_key: key, size: 100, sha256: 'abc'}),
                      {status: 200, headers: {'Content-Type': 'application/json'}});
};
const reads = [];
const bucket = {
  async get(key, opts) {
    reads.push(key);
    if (!key.startsWith('share/shr_abcdefgh12/')) return null;
    if (opts && opts.range) {
      const r = opts.range;
      const off = r.suffix !== undefined ? 100 - r.suffix : r.offset;
      const len = r.length !== undefined ? r.length : 100 - off;
      return {size: 100, range: {offset: off, length: len}, body: CT.slice(off, off + len)};
    }
    return {size: 100, body: CT};
  },
};
const env = {WEIGHTS: bucket};
async function hit(path, headers = {}) {
  const req = new Request('https://artifact.aitherium.com' + path, {headers});
  const r = await worker.fetch(req, env);
  const buf = new Uint8Array(await r.arrayBuffer());
  return {status: r.status, len: buf.length, first: buf[0], cr: r.headers.get('Content-Range'),
          cache: r.headers.get('Cache-Control')};
}
const out = {};
out.noTicket = await hit('/s/shr_abcdefgh12');
out.badTicket = await hit('/s/shr_abcdefgh12?t=bad');
out.badId = await hit('/s/..%2Fetc?t=good');
out.whole = await hit('/s/shr_abcdefgh12?t=good');
out.range = await hit('/s/shr_abcdefgh12', {'X-Share-Ticket': 'good', Range: 'bytes=10-19'});
grantMode = 'revoked'; out.revoked = await hit('/s/shr_abcdefgh12?t=good');
grantMode = 'down'; out.down = await hit('/s/shr_abcdefgh12?t=good');
grantMode = 'escape'; out.escape = await hit('/s/shr_abcdefgh12?t=good');
out.reads = reads;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_rendered_share_route_fails_closed_under_node(tmp_path):
    js, _ = serve_spec.render(_spec("https://api.example.com/api/share/grant-check"),
                              tmp_path / "awrtifact.yaml")
    (tmp_path / "index.mjs").write_text(js, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["noTicket"]["status"] == 401
    assert out["badTicket"]["status"] == 403
    assert out["badId"]["status"] == 404
    assert out["whole"] == {"status": 200, "len": 100, "first": 0, "cr": None,
                            "cache": "private, no-store"}
    assert out["range"]["status"] == 206
    assert out["range"]["len"] == 10 and out["range"]["first"] == 10
    assert out["range"]["cr"] == "bytes 10-19/100"
    assert out["revoked"]["status"] == 410
    assert out["down"]["status"] == 503
    assert out["escape"]["status"] == 403
    # Refusals never touched the bucket: exactly the two served requests read it.
    assert out["reads"] == ["share/shr_abcdefgh12/ciphertext.bin"] * 2
