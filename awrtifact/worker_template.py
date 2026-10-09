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
    __R2_SETS_JSON__       prefix → {deny, deny_unknown, reason, licence_url}
    __SHARE_ROUTE_JS__     the Aither Share byte route, or "" (opt-in per worker)
    __SHARE_ROUTE_DISPATCH__  its dispatch line in fetch(), or ""
    __SHOP_ROUTE_JS__      the /shop/<product>/latest redirect, or "" (opt-in per worker)
    __SHOP_ROUTE_DISPATCH__   its dispatch line in fetch(), or ""
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
// R2-only, path-preserving, country-gated trees (spec `r2_sets`). Served from
// the private bucket and NOTHING else: no GitHub upstream, no chunked map, no
// flat-name fallback -- the whole point is that the worker is the only door.
const R2_SETS = __R2_SETS_JSON__;
const SAFE_KEY = /^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)*$/;__SHARE_ROUTE_JS____SHOP_ROUTE_JS__

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
  // Media (2026-10-01, the AitherOS showcase film): a <video> or <img> pointed at
  // the store needs a media type, or a direct link downloads instead of playing.
  if (name.endsWith('.mp4')) return 'video/mp4';
  if (name.endsWith('.webm')) return 'video/webm';
  if (/\.jpe?g$/.test(name)) return 'image/jpeg';
  if (name.endsWith('.png')) return 'image/png';
  if (name.endsWith('.vtt')) return 'text/vtt; charset=utf-8';
  if (name.endsWith('.srt')) return 'text/plain; charset=utf-8';
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

// A single-upstream body is PROMISED (explicit Content-Length) and then pumped
// with resume, never piped straight through. Measured 2026-10-02 on
// weights.aitherium.com: 12 of 13 plain GETs of Bonsai-4B-Q1_0.gguf
// (572,270,624 bytes) ended between 2 MB and 200 MB with HTTP 200,
// `Transfer-Encoding: chunked` and NO Content-Length — the upstream stream ended
// early and CLEANLY, the passthrough closed with it, and curl exited 0 on a
// fragment. GitHub direct was whole 3 of 3, and a ranged GET through this worker
// (bytes=300000000-399999999) returned exactly 100,000,000 bytes: the bytes are
// there, the long stream is what dies. So an early end is answered with a Range
// re-fetch of the SAME url from the byte we stopped at.
//
// RESUME_STALLS bounds CONSECUTIVE attempts that add no bytes (progress resets
// it). RESUME_FETCHES bounds the total: a release download is a redirect, so
// each re-fetch costs two subrequests, and 20 of them stay under the 50 a
// free-plan invocation is allowed.
const RESUME_STALLS = 5;
const RESUME_FETCHES = 20;

// `Content-Range: bytes <start>-<end>/<total|*>` -> absolute numbers, or null.
function parseContentRange(value) {
  const m = /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec((value || '').trim());
  if (!m) return null;
  const start = Number(m[1]);
  const end = Number(m[2]);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || end < start) return null;
  return { start, end, total: m[3] === '*' ? null : Number(m[3]) };
}

// The absolute byte span a 200/206 upstream response PROMISES, or null when it
// promises nothing we can hold it to (no length, or a content-encoded body whose
// Content-Length counts compressed bytes while the reader yields decoded ones).
function promisedSpan(upstream) {
  const enc = (upstream.headers.get('Content-Encoding') || 'identity').toLowerCase();
  if (enc !== 'identity') return null;
  if (upstream.status === 206) return parseContentRange(upstream.headers.get('Content-Range'));
  if (upstream.status !== 200) return null;
  const raw = upstream.headers.get('Content-Length');
  if (raw === null || !/^\d+$/.test(raw.trim())) return null;
  const total = Number(raw);
  if (!Number.isSafeInteger(total)) return null;
  return { start: 0, end: total - 1, total };
}

