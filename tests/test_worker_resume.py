"""The single-upstream lane never hands the client a clean short body.

Measured 2026-10-02 on weights.aitherium.com: 12 of 13 plain GETs of
`Bonsai-4B-Q1_0.gguf` (572,270,624 bytes) ended between 2 MB and 200 MB with
HTTP 200, `Transfer-Encoding: chunked` and no Content-Length, so curl exited 0
on a fragment. GitHub direct was whole 3 of 3 and a ranged GET through the
worker returned its exact 100,000,000 bytes. The worker piped the upstream body
through with no declared length, so an upstream stream that ENDED early was
indistinguishable from a finished one.

These tests EXECUTE the generated worker under Node against a fake upstream
that ends (or errors) its stream early:

1. a plain GET is byte-exact with an explicit Content-Length, resumed by Range;
2. a client Range request still gets exactly its range (206 + Content-Range);
3. an upstream that never makes progress produces an ERRORED stream, never a
   clean short one;
4. HEAD, CORS, the forced Content-Type and the 404 fall-through are preserved.

Both stream mechanisms are exercised: Cloudflare's `FixedLengthStream` (shimmed
here, Node has none) and the plain `TransformStream` fallback.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from awrtifact import serve_spec

SIZE = 1_000_003  # odd on purpose: never a multiple of the fake's chunk size


def _spec() -> dict:
    return {
        "store": {"name": "awrtifact", "repo": "Aitherium/aitherkvcache"},
        "workers": [{"name": "awrtifact", "dir": "awrtifact",
                     "route": "weights.aitherium.com/*",
                     "r2_binding": "WEIGHTS", "r2_bucket": "aither-artifacts"}],
        # Two upstreams: the first never has the file, so every scenario also
        # walks the 404 fall-through.
        "upstreams": [{"release": "empty-v1"}, {"release": "weights-v1"}],
        "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.gguf$"},
        "artifacts": [],
    }


_HARNESS = r"""
import { createHash } from 'node:crypto';

const SIZE = __SIZE__;
const CHUNK = 65536;
const DATA = new Uint8Array(SIZE);
for (let i = 0; i < SIZE; i++) DATA[i] = (i * 31 + (i >> 8) * 7) & 0xff;
const sha = (u8) => createHash('sha256').update(u8).digest('hex');

// The worker backs off 500 ms between stalled attempts; the test does not wait.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, _ms, ...rest) => realSetTimeout(fn, 0, ...rest);

if (process.argv[2] === 'fixed') {
  // Cloudflare's FixedLengthStream: an identity stream that REFUSES to close
  // before (or write past) the declared length. Node has none, so shim the
  // contract the worker relies on.
  globalThis.FixedLengthStream = class extends TransformStream {
    constructor(expected) {
      let seen = 0;
      super({
        transform(chunk, controller) {
          seen += chunk.byteLength;
          if (seen > expected) throw new TypeError('FixedLengthStream: wrote past the length');
          controller.enqueue(chunk);
        },
        flush() {
          if (seen !== expected) throw new TypeError('FixedLengthStream: closed short');
        },
      });
    }
  };
}

const { default: worker } = await import('./index.mjs');

// plan: {cuts, after, how: 'end'|'error', stall}
//   cuts  — how many upstream responses stop early
//   after — bytes delivered before an early stop
//   how   — 'end' closes the stream CLEANLY (the measured failure), 'error' throws
//   stall — after the first response, every later one delivers ZERO bytes
let plan;
let calls;
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  const method = init.method || 'GET';
  const range = new Headers(init.headers || {}).get('Range');
  calls.push({ url: url.split('/').slice(-2).join('/'), method, range });
  if (!url.includes('/weights-v1/')) return new Response('missing', { status: 404 });
  let start = 0;
  let end = SIZE - 1;
  let status = 200;
  // GitHub answers octet-stream for everything; a wrong TYPE proves ours wins.
  const headers = { 'Content-Type': 'text/plain', ETag: '"abc"' };
  if (range) {
    const m = /^bytes=(\d+)-(\d*)$/.exec(range);
    start = Number(m[1]);
    if (m[2] !== '') end = Number(m[2]);
    status = 206;
    headers['Content-Range'] = `bytes ${start}-${end}/${SIZE}`;
  }
  headers['Content-Length'] = String(end - start + 1);
  if (method === 'HEAD') return new Response(null, { status, headers });
  let limit = end - start + 1;
  let early = false;
  if (plan.stall && calls.filter((c) => c.method === 'GET' && c.url.startsWith('weights-v1')).length > 1) {
    limit = 0; early = true;
  } else if (plan.cuts > 0) {
    plan.cuts -= 1; limit = Math.min(limit, plan.after); early = true;
  }
  let sent = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (sent >= limit) {
        if (early && plan.how === 'error') controller.error(new Error('upstream reset'));
        else controller.close();
        return;
      }
      const n = Math.min(CHUNK, limit - sent);
      controller.enqueue(DATA.slice(start + sent, start + sent + n));
      sent += n;
    },
  });
  return new Response(body, { status, headers });
};

