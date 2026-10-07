"""r2_sets: R2-only, path-preserving, country-gated trees.

Exists for weights whose licence restricts WHERE they may be distributed
(Tencent Hunyuan 3D 2.1 excludes the EU, UK and South Korea). The guarantees
pinned here, each one a way the gate could silently fail open:

* a refused country gets 451 BEFORE storage is touched, and an unknown
  country is refused too (deny_unknown);
* an allowed request is served from R2 by its FULL path (two files named
  model.fp16.ckpt in different directories are different objects);
* nothing in a gated tree ever falls back to a public GitHub upstream;
* the spec refuses a country typo (UK is not a code -- GB is) instead of
  generating a gate that leaves the territory open.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from awrtifact import serve_spec
from awrtifact import spec as spec_mod

EU27 = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR",
        "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]
# EU territory under its own ISO code (TFEU 349/355) -- the review found these served.
EU_OWN_CODES = ["GF", "GP", "MQ", "RE", "YT", "MF", "AX"]
UK_ADJ = ["GB", "GI", "IM", "JE", "GG"]


def _spec(r2_sets=None) -> dict:
    return {
        "store": {"name": "awrtifact", "repo": "Aitherium/aitherkvcache"},
        "workers": [{"name": "awrtifact", "dir": "awrtifact",
                     "route": "artifact.aitherium.com/*",
                     "r2_binding": "WEIGHTS", "r2_bucket": "aither-artifacts"}],
        "upstreams": [{"release": "artifact-v1"}],
        "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.(gguf|ckpt)$"},
        "artifacts": [],
        "r2_sets": r2_sets if r2_sets is not None else [{
            "prefix": "hunyuan3d-2.1",
            "deny_countries": EU27 + EU_OWN_CODES + UK_ADJ + ["KR"],
            "deny_unknown": True,
            "reason": "Tencent Hunyuan 3D 2.1 Community License excludes the EU, UK and South Korea",
            "licence_url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE",
        }],
    }


def test_r2_sets_map_is_validated_and_sorted():
    m = spec_mod.r2_sets(_spec())
    assert list(m) == ["hunyuan3d-2.1"]
    assert m["hunyuan3d-2.1"]["deny"] == sorted(EU27 + EU_OWN_CODES + UK_ADJ + ["KR"])
    assert m["hunyuan3d-2.1"]["deny_unknown"] is True


@pytest.mark.parametrize("rows, match", [
    ([{"prefix": "x", "deny_countries": ["UK"], "reason": "r"}], "GB"),
    ([{"prefix": "x", "deny_countries": ["EL"], "reason": "r"}], "GR"),
    ([{"prefix": "x", "deny_countries": ["EU"], "reason": "r"}], "member states"),
    ([{"prefix": "x", "deny_countries": ["IC"], "reason": "r"}], "reserved"),
    ([{"prefix": "x", "deny_countries": ["de"], "reason": "r"}], "alpha-2"),
    ([{"prefix": "x", "deny_countries": ["DE"], "reason": ""}], "reason"),
    ([{"prefix": "artifact-v1", "deny_countries": ["DE"], "reason": "r"}], "shadows"),
    ([{"prefix": "shop", "deny_countries": ["DE"], "reason": "r"}], "shadows"),
    ([{"prefix": "Bad Prefix", "deny_countries": [], "reason": "r"}], "slug"),
    ([{"prefix": "x", "deny_countries": [], "reason": "r"}] * 2, "duplicate"),
])
def test_spec_refuses_bad_r2_sets(rows, match):
    with pytest.raises(ValueError, match=match):
        spec_mod.validate(_spec(rows))


def test_render_embeds_the_gate(tmp_path):
    js, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    assert "__R2_SETS_JSON__" not in js
    assert '"hunyuan3d-2.1"' in js and "serveR2Set" in js and "451" in js


_HARNESS = r"""
import worker from './index.mjs';
const fetched = [];
globalThis.fetch = async (url) => { fetched.push(String(url)); return new Response('x', {status: 200}); };
const store = {
  'hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt': new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]),
  'hunyuan3d-2.1/hunyuan3d-vae-v2-1/model.fp16.ckpt': new Uint8Array([9, 9]),
  'hunyuan3d-2.1/NOTICE': new TextEncoder().encode('Tencent Hunyuan 3D 2.1 is licensed...'),
};
const r2reads = [];
// Strict like real R2: a range it cannot satisfy in full THROWS (R2 error 10039,
// InvalidRange) rather than clamping -- which is how a past-EOF Range became 404.
const WEIGHTS = {
  async get(key, opts) {
    r2reads.push(key);
    const b = store[key];
    if (!b) return null;
    if (opts && opts.range) {
      let off, len;
      if (opts.range.suffix !== undefined) {
        if (opts.range.suffix > b.length) throw new Error('get: InvalidRange (10039)');
        off = b.length - opts.range.suffix; len = opts.range.suffix;
      } else {
        off = opts.range.offset ?? 0; len = opts.range.length ?? (b.length - off);
      }
      if (off >= b.length || len <= 0 || off + len > b.length) throw new Error('get: InvalidRange (10039)');
      return {size: b.length, range: {offset: off, length: len}, body: b.slice(off, off + len)};
    }
    return {size: b.length, body: b};
  },
  async head(key) {
    r2reads.push('HEAD ' + key);
    const b = store[key];
    return b ? {size: b.length} : null;
  },
};
async function hit(path, country, headers) {
  const req = new Request('https://artifact.aitherium.com' + path, {headers: headers || {}});
  if (country !== undefined) Object.defineProperty(req, 'cf', {value: {country}});
  const r = await worker.fetch(req, {WEIGHTS});
  const buf = new Uint8Array(await r.arrayBuffer());
  return {status: r.status, len: buf.length, src: r.headers.get('x-weight-source'),
          link: r.headers.get('Link'), body: new TextDecoder().decode(buf).slice(0, 80),
          range: r.headers.get('Content-Range')};
}
const out = {};
const before = () => r2reads.length;
let n = before();
out.de = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'DE');
out.gb = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'GB');
out.kr = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'kr');
out.nocf = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', undefined);
out.xx = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'XX');
out.gf = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'GF');
out.ax = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'AX');
out.je = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'JE');
out.eu = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'EU');
for (const cc of ['UK', 'EL', 'IC', 'EA', 'FX']) out['r_' + cc] = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', cc);
out.refused_r2_reads = r2reads.length - n;
out.us_dit = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'US');
out.us_vae = await hit('/hunyuan3d-2.1/hunyuan3d-vae-v2-1/model.fp16.ckpt', 'US');
out.us_range = await hit('/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt', 'US', {Range: 'bytes=2-4'});
const DIT = '/hunyuan3d-2.1/hunyuan3d-dit-v2-1/model.fp16.ckpt';
// The live bug: a range wholly past EOF answered 404 (looked like a missing key).
out.us_past_eof = await hit(DIT, 'US', {Range: 'bytes=18-28'});
out.us_at_eof = await hit(DIT, 'US', {Range: 'bytes=8-'});
// A suffix longer than the object is the WHOLE object (RFC 9110 14.1.2), not an error.
out.us_big_suffix = await hit(DIT, 'US', {Range: 'bytes=-100'});
out.us_suffix = await hit(DIT, 'US', {Range: 'bytes=-3'});
// A last-byte past EOF ends at EOF.
out.us_over_end = await hit(DIT, 'US', {Range: 'bytes=6-100'});
out.us_open = await hit(DIT, 'US', {Range: 'bytes=5-'});
n = before();
out.us_range_again = await hit(DIT, 'US', {Range: 'bytes=2-4'});
out.valid_range_reads = r2reads.length - n;
// A past-EOF range on a MISSING key is still a miss, not a 416.
out.us_missing_range = await hit('/hunyuan3d-2.1/nope.ckpt', 'US', {Range: 'bytes=18-28'});
n = before();
out.de_past_eof = await hit(DIT, 'DE', {Range: 'bytes=18-28'});
out.refused_range_reads = r2reads.length - n;
// Flat lane: a ranged R2 miss still falls through to the upstreams.
out.flat_miss_range = await hit('/elsewhere.gguf', 'US', {Range: 'bytes=18-28'});
out.us_notice = await hit('/hunyuan3d-2.1/NOTICE', 'US');
out.us_missing = await hit('/hunyuan3d-2.1/nope.ckpt', 'US');
out.us_bad = await hit('/hunyuan3d-2.1/a%20b.ckpt', 'US');
out.proto = await hit('/__proto__/x.gguf', 'US');
out.fetched = fetched;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_gate_behaviour_under_node(tmp_path):
    js, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    (tmp_path / "index.mjs").write_text(js, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    for k in ("de", "gb", "kr", "nocf", "xx", "gf", "ax", "je", "eu",
              "r_UK", "r_EL", "r_IC", "r_EA", "r_FX"):
        assert out[k]["status"] == 451, (k, out[k])
        assert "unavailable in your region" in out[k]["body"]
        assert out[k]["link"] and "Hunyuan3D-2.1" in out[k]["link"]
    # Refused BEFORE storage: not one R2 read for any refusal.
    assert out["refused_r2_reads"] == 0
    # Allowed: served from R2 by FULL path -- the two same-named files differ.
    assert out["us_dit"]["status"] == 200 and out["us_dit"]["len"] == 8
    assert out["us_dit"]["src"] == "r2"
    assert out["us_vae"]["status"] == 200 and out["us_vae"]["len"] == 2
    assert out["us_range"]["status"] == 206 and out["us_range"]["len"] == 3
    assert out["us_range"]["range"] == "bytes 2-4/8"
    # Past EOF: 416 with the whole size, not a 404 that reads as "no such file".
    for k in ("us_past_eof", "us_at_eof"):
        assert out[k]["status"] == 416, (k, out[k])
        assert out[k]["range"] == "bytes */8", (k, out[k])
        assert out[k]["len"] == 0
    # Suffix longer than the object: the whole object as a 206.
    assert out["us_big_suffix"]["status"] == 206, out["us_big_suffix"]
    assert out["us_big_suffix"]["range"] == "bytes 0-7/8" and out["us_big_suffix"]["len"] == 8
    assert out["us_suffix"]["status"] == 206 and out["us_suffix"]["range"] == "bytes 5-7/8"
    assert out["us_over_end"]["status"] == 206 and out["us_over_end"]["range"] == "bytes 6-7/8"
    assert out["us_over_end"]["len"] == 2
    assert out["us_open"]["status"] == 206 and out["us_open"]["range"] == "bytes 5-7/8"
    # A valid range is unaffected and costs exactly one R2 read (no HEAD).
    assert out["us_range_again"]["status"] == 206 and out["us_range_again"]["range"] == "bytes 2-4/8"
    assert out["valid_range_reads"] == 1
    assert out["us_missing_range"]["status"] == 404
    # A refused country with a Range still never touches R2 (no get, no head).
    assert out["de_past_eof"]["status"] == 451 and out["refused_range_reads"] == 0
    assert any(u.endswith("/elsewhere.gguf") for u in out["fetched"]), out["fetched"]
    assert out["us_notice"]["status"] == 200
    assert out["us_missing"]["status"] == 404
    assert out["us_bad"]["status"] == 400
    # /__proto__/... must not be mistaken for a gated set (a plain R2_SETS[seg]
    # lookup reaches Object.prototype and crashes with a 500): it falls through
    # to the ordinary lane like any unknown prefix.
    assert out["proto"]["status"] not in (451, 500), out["proto"]
    # The gated tree NEVER fell back to a public upstream.
    assert not any("hunyuan" in u for u in out["fetched"]), out["fetched"]


def test_deployed_spec_denies_the_whole_excluded_territory():
    """The REAL spec, not a fixture: removing any excluded code from the deployed
    list must fail here, because the tests above only exercise the template."""
    from pathlib import Path
    real = Path(__file__).resolve().parents[4] / ".DEPLOYMENT/workers/awrtifact/awrtifact.yaml"
    if not real.is_file():
        pytest.skip("not inside the AitherOS monorepo")
    sets = spec_mod.r2_sets(spec_mod.load(real))
    hy = sets.get("hunyuan3d-2.1")
    assert hy is not None, "the Hunyuan3D gate is missing from the deployed spec"
    missing = set(EU27 + EU_OWN_CODES + UK_ADJ + ["KR"]) - set(hy["deny"])
    assert not missing, f"deployed gate leaves these territories open: {sorted(missing)}"
    assert hy["deny_unknown"] is True
