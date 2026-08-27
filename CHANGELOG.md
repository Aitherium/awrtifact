# Changelog

## 0.1.0 (2026-08-27)

First public release. The deliberate chunk-and-release loop:

- `split` — slice any file into `.partN` slices under GitHub's 2 GiB
  per-asset cap, recording a manifest with per-part and whole-file sha256.
- `plan` / `upload` — compute what a release is missing and upload only the
  gaps (size-checked, resumable, `--create` makes the release).
- `verify` — prove local parts match the manifest byte-for-byte.
- `fetch` — resumable, verified download with a sha256 TOFU lockfile.
- `serve-spec` — generate the serving Cloudflare Worker (CORS + Range +
  `.partN` stitching) from a declarative spec; `--check` gates drift.
- `mirror` — feed it a Range-serving URL (cloud-to-cloud, origin uplink
  never involved) or a local file; it mirrors and verifies.
- `backup-catalog` — dispatch mirror runs for the artifacts the spec names
  that are not yet in their releases.

The core is stdlib-only; the `[spec]` extra adds PyYAML for the
spec-shaped commands.
