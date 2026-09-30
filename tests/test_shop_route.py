"""The opt-in `/shop/<product>/latest` redirect in the generated worker.

A shop SKU's `download_url` is a stable URL; the bytes are a versioned release asset.
Until 2026-09-30 the worker had no such route ('latest' fails the allowlist) and both
shop downloads (saga, deep-research) 404'd while their releases were complete.

1. OPT-IN IS BYTE-EXACT: a worker without `shop_route` renders as before.
2. The spec refuses a shop row that names no artifact, a bad slug, or a duplicate.
3. Executed under Node: a known product 302s to /<release>/<name> on the same host,
   and that target then serves the bytes through the prefixed release lane; an
   unknown product is 404, never a fall-through to the upstreams.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from awrtifact import serve_spec, worker_template
from awrtifact import spec as spec_mod


def _spec(shop_route: bool = True, shop=None) -> dict:
    worker = {"name": "awrtifact", "dir": "awrtifact", "route": "artifact.aitherium.com/*",
              "r2_binding": "WEIGHTS", "r2_bucket": "aither-artifacts"}
    if shop_route:
        worker["shop_route"] = True
    return {
        "store": {"name": "awrtifact", "repo": "Aitherium/aitherkvcache"},
        "workers": [worker], "upstreams": [{"release": "artifact-v1"}],
        "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.gguf$"},
        "artifacts": [{"id": "shop-saga-zip", "name": "saga-1.0.0.zip", "total": 12,
                       "release": "shop-saga-v1"}],
        "shop": shop if shop is not None else [{"product": "saga",
                                                "artifact": "shop-saga-zip"}],
    }


def test_shop_map_names_release_and_file():
    assert spec_mod.shop_map(_spec()) == {"saga": "/shop-saga-v1/saga-1.0.0.zip"}


@pytest.mark.parametrize("rows, match", [
    ([{"product": "saga", "artifact": "nope"}], "not an artifacts"),
    ([{"product": "Saga!", "artifact": "shop-saga-zip"}], "slug"),
    ([{"product": "saga", "artifact": "shop-saga-zip"}] * 2, "duplicate"),
])
def test_spec_refuses_bad_shop_rows(rows, match):
    with pytest.raises(ValueError, match=match):
        spec_mod.validate(_spec(shop=rows))


def test_absent_shop_route_renders_no_shop_code(tmp_path, monkeypatch):
    js, _ = serve_spec.render(_spec(shop_route=False), tmp_path / "awrtifact.yaml")
    assert "serveShop" not in js and "__SHOP" not in js
    legacy = worker_template.JS_TEMPLATE.replace("__SHOP_ROUTE_JS__", "").replace(
        "__SHOP_ROUTE_DISPATCH__", "")
    monkeypatch.setattr(worker_template, "JS_TEMPLATE", legacy)
    js_legacy, _ = serve_spec.render(_spec(shop_route=False), tmp_path / "awrtifact.yaml")
    assert js == js_legacy


_HARNESS = r"""
import worker from './index.mjs';
const fetched = [];
globalThis.fetch = async (url, init) => {
  fetched.push(String(url));
  if (String(url).endsWith('/shop-saga-v1/saga-1.0.0.zip')) {
    return new Response(new Uint8Array([80, 75, 3, 4, 1, 2, 3, 4, 5, 6, 7, 8]),
                        {status: 200, headers: {'Content-Length': '12'}});
  }
  return new Response('missing', {status: 404});
};
async function hit(path) {
  const r = await worker.fetch(new Request('https://artifact.aitherium.com' + path), {});
  const buf = new Uint8Array(await r.arrayBuffer());
  return {status: r.status, location: r.headers.get('Location'), len: buf.length,
          cache: r.headers.get('Cache-Control'),
          cors: r.headers.get('Access-Control-Allow-Origin')};
}
const out = {};
out.latest = await hit('/shop/saga/latest');
out.target = await hit(out.latest.location);
out.unknown = await hit('/shop/nope/latest');
out.proto = await hit('/shop/__proto__/latest');
out.fetched = fetched;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_rendered_shop_route_redirects_then_serves_under_node(tmp_path):
    js, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    (tmp_path / "index.mjs").write_text(js, encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(["node", "harness.mjs"], cwd=tmp_path, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["latest"]["status"] == 302
    assert out["latest"]["location"] == "/shop-saga-v1/saga-1.0.0.zip"
    assert out["latest"]["cache"] == "public, max-age=300"
    assert out["latest"]["cors"] == "*"
    assert out["target"]["status"] == 200 and out["target"]["len"] == 12
    assert out["unknown"]["status"] == 404
    assert out["proto"]["status"] == 404
    # The redirect itself fetched nothing; only the versioned target hit the upstream.
    assert out["fetched"] == [
        "https://github.com/Aitherium/aitherkvcache/releases/download/shop-saga-v1/saga-1.0.0.zip"]