// Write bytes [span.start, span.end] of `url` into the writer, starting with the
// already-open response `first`. Returns only when EVERY promised byte was
// written; otherwise it throws, and the caller aborts the stream so the client
// sees a failed transfer — never a clean short one.
async function pumpResumable(url, first, span, writer, what) {
  let offset = span.start;
  let resp = first;
  let stalls = 0;
  let fetches = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const before = offset;
    if (resp && resp.body) {
      const reader = resp.body.getReader();
      while (offset <= span.end) {
        let step;
        try {
          step = await reader.read();
        } catch (_e) {
          break; // the upstream connection died mid-body: resume from `offset`
        }
        if (step.done) break; // ended — early or not, `offset` decides below
        const value = step.value;
        if (!value || !value.byteLength) continue;
        // Never write past the promise: a FixedLengthStream throws on overrun.
        const room = span.end - offset + 1;
        // A failed WRITE is the client going away, not the upstream: it is NOT
        // caught here, so it ends the pump instead of fetching for nobody.
        await writer.write(value.byteLength > room ? value.subarray(0, room) : value);
        offset += Math.min(value.byteLength, room);
      }
      try { await reader.cancel(); } catch (_) { /* already closed or errored */ }
    }
    if (offset > span.end) return;
    stalls = offset > before ? 0 : stalls + 1;
    if (stalls >= RESUME_STALLS) {
      throw new Error(`upstream ${what} made no progress at byte ${offset} of ${span.end + 1}`);
    }
    fetches += 1;
    if (fetches > RESUME_FETCHES) {
      throw new Error(`upstream ${what} still short at byte ${offset} after ${RESUME_FETCHES} resumes`);
    }
    if (stalls > 0) await new Promise((r) => setTimeout(r, 500));
    resp = null;
    let next;
    try {
      next = await fetch(url, {
        method: 'GET',
        headers: { Range: `bytes=${offset}-${span.end}` },
        redirect: 'follow',
      });
    } catch (_e) {
      continue; // counted as a stall on the next pass
    }
    // Only a 206 that STARTS at our offset and describes the same object may be
    // spliced in. A 200 means the Range was ignored (the body restarts at byte
    // 0), a 429/5xx is an error page — writing either would corrupt the file
    // while still satisfying Content-Length.
    const cr = next.status === 206 ? parseContentRange(next.headers.get('Content-Range')) : null;
    const sameObject = cr && (cr.total === null || span.total === null || cr.total === span.total);
    if (cr && cr.start === offset && sameObject) {
      resp = next;
    } else if (next.body) {
      try { await next.body.cancel(); } catch (_) { /* nothing to drain */ }
    }
  }
}