async function hit(p, { method = 'GET', range = null } = {}) {
  plan = { cuts: 0, after: 0, how: 'end', stall: false, ...p };
  calls = [];
  const headers = range ? { Range: range } : {};
  const r = await worker.fetch(new Request('https://weights.aitherium.com/m.gguf', { method, headers }), {});
  const got = [];
  let errored = false;
  if (r.body) {
    const reader = r.body.getReader();
    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        got.push(value);
      }
    } catch (_e) {
      errored = true;
    }
  }
  const buf = Buffer.concat(got);
  return {
    status: r.status, errored, len: buf.length, sha: sha(buf),
    contentLength: r.headers.get('Content-Length'),
    contentRange: r.headers.get('Content-Range'),
    contentType: r.headers.get('Content-Type'),
    acao: r.headers.get('Access-Control-Allow-Origin'),
    expose: r.headers.get('Access-Control-Expose-Headers'),
    ranges: calls.filter((c) => c.method === 'GET' && c.url.startsWith('weights-v1')).map((c) => c.range),
    missed: calls.filter((c) => c.url.startsWith('empty-v1')).length,
  };
}

const out = {
  size: SIZE,
  wholeSha: sha(DATA),
  sliceSha: sha(DATA.slice(1000, 501000)),
  clean: await hit({}),
  cut: await hit({ cuts: 3, after: 150001 }),
  reset: await hit({ cuts: 2, after: 150001, how: 'error' }),
  ranged: await hit({ cuts: 2, after: 100001 }, { range: 'bytes=1000-500999' }),
  rangedClean: await hit({}, { range: 'bytes=1000-500999' }),
  openRange: await hit({ cuts: 1, after: 70000 }, { range: 'bytes=900000-' }),
  stalled: await hit({ cuts: 1, after: 150001, stall: true }),
  head: await hit({}, { method: 'HEAD' }),
};
globalThis.fetch = async () => new Response('missing', { status: 404 });
const miss = await worker.fetch(new Request('https://weights.aitherium.com/m.gguf'), {});
out.missStatus = miss.status;
// The release that HOLDS the file fails transiently; the others honestly 404.
// Measured 2026-10-08: this answered 404, which reads as "wrong filename".
globalThis.fetch = async (url) => (String(url).includes('/weights-v1/')
  ? new Response('bad gateway', { status: 502 })
  : new Response('missing', { status: 404 }));
