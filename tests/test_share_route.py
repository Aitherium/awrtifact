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
// Strict like real R2: a range it cannot satisfy in full THROWS (InvalidRange).
const bucket = {
  async get(key, opts) {
    reads.push(key);
    if (!key.startsWith('share/shr_abcdefgh12/')) return null;
    if (opts && opts.range) {
      const r = opts.range;
      if (r.suffix !== undefined && r.suffix > 100) throw new Error('get: InvalidRange (10039)');
      const off = r.suffix !== undefined ? 100 - r.suffix : r.offset;
      const len = r.length !== undefined ? r.length : 100 - off;
      if (off >= 100 || len <= 0 || off + len > 100) throw new Error('get: InvalidRange (10039)');
      return {size: 100, range: {offset: off, length: len}, body: CT.slice(off, off + len)};
    }
    return {size: 100, body: CT};
  },
  async head(key) {
    reads.push('HEAD ' + key);
    return key.startsWith('share/shr_abcdefgh12/') ? {size: 100} : null;
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
const T = {'X-Share-Ticket': 'good'};
out.pastEof = await hit('/s/shr_abcdefgh12', {...T, Range: 'bytes=110-120'});
out.bigSuffix = await hit('/s/shr_abcdefgh12', {...T, Range: 'bytes=-1000'});
out.overEnd = await hit('/s/shr_abcdefgh12', {...T, Range: 'bytes=95-200'});
out.missing = await hit('/s/shr_missing000', {...T, Range: 'bytes=110-120'});
out.missingWhole = await hit('/s/shr_missing000?t=good');
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
    # Past EOF: 416 with the whole size (was an uncaught throw -> 500 at the edge).
    assert out["pastEof"]["status"] == 416, out["pastEof"]
    assert out["pastEof"]["cr"] == "bytes */100" and out["pastEof"]["len"] == 0
    assert out["pastEof"]["cache"] == "private, no-store"
    # Suffix longer than the object: the whole object as a 206.
    assert out["bigSuffix"]["status"] == 206, out["bigSuffix"]
    assert out["bigSuffix"]["cr"] == "bytes 0-99/100" and out["bigSuffix"]["len"] == 100
    assert out["overEnd"]["status"] == 206 and out["overEnd"]["cr"] == "bytes 95-99/100"
    assert out["missing"]["status"] == 404 and out["missingWhole"]["status"] == 404
    assert out["revoked"]["status"] == 410
    assert out["down"]["status"] == 503
    assert out["escape"]["status"] == 403
    # Refusals never touched the bucket: only the served/R2-checked requests read it,
    # and a valid range or whole get costs one read with no HEAD.
    ok, miss = "share/shr_abcdefgh12/ciphertext.bin", "share/shr_missing000/ciphertext.bin"
    assert out["reads"] == [ok, ok,
                            ok, "HEAD " + ok,          # pastEof -> 416
                            ok, "HEAD " + ok, ok,      # bigSuffix -> clamp, re-read
                            ok, "HEAD " + ok, ok,      # overEnd -> clamp, re-read
                            miss, "HEAD " + miss,      # missing ranged -> 404
                            miss]                      # missing whole -> 404


_PREFIX_HARNESS = r"""
import worker from './index.mjs';
const reads = [];
// A bucket that answers ANY key: if the public lane ever resolves a `share/...`
// request to a name, this serves it -- so a 404 here is the guard, not a miss.
const bucket = {
  async get(key) { reads.push(key); return {size: 3, body: new Uint8Array([1, 2, 3])}; },
  async head(key) { reads.push('HEAD ' + key); return {size: 3}; },
};
globalThis.fetch = async () => new Response('no upstream', {status: 404});
async function hit(path) {
  const req = new Request('https://artifact.aitherium.com' + path);
  return (await worker.fetch(req, {WEIGHTS: bucket})).status;
}
const out = {};
out.control = await hit('/ciphertext.bin');
const before = reads.length;
out.nested = await hit('/share/shr_abcdefgh12/ciphertext.bin');
out.seal = await hit('/share/shr_abcdefgh12/awseal.json');
out.bare = await hit('/share');
out.shareReads = reads.slice(before);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("share_url", [None, "https://api.example.com/api/share/grant-check"])
def test_public_lane_refuses_the_share_prefix_under_node(tmp_path, share_url):
    # An allowlist that admits the share objects' bare names, so the ONLY thing
    # standing between `/share/<id>/ciphertext.bin` and the bucket is the guard.
    spec = _spec(share_url)
    spec["allowlist"] = {"regex": r"^[A-Za-z0-9._-]+\.(bin|json)$"}
    js, _ = serve_spec.render(spec, tmp_path / "awrtifact.yaml")
    (tmp_path / "index.mjs").write_text(js, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_PREFIX_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["control"] == 200, out  # the harness CAN serve a public bare name
    assert out["nested"] == 404 and out["seal"] == 404 and out["bare"] == 404, out
    assert out["shareReads"] == [], out  # refused before the bucket is touched
