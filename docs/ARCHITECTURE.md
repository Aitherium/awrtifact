# Architecture

How awrtifact's pieces fit together.

## The data plane: release assets are the store

An artifact lives as `.partN` slices in a GitHub release. The release is the
versioned store; the manifest is the byte contract:

```json
{
  "name": "model.gguf",
  "total": 49999999999,
  "sha256": "<whole-file digest>",
  "part_size": 1900000000,
  "parts": [
    {"name": "model.gguf.part0", "size": 1900000000, "sha256": "..."},
    {"name": "model.gguf.part1", "size": 1900000000, "sha256": "..."}
  ]
}
```

Invariants: every part ≤ 2 GiB, `Σ parts == total`, per-part sha256, and
the whole-file sha256 stitched across part boundaries.

## The serving plane: a generated worker

`awrtifact serve-spec awrtifact.yaml` generates a Cloudflare Worker from a
declarative spec (`store`, `upstreams`, `allowlist`, `artifacts[]`). The
worker:

1. Answers `GET /<name>` with CORS headers, honoring `Range`.
2. Serves whole files (small artifacts) straight from an upstream release.
3. Stitches `.partN` slices for chunked artifacts: a `Range` that crosses a
   part boundary is answered by fetching the tail of one part and the head
   of the next and concatenating — byte-verified against the manifest.

`serve-spec --check` fails when a committed worker disagrees with a fresh
render of the spec — the worker is generated data, never hand-edited.

## The mirror lane: cloud-to-cloud by default

For a Range-serving source URL (e.g. a HuggingFace resolve link), `awrtifact
mirror <URL>` dispatches a workflow whose runners each Range-fetch a slice
and upload it as a release asset — the origin box's uplink is never
involved, and ≤20 parallel runners mirror multi-hundred-GB artifacts in
minutes. A URL that does not answer Range is refused outright, not fallen
back to a direct download. For a LOCAL file, `mirror` splits, creates the
release, uploads missing parts, and verifies.

## Why the checks exist

- **Size is the truncation detector.** A short file does not raise; the
  loader reports a corrupt artifact. Sizes are checked at every step and
  the client checks `total` against what it received.
- **sha256 per part and whole.** The seam proof: fetching across a part
  boundary must reproduce the same bytes as the manifest's whole-file
  digest.
- **Resume everywhere.** Uploads skip present parts; fetches resume from the
  byte count on disk via `Range`.

## The spec is the source of truth

The spec names the store's repo, releases, allowlist and every artifact
with its origin (`source_url`) and release. It doubles as the backup
manifest: `backup-catalog` dispatches mirror runs for the artifacts that
are not yet in their releases. Adding an artifact is a spec edit +
regenerate, never a worker edit.