const flaky = await worker.fetch(new Request('https://weights.aitherium.com/m.gguf'), {});
out.flakyStatus = flaky.status;
out.flakyRetryAfter = flaky.headers.get('Retry-After');
const flakyHead = await worker.fetch(new Request('https://weights.aitherium.com/m.gguf', { method: 'HEAD' }), {});
out.flakyHeadStatus = flakyHead.status;
globalThis.fetch = async (url) => {
  if (String(url).includes('/weights-v1/')) throw new TypeError('network connection lost');
  return new Response('missing', { status: 404 });
};
const thrown = await worker.fetch(new Request('https://weights.aitherium.com/m.gguf'), {});
out.thrownStatus = thrown.status;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module", params=["fixed", "transform"])
def run(request, tmp_path_factory):
    """Execute the generated worker once per stream mechanism; return its report."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed; the generated worker cannot be executed")
    tmp = tmp_path_factory.mktemp(f"worker-{request.param}")
    js, _ = serve_spec.render(_spec(), tmp / "awrtifact.yaml")
    (tmp / "index.mjs").write_text(js, encoding="utf-8")
    (tmp / "harness.mjs").write_text(
        _HARNESS.replace("__SIZE__", str(SIZE)), encoding="utf-8")
    proc = subprocess.run(["node", "harness.mjs", request.param], cwd=tmp,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_clean_upstream_is_whole_with_content_length(run):
    got = run["clean"]
    assert got["status"] == 200 and not got["errored"]
    assert got["contentLength"] == str(SIZE)
    assert got["len"] == SIZE and got["sha"] == run["wholeSha"]
    # One upstream GET, the client's own (no Range): no resume when none is needed.
    assert got["ranges"] == [None]


def test_early_clean_end_is_resumed_byte_exact(run):
    """The measured failure: upstream ends cleanly at a fragment, three times."""
    got = run["cut"]
    assert got["status"] == 200 and not got["errored"]
    assert got["contentLength"] == str(SIZE)
    assert got["len"] == SIZE and got["sha"] == run["wholeSha"]
    # Each resume asks for exactly the bytes still owed, on the SAME upstream.
    assert got["ranges"] == [
        None,
        f"bytes=150001-{SIZE - 1}",
        f"bytes=300002-{SIZE - 1}",
        f"bytes=450003-{SIZE - 1}",
    ]


def test_upstream_read_error_is_resumed_byte_exact(run):
    got = run["reset"]
    assert got["status"] == 200 and not got["errored"]
    assert got["len"] == SIZE and got["sha"] == run["wholeSha"]
    assert len(got["ranges"]) == 3


def test_client_range_survives_early_ends(run):
    for key in ("ranged", "rangedClean"):
        got = run[key]
        assert got["status"] == 206 and not got["errored"], key
        assert got["contentRange"] == f"bytes 1000-500999/{SIZE}", key
        assert got["contentLength"] == "500000", key
        assert got["len"] == 500000 and got["sha"] == run["sliceSha"], key
    assert run["ranged"]["ranges"] == [
        "bytes=1000-500999", "bytes=101001-500999", "bytes=201002-500999"]
    assert run["rangedClean"]["ranges"] == ["bytes=1000-500999"]


def test_open_ended_client_range(run):
    got = run["openRange"]
    assert got["status"] == 206 and not got["errored"]
    assert got["contentRange"] == f"bytes 900000-{SIZE - 1}/{SIZE}"
    assert got["len"] == SIZE - 900000
    assert got["ranges"] == ["bytes=900000-", f"bytes=970000-{SIZE - 1}"]


def test_no_progress_errors_the_stream_never_a_clean_short_body(run):
    got = run["stalled"]
    assert got["status"] == 200
    # The promise (Content-Length) is the whole file; the delivery is a fragment.
    assert got["contentLength"] == str(SIZE)
    assert got["len"] < SIZE
    assert got["errored"], "a short body must FAIL the transfer, never end cleanly"
    # Bounded: the first GET plus a fixed number of no-progress resumes.
    assert 2 <= len(got["ranges"]) <= 10


def test_preserved_head_cors_type_and_fallthrough(run):
    head = run["head"]
    assert head["status"] == 200 and head["len"] == 0
    assert head["contentLength"] == str(SIZE)
    for key in ("clean", "cut", "ranged", "head"):
        got = run[key]
        assert got["acao"] == "*", key
        assert "Content-Length" in got["expose"], key
        # Upstream said text/plain; the TYPE is always ours.
        assert got["contentType"] == "application/octet-stream", key
        # The first upstream 404'd and the second answered.
        assert got["missed"] == 1, key
    assert run["missStatus"] == 404


def test_transient_upstream_failure_is_503_never_404(run):
    """A 404 means "no upstream has this name"; it must not be claimed after a 5xx/throw."""
    assert run["flakyStatus"] == 503
    assert run["flakyRetryAfter"] == "5"
    assert run["flakyHeadStatus"] == 503
    assert run["thrownStatus"] == 503