// Answer a GET with the promised span of one upstream asset: explicit
// Content-Length, body pumped by pumpResumable.
function serveResumable(url, upstream, span, name) {
  const length = span.end - span.start + 1;
  const headers = new Headers(upstream.headers);
  for (const [k, v] of Object.entries(cors)) headers.set(k, v);
  headers.set('Cache-Control', 'public, max-age=31536000, immutable');
  // GitHub releases answer octet-stream for EVERYTHING, including .js —
  // `import()` refuses that MIME (measured 2026-08-26). The upstream
  // headers are copied for Content-Range/ETag, but the TYPE is always ours.
  headers.set('Content-Type', contentTypeFor(name));
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Length', String(length));
  // FixedLengthStream is what makes the runtime SEND that Content-Length instead
  // of chunking, and it refuses to close short — so even a bug in the pump cannot
  // produce a clean fragment. The TransformStream arm is serveChunked's mechanism,
  // kept for runtimes without it.
  const { readable, writable } = typeof FixedLengthStream === 'function'
    ? new FixedLengthStream(length)
    : new TransformStream();
  (async () => {
    const writer = writable.getWriter();
    try {
      await pumpResumable(url, upstream, span, writer, name);
      await writer.close();
    } catch (e) {
      try { await writer.abort(e); } catch (_) { /* already aborted */ }
    }
  })();
  return new Response(readable, { status: upstream.status, headers });
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
// The R2 range get failed: learn the size with a HEAD, then answer the way
// RFC 9110 14.1.2 says -- 416 with `bytes */size` when no byte of the range
// exists, otherwise clamp (a suffix longer than the object is the whole
// object; a last-byte past EOF ends at EOF) and re-read. null = genuine miss,
// so the flat lane still falls through to its upstreams.
// `baseHeaders` lets the share route keep its private, no-store headers on the 416.
async function rangeAfterR2Refusal(bucket, name, m, baseHeaders = null) {
  let head;
  try {
    head = typeof bucket.head === 'function' ? await bucket.head(name) : null;
  } catch (_e) {
    return null;
  }
  if (!head || typeof head.size !== 'number') return null;
  const size = head.size;
  const [, startRaw, endRaw] = m;
  let r;
  if (startRaw === '') {
    const suffix = Number(endRaw);
    if (endRaw === '' || suffix === 0 || size === 0) return notSatisfiable(size, baseHeaders);
    r = { offset: Math.max(0, size - suffix), length: Math.min(suffix, size) };
  } else {
    const start = Number(startRaw);
    if (endRaw !== '' && Number(endRaw) < start) return null; // malformed: unchanged
    if (start >= size) return notSatisfiable(size, baseHeaders);
    const end = endRaw === '' ? size - 1 : Math.min(Number(endRaw), size - 1);
    r = { offset: start, length: end - start + 1 };
  }
  try {
    return await bucket.get(name, { range: r });
  } catch (_e) {
    return null;
  }
}

function notSatisfiable(size, baseHeaders = null) {
  const h = new Headers(baseHeaders || cors);
  h.set('Accept-Ranges', 'bytes');
  h.set('Content-Range', `bytes */${size}`);
  if (!baseHeaders) h.set('x-weight-source', 'r2');
  return new Response(null, { status: 416, headers: h });
}

async function serveFromR2(request, env, name) {
  const bucket = env && env.__R2_BINDING__;
  if (!bucket) return null;

  const rangeHeader = request.headers.get('Range');
  let object;
  let m = null;
  try {
    if (rangeHeader) {
      m = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
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
    object = null;
  }
  if (!object && m) {
    // A ranged get that threw or came back empty is EITHER a missing key OR a
    // range R2 refuses (past EOF, or a span running off the end). Only a HEAD
    // tells them apart, and it is spent only on this failure path: a served
    // range, a whole-object get and a refused country (serveR2Set answers 451
    // before calling here) cost no extra read. Measured 2026-10-07: a past-EOF
    // range on the gated hunyuan tree answered 404 instead of 416.
    object = await rangeAfterR2Refusal(bucket, name, m);
    if (object instanceof Response) return object;
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

/*
 * A gated R2 tree. Country first, BEFORE any storage access: a refused request
 * must not cost a read, and it must not leak whether the key exists. 451 is the
 * status for exactly this ("Unavailable For Legal Reasons", RFC 7725).
 */
async function serveR2Set(request, env, segs) {
  const set = R2_SETS[segs[0]];
  const country = ((request.cf && request.cf.country) || '').toUpperCase();
  // XX = unknown, T1 = Tor; EU / AP are continent-only geo-IP answers. None names a
  // country, so none can prove the request is inside the licensed territory.
  const unknown = !country || !/^[A-Z]{2}$/.test(country) ||
                  ['XX', 'T1', 'EU', 'AP', 'ZZ', 'UK', 'EL', 'IC', 'EA', 'FX'].includes(country);
  if ((unknown && set.deny_unknown) || set.deny.includes(country)) {
    const h = new Headers(cors);
    h.set('Content-Type', 'text/plain; charset=utf-8');
    if (set.licence_url) h.set('Link', `<${set.licence_url}>; rel="blocked-by"`);
    return new Response(`unavailable in your region: ${set.reason}\n`, { status: 451, headers: h });
  }
  const key = segs.join('/');
  if (!SAFE_KEY.test(key) || segs.some(s => s === '.' || s === '..')) {
    return new Response('bad path\n', { status: 400, headers: cors });
  }
  const served = await serveFromR2(request, env, key);
  if (served) return served;
  return new Response('not found\n', { status: 404, headers: cors });
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
    }__SHARE_ROUTE_DISPATCH____SHOP_ROUTE_DISPATCH__
    // Take only the FILENAME, so both surfaces work with one worker:
    //   artifacts.aitherium.com/<name>   (custom route, preferred)
    //   <worker>.<account>.workers.dev/<name> (fallback)
    // Prefix route: /<release>/<file> resolves ONLY within that release — each
    // release is its own namespace, so same-named files in different releases
    // (e.g. two tokenizer.json) coexist; the flat route keeps the legacy
    // first-match behaviour.
    const segs = new URL(request.url).pathname.split('/').filter(Boolean);
    if (segs.length >= 2 && Object.prototype.hasOwnProperty.call(R2_SETS, segs[0])) {
      return serveR2Set(request, env, segs);
    }
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
    // A PREFIXED request names one release, so a same-named object from another
    // release must not answer it. Measured 2026-09-03: /microembedder-v2/config.json
    // served microembedder-v1's 650-byte config (and its 22,972,370-byte ONNX) because
    // R2 and the chunked map are keyed by bare name and were consulted BEFORE the
    // prefix. R2 is skipped for prefixed paths (its keys carry no release); chunked
    // entries answer only when their upstream IS the requested release.
    const fromR2 = baseOverride ? null : await serveFromR2(request, env, name);
    if (fromR2) return fromR2;
    // Virtual chunked asset (>2 GiB source, split at upload).
    if (CHUNKED[name] && (!baseOverride || CHUNKED[name].upstream === baseOverride)) {
      return serveChunked(request, CHUNKED[name], name);
    }
    // Try each upstream until one has the file. A GitHub release 404s fast for a
    // missing asset, so the fallback cost is one small miss per unknown name.
    // A prefixed path has exactly ONE candidate (its own release).
    const candidates = baseOverride ? [baseOverride] : UPSTREAMS;
    // Whether any upstream failed TRANSIENTLY (429/5xx, a thrown fetch, a dead or
    // empty first chunk). If one did, "not found" is a lie: the file may be on that
    // upstream. Measured 2026-10-08: a CI range probe got HTTP 404 for
    // Bonsai-4B-Q1_0.gguf, which the mirror serves (206) on every other probe --
    // transient misses on the release that holds it fell through to the releases
    // that do not, and the worker answered 404. An installer reads a 404 as "wrong
    // name" and the checker as "our mirror serves nothing"; a 503 says what
    // happened and that a retry may work.
    let transient = false;
    for (const base of candidates) {
      if (request.method === 'HEAD') {
        let upstream;
        try {
          upstream = await fetch(base + name, { method: 'HEAD', redirect: 'follow' });
        } catch (_e) {
          transient = true;
          continue;
        }
        if (upstream.status === 404 || upstream.status === 410) continue;
        if (upstream.status === 429 || upstream.status >= 500) {
          transient = true;
          continue;
        }
        const headers = new Headers(upstream.headers);
        for (const [k, v] of Object.entries(cors)) headers.set(k, v);
        headers.set('Cache-Control', 'public, max-age=31536000, immutable');
        headers.set('Content-Type', contentTypeFor(name));
        return new Response(null, { status: upstream.status, headers });
      }
      // GET with retry on an EMPTY body (the 206/0 class, measured 2026-08-28).
      // Streaming-safe: only the first chunk is awaited to decide.
      for (let attempt = 1; attempt <= 3; attempt++) {
        if (attempt > 1) await new Promise((r) => setTimeout(r, 500 * (attempt - 1)));
        let upstream;
        try {
          upstream = await fetch(base + name, {
            method: 'GET',
            headers: request.headers.has('Range') ? { Range: request.headers.get('Range') } : {},
            redirect: 'follow',
          });
        } catch (_e) {
          transient = true; // connection refused/reset before any status
          continue;
        }
        if (upstream.status === 404 || upstream.status === 410) break; // next upstream
        if (upstream.status === 429 || upstream.status >= 500) {
          // transient (rate limit) — retry, never serve the error as bytes
          transient = true;
          continue;
        }
        // The normal case: the upstream says how many bytes it owes, so we
        // promise them and resume an early end (the clean-fragment class,
        // measured 2026-10-02). This also covers the 206/0 and dead-first-chunk
        // classes below — an empty body is just a resume from the first byte.
        const span = promisedSpan(upstream);
        if (span) return serveResumable(base + name, upstream, span, name);
        // No promise to hold the upstream to (416, an unsized or encoded body):
        // the passthrough below, unchanged.
        const reader = upstream.body.getReader();
        let first;
        try {
          first = await reader.read();
        } catch (_e) {
          // The connection died between headers and the first chunk — the
          // same class as streamUpstream (measured 2026-09-01 at ~40% of cold
          // Cloudflare->GitHub fetches); a throw here is NOT the empty-body
          // check below, and uncaught it aborts the response after the status
          // was sent. Retry the fetch (the loop head backs off).
          transient = true;
          continue;
        }
        if (!first.value) {
          transient = true;
          continue; // empty first chunk (0-length or done) — retry
        }
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
    if (transient) {
      headers.set('Retry-After', '5');
      headers.set('Cache-Control', 'no-store');
      return new Response('mirror upstream unavailable (transient), retry\n', { status: 503, headers });
    }
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


#: Aither Share byte route: `/s/<share_id>?t=<ticket>`. Rendered ONLY for a worker
#: whose spec entry sets `share_grant_url`; every other worker renders byte-for-
#: byte as before, so adding this template never redeploys a worker by itself.
#:
#: What it serves is CIPHERTEXT from R2 (`share/<id>/ciphertext.bin`). The worker
#: never holds a data key. Every request asks Genesis (through the public grant-
#: check door) whether the ticket still holds, so a revoke is effective on the
#: next request: the response is `private, no-store`, and nothing is cached at the
#: edge that could outlive a revocation.
SHARE_ROUTE_JS = r"""

// ── Aither Share: /s/<share_id>?t=<ticket> ─────────────────────────────────────
// GENERATED because this worker's spec sets share_grant_url. Grant-checked on
// EVERY request (never cached), Range-capable, ciphertext only.
const SHARE_GRANT_URL = __SHARE_GRANT_URL_JSON__;
const SHARE_ID = /^shr_[A-Za-z0-9_-]{8,64}$/;
const shareHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, X-Share-Ticket',
  'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges, X-Share-Sha256',
  'Cache-Control': 'private, no-store',
};

function shareRefusal(status, text) {
  return new Response(text + '\n', { status, headers: shareHeaders });
}

async function serveShare(request, env, shareId) {
  if (!SHARE_ID.test(shareId)) return shareRefusal(404, 'not found');
  const url = new URL(request.url);
  // TODO(ticket-in-query): `?t=` lands in edge logs and Referer headers, and the
  // ticket is reusable for its 5-minute life (it unlocks ciphertext only). Accept
  // it from the X-Share-Ticket header only, or have the grant check spend it.
  const ticket = request.headers.get('X-Share-Ticket') || url.searchParams.get('t') || '';
  if (!ticket) return shareRefusal(401, 'this link needs a ticket');
  const bucket = env && env.__R2_BINDING__;
  if (!bucket) return shareRefusal(503, 'share storage is not bound');

  let grant;
  try {
    const resp = await fetch(SHARE_GRANT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ share_id: shareId, ticket }),
    });
    // 410 = revoked or expired, relayed as-is so the page can say so.
    if (resp.status === 410) return shareRefusal(410, 'revoked by sender');
    if (!resp.ok) return shareRefusal(resp.status === 404 ? 404 : 403, 'not allowed');
    grant = await resp.json();
  } catch (_e) {
    // Fail CLOSED: an unreachable grant check never falls through to serving.
    return shareRefusal(503, 'grant check unavailable');
  }
  const prefix = `share/${shareId}/`;
  if (!grant || grant.ok !== true || typeof grant.object_key !== 'string'
      || !grant.object_key.startsWith(prefix)) {
    return shareRefusal(403, 'not allowed');
  }

  const rangeHeader = request.headers.get('Range');
  let object;
  if (rangeHeader) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
    if (!m || (m[1] === '' && m[2] === '')) return shareRefusal(416, 'bad range');
    let r;
    if (m[1] === '') r = { suffix: Number(m[2]) };
    else if (m[2] === '') r = { offset: Number(m[1]) };
    else {
      const start = Number(m[1]);
      const end = Number(m[2]);
      if (end < start) return shareRefusal(416, 'range not satisfiable');
      r = { offset: start, length: end - start + 1 };
    }
    // R2 THROWS on a range it cannot satisfy (past EOF, oversized suffix). That
    // used to escape as an uncaught 500; it now takes the same HEAD-then-416-or-
    // clamp path as serveFromR2, still strictly after the grant check above.
    try {
      object = await bucket.get(grant.object_key, { range: r });
    } catch (_e) {
      object = null;
    }
    if (!object) {
      object = await rangeAfterR2Refusal(bucket, grant.object_key, m, shareHeaders);
      if (object instanceof Response) return object;
    }
  } else {
    try {
      object = await bucket.get(grant.object_key);
    } catch (_e) {
      return shareRefusal(503, 'share storage unavailable');
    }
  }
  if (!object) return shareRefusal(404, 'not found');

  const headers = new Headers(shareHeaders);
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Type', 'application/octet-stream');
  if (typeof grant.sha256 === 'string') headers.set('X-Share-Sha256', grant.sha256);
  if (rangeHeader && object.range && typeof object.range.offset === 'number') {
    const start = object.range.offset;
    const len = object.range.length ?? (object.size - start);
    headers.set('Content-Length', String(len));
    // Whole-object size, never `*`: a client detects truncation against it.
    headers.set('Content-Range', `bytes ${start}-${start + len - 1}/${object.size}`);
    if (request.method === 'HEAD') return new Response(null, { status: 206, headers });
    return new Response(object.body, { status: 206, headers });
  }
  headers.set('Content-Length', String(object.size));
  if (request.method === 'HEAD') return new Response(null, { status: 200, headers });
  return new Response(object.body, { status: 200, headers });
}"""

SHARE_ROUTE_DISPATCH = r"""
    // Aither Share bytes: grant-checked, never matched by the allowlist below.
    {
      const shareSegs = new URL(request.url).pathname.split('/').filter(Boolean);
      if (shareSegs.length === 2 && shareSegs[0] === 's') {
        return serveShare(request, env, shareSegs[1]);
      }
    }"""


#: Shop downloads: `/shop/<product>/latest` -> 302 to `/<release>/<file>` on the same
#: host. Rendered ONLY for a worker whose spec entry sets `shop_route: true`. The
#: product rows are `spec.shop` (spec.shop_map); the target is an artifact the spec
#: already serves, so the redirect can never point outside the allowlisted releases.
#: A short edge cache (5 min) because `latest` moves when a new build is published;
#: the versioned target itself stays immutable.
SHOP_ROUTE_JS = r"""

// ── Shop downloads: /shop/<product>/latest ─────────────────────────────────────
// GENERATED because this worker's spec sets shop_route. The stable URL a shop SKU's
// download_url names; the bytes are a versioned release asset listed in the spec.
const SHOP = __SHOP_JSON__;

function serveShop(product) {
  const target = Object.prototype.hasOwnProperty.call(SHOP, product) ? SHOP[product] : null;
  if (!target) return new Response('no such product\n', { status: 404, headers: cors });
  const headers = new Headers(cors);
  headers.set('Location', target);
  headers.set('Cache-Control', 'public, max-age=300');
  return new Response(null, { status: 302, headers });
}"""

SHOP_ROUTE_DISPATCH = r"""
    // Shop downloads: a stable per-product URL, redirected to the versioned asset.
    {
      const shopSegs = new URL(request.url).pathname.split('/').filter(Boolean);
      if (shopSegs.length === 3 && shopSegs[0] === 'shop' && shopSegs[2] === 'latest') {
        return serveShop(shopSegs[1]);
      }
    }"""
