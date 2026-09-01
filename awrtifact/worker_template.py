"""The generated-worker source templates (JS + wrangler.toml).

`awrtifact serve-spec` fills the __TOKEN__ slots from the spec. The JS is a
faithful port of `.DEPLOYMENT/workers/bonsai-weights/index.js` — the logic is
production-proven; only the DATA sections (allowlist, upstreams, split
manifest) are generated. The comments that encode measured lessons are kept
verbatim: a future editor must not rediscover them the expensive way.

Sentinel tokens (never valid in generated output):
    __GENERATED_HEADER__   the do-not-edit banner
    __UPSTREAMS_JSON__     array of release base URLs (try-in-order)
    __ALLOWED_SRC__        regex source string
    __WHOLE_JSON__         array of whole-file names
    __CHUNKED_JSON__       name → {upstream, parts:[{name,size}]}
"""

from __future__ import annotations

JS_TEMPLATE = r"""/**
 * __GENERATED_HEADER__
 *
 * CORS + range proxy for artifacts stored as GitHub release assets — WASM,
 * WebGPU, GGUF and ESM lanes.
 *
 * GitHub release assets give us 2GB files with range support — but send NO
 * Access-Control-Allow-Origin, so a browser fetch from aitherium.com is blocked
 * outright (verified 2026-08-01: 206 Partial Content, zero access-control-* headers).
 * GitHub Pages, the other candidate, caps files at 100MB. This Worker is the seam the
 * mirror design already called for: it forwards Range verbatim, streams the body, and
 * adds the CORS headers the browser requires.
 *
 * Allowlisted upstream ONLY — an open proxy would let anyone stream anything through
 * this hostname.
 */
const UPSTREAMS = __UPSTREAMS_JSON__;
const PATH_UPSTREAMS = __PATH_UPSTREAMS_JSON__;
const ALLOWED = /__ALLOWED_SRC__/;

// Fleet weights served whole (under the 2 GiB asset cap, no stitching needed).
const WHOLE = new Set(__WHOLE_JSON__);

// Files that exceed GitHub's 2 GiB per-asset cap ship as .partN slices uploaded
// separately, and this worker STITCHES them back into a single virtual asset the
// client asks for by the original filename. Range requests are translated into
// per-part sub-ranges. The manifest is GENERATED from the awrtifact spec — never
// hand-edited (a hand-edited entry is exactly how a stale build ships).
const CHUNKED = __CHUNKED_JSON__;

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, Content-Type',
  'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges, ETag',
};

/**
 * This worker serves BOTH weight binaries AND ESM modules. Weights fetched via
 * Range are binary (octet-stream is correct); MODULES are loaded by the page
 * with `import()`, which STRICTLY requires a JavaScript MIME — served as
 * octet-stream the browser refuses with "Failed to fetch dynamically imported
 * module" (measured 2026-08-26: the studio's on-device agent died on exactly
 * that on every browser; the module URL 200'd while the import failed).
 */
function contentTypeFor(name) {
  if (/\.(?:esm\.)?(?:js|mjs)$/.test(name)) return 'application/javascript; charset=utf-8';
  if (name.endsWith('.wasm')) return 'application/wasm';
  if (name.endsWith('.json')) return 'application/json';
  return 'application/octet-stream';
}

// Parse a single-range HTTP `Range` header. Multi-range not supported (GGUF
// loaders never ask for it). Returns absolute {start,end} or null on malformation.
function parseRange(header, totalSize) {
  const m = /^bytes=(\d+)-(\d*)$/.exec(header || '');
  if (!m) return null;
  const start = parseInt(m[1], 10);
  const end = m[2] === '' ? totalSize - 1 : parseInt(m[2], 10);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return { start, end };
}

// Serve a virtual (chunked) asset by translating the client's byte range into
// per-part sub-ranges, fetching each part with a scoped Range, and streaming the
// concatenated bodies back. Never buffers a whole part in memory.
async function serveChunked(request, spec, name) {
  const totalSize = spec.parts.reduce((s, p) => s + p.size, 0);
  const rangeHeader = request.headers.get('Range');
  let start = 0;
  let end = totalSize - 1;
  const hasRange = !!rangeHeader;
  if (hasRange) {
    const r = parseRange(rangeHeader, totalSize);
    if (!r) {
      const h = new Headers(cors);
      h.set('Content-Range', `bytes */${totalSize}`);
      return new Response('bad range\n', { status: 416, headers: h });
    }
    start = r.start;
    end = r.end;
    if (start < 0 || end < start || end >= totalSize) {
      const h = new Headers(cors);
      h.set('Content-Range', `bytes */${totalSize}`);
      return new Response('range not satisfiable\n', { status: 416, headers: h });
    }
  }
  const contentLength = end - start + 1;
  const headers = new Headers(cors);
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Type', contentTypeFor(name));
  headers.set('Content-Length', String(contentLength));
  headers.set('Cache-Control', 'public, max-age=31536000, immutable');
  if (hasRange) headers.set('Content-Range', `bytes ${start}-${end}/${totalSize}`);
  const status = hasRange ? 206 : 200;
  if (request.method === 'HEAD') return new Response(null, { status, headers });

  const { readable, writable } = new TransformStream();
  (async () => {
    const writer = writable.getWriter();
    try {
      let partStart = 0;
      for (const part of spec.parts) {
        const partEnd = partStart + part.size - 1;
        if (partEnd < start) { partStart += part.size; continue; }
        if (partStart > end) break;
        const subStart = Math.max(0, start - partStart);
        const subEnd = Math.min(part.size - 1, end - partStart);
        await streamUpstream(
          spec.upstream + part.name,
          { Range: `bytes=${subStart}-${subEnd}` },
          writer,
          part.name,
        );
        partStart += part.size;
      }
      await writer.close();
    } catch (e) {
      try { await writer.abort(e); } catch (_) { /* already aborted */ }
    }
  })();
  return new Response(readable, { status, headers });
}

// Stream one upstream body into the writer, RETRYING an EMPTY body. A
// Cloudflare→GitHub fetch can return a valid status with ZERO bytes
// (measured 2026-08-28: 206/0 at a part seam — the client then sees
// Content-Length promise a full range and receive nothing, which its
// truncation check reads as corruption; the AW002 seam gate caught it as
// 'does not stitch'). Streaming-safe: only the FIRST chunk is awaited to
// decide, so large bodies are never buffered.
async function streamUpstream(url, headers, writer, what) {
  for (let attempt = 1; attempt <= 5; attempt++) {
    const resp = await fetch(url, { headers, redirect: 'follow' });
    if (resp.status === 429 || resp.status >= 500) {
      // TRANSIENT upstream state (GitHub rate-limits the shared egress IP —
      // measured 2026-08-28: a burst of fresh-fetch probes tripped it, and
      // throwing here killed the stream into a 206/0 to the client). Retry,
      // never fail the stream on a 429/5xx.
      continue;
    }
    if (resp.status !== 206 && resp.status !== 200) {
      throw new Error(`upstream ${what} -> ${resp.status}`);
    }
    const reader = resp.body.getReader();
    let first;
    try {
      first = await reader.read();
    } catch (_e) {
      // The connection died between the response headers and the first body
      // chunk — measured 2026-09-01 at ~40% of cold Cloudflare->GitHub
      // fetches of the 90MB part, and the deployed empty-chunk retry did NOT
      // move the client-visible rate (3/10 pre -> 4/10 post): the read
      // THROWS here, it does not return empty, so no retry loop ever saw it.
      // Uncaught, it aborts the writer AFTER the 206 status was sent — the
      // client-visible 206/0. Same treatment as an empty body: back off and
      // retry the fetch.
      await new Promise((r) => setTimeout(r, 500));
      continue;
    }
    if (!first.value) {
      // 0-length FIRST chunk — cold upstream connections (Cloudflare -> GitHub)
      // can deliver valid 206/200 headers with an empty body, and the first
      // chunk is NOT always final (measured 2026-09-01: ~30-40% of cold first
      // fetches). Cancel the reader so the connection drains, back off, and
      // retry the fetch.
      await reader.cancel();
      await new Promise((r) => setTimeout(r, 500));
      continue;
    }
    await writer.write(first.value);
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await writer.write(value);
    }
    return;
  }
  throw new Error(`upstream ${what} failed after 5 attempts`);
}

/**
 * R2 first. Everything below is unchanged and stays as the fallback.
 *
 * WHY R2 AT ALL: every mechanism in this file exists because a GitHub Release
 * asset is capped at 2 GiB. That single limit is the direct cause of the
 * `.partN` split, the generated manifest, and serveChunked()'s range-stitching.
 * R2 has no per-object cap, serves Range natively, and charges nothing for
 * egress, so an object living there needs none of it: no split, no manifest to
 * drift, no stitching.
 *
 * A miss returns null and falls straight through to the existing path, so a
 * config slip must degrade to the old lane, never 500 the artifact request.
 */
async function serveFromR2(request, env, name) {
  const bucket = env && env.__R2_BINDING__;
  if (!bucket) return null;

  const rangeHeader = request.headers.get('Range');
  let object;
  try {
    if (rangeHeader) {
      const m = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
      if (!m) return null;                 // let the existing parser answer 416
      const [, startRaw, endRaw] = m;
      // R2 wants {offset,length} or {suffix}; translate the three legal forms.
      let r;
      if (startRaw === '') r = { suffix: Number(endRaw) };
      else if (endRaw === '') r = { offset: Number(startRaw) };
      else r = { offset: Number(startRaw), length: Number(endRaw) - Number(startRaw) + 1 };
      object = await bucket.get(name, { range: r });
    } else {
      object = await bucket.get(name);
    }
  } catch (_e) {
    return null;
  }
  if (!object) return null;

  const headers = new Headers(cors);
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Type', contentTypeFor(name));
  headers.set('Cache-Control', 'public, max-age=31536000, immutable');
  // So a probe can tell WHICH lane answered without guessing from timing.
  headers.set('x-weight-source', 'r2');

  if (rangeHeader && object.range && typeof object.range.offset === 'number') {
    const start = object.range.offset;
    const len = object.range.length ?? (object.size - start);
    const end = start + len - 1;
    headers.set('Content-Length', String(len));
    // `object.size` is the WHOLE object, not the slice — R2 reports the served
    // slice separately in `object.range`. An unknown total is not a cosmetic
    // loss here: the client compares the declared size against what it received
    // to detect truncation, so `Content-Range: bytes .../*` is precisely the
    // "download finished, model corrupt" failure. Always the whole-object size.
    headers.set('Content-Range', `bytes ${start}-${end}/${object.size}`);
    if (request.method === 'HEAD') return new Response(null, { status: 206, headers });
    return new Response(object.body, { status: 206, headers });
  }
  headers.set('Content-Length', String(object.size));
  if (request.method === 'HEAD') return new Response(null, { status: 200, headers });
  return new Response(object.body, { status: 200, headers });
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    // Health surface for the addon manifest / fleet probes.
    if (new URL(request.url).pathname === '/__health') {
      return new Response('{"ok":true}', {
        status: 200,
        headers: { 'Content-Type': 'application/json', ...cors },
      });
    }
    // Take only the FILENAME, so both surfaces work with one worker:
    //   artifacts.aitherium.com/<name>   (custom route, preferred)
    //   <worker>.<account>.workers.dev/<name> (fallback)
    // Prefix route: /<release>/<file> resolves ONLY within that release — each
    // release is its own namespace, so same-named files in different releases
    // (e.g. two tokenizer.json) coexist; the flat route keeps the legacy
    // first-match behaviour.
    const segs = new URL(request.url).pathname.split('/').filter(Boolean);
    let baseOverride = null;
    if (segs.length >= 2 && PATH_UPSTREAMS[segs[0]]) {
      baseOverride = PATH_UPSTREAMS[segs[0]];
    }
    const name = segs.pop() || '';
    if (!ALLOWED.test(name) && !CHUNKED[name] && !WHOLE.has(name)) {
      return new Response('not a known artifact\n', { status: 404, headers: cors });
    }
    // R2 before everything, INCLUDING the chunked path: an object uploaded whole
    // to R2 makes its `.partN` manifest irrelevant, and checking after would keep
    // serving the stitched copy of a file that no longer needs stitching.
    const fromR2 = await serveFromR2(request, env, name);
    if (fromR2) return fromR2;
    // Virtual chunked asset (>2 GiB source, split at upload).
    if (CHUNKED[name]) return serveChunked(request, CHUNKED[name], name);
    // Try each upstream until one has the file. A GitHub release 404s fast for a
    // missing asset, so the fallback cost is one small miss per unknown name.
    // A prefixed path has exactly ONE candidate (its own release).
    const candidates = baseOverride ? [baseOverride] : UPSTREAMS;
    for (const base of candidates) {
      if (request.method === 'HEAD') {
        const upstream = await fetch(base + name, { method: 'HEAD', redirect: 'follow' });
        if (upstream.status === 404 || upstream.status === 410) continue;
        const headers = new Headers(upstream.headers);
        for (const [k, v] of Object.entries(cors)) headers.set(k, v);
        headers.set('Cache-Control', 'public, max-age=31536000, immutable');
        headers.set('Content-Type', contentTypeFor(name));
        return new Response(null, { status: upstream.status, headers });
      }
      // GET with retry on an EMPTY body (the 206/0 class, measured 2026-08-28).
      // Streaming-safe: only the first chunk is awaited to decide.
      for (let attempt = 1; attempt <= 3; attempt++) {
        const upstream = await fetch(base + name, {
          method: 'GET',
          headers: request.headers.has('Range') ? { Range: request.headers.get('Range') } : {},
          redirect: 'follow',
        });
        if (upstream.status === 404 || upstream.status === 410) break; // next upstream
        if (upstream.status === 429 || upstream.status >= 500) {
          // transient (rate limit) — retry, never serve the error as bytes
          continue;
        }
        const reader = upstream.body.getReader();
        let first;
        try {
          first = await reader.read();
        } catch (_e) {
          // The connection died between headers and the first chunk — the
          // same class as streamUpstream (measured 2026-09-01 at ~40% of cold
          // Cloudflare->GitHub fetches); a throw here is NOT the empty-body
          // check below, and uncaught it aborts the response after the status
          // was sent. Back off and retry the fetch.
          await new Promise((r) => setTimeout(r, 500));
          continue;
        }
        if (!first.value) continue; // empty first chunk (0-length or done) — retry
        const body = new ReadableStream({
          async start(controller) {
            if (first.value) controller.enqueue(first.value);
            // eslint-disable-next-line no-constant-condition
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              controller.enqueue(value);
            }
            controller.close();
          },
        });
        const headers = new Headers(upstream.headers);
        for (const [k, v] of Object.entries(cors)) headers.set(k, v);
        headers.set('Cache-Control', 'public, max-age=31536000, immutable');
        // GitHub releases answer octet-stream for EVERYTHING, including .js —
        // `import()` refuses that MIME (measured 2026-08-26). The upstream
        // headers are copied for Content-Length/Range, but the TYPE is always ours.
        headers.set('Content-Type', contentTypeFor(name));
        return new Response(body, { status: upstream.status, headers });
      }
    }
    const headers = new Headers(cors);
    return new Response('not found on any mirror upstream\n', { status: 404, headers });
  },
};
"""

TOML_TEMPLATE = """\
# GENERATED by `awrtifact serve-spec` — DO NOT EDIT BY HAND.
# Regenerate from the spec; a git diff after regeneration is the drift check.
name = "__WORKER_NAME__"
main = "index.js"
compatibility_date = "__COMPAT_DATE__"

# workers_dev stays ON deliberately: adding `routes` silently disables
# *.workers.dev (measured 2026-08-01 on bonsai-weights: 206 → 404). Both
# hostnames work; a route is an ADDITION, never a replacement. Never use
# `custom_domain` — the deploy token is zone-read-only.
workers_dev = true
__ROUTE_BLOCK__

[[r2_buckets]]
binding = "__R2_BINDING__"
bucket_name = "__R2_BUCKET__"
"""
