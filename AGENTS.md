# awrtifact for agents

Read this if you are an agent (or a human) working in this repository.

## What this repo is

The public mirror of the awrtifact package — the deliberate chunk-and-release
artifact store. Source of truth is the private monorepo; this repo exists so
the package can publish to PyPI and strangers can read the docs. **Do not
hand-edit the mirrored files here** (`awrtifact/`, `tests/`, `pyproject.toml`,
`README.md`) — the next sync from the monorepo overwrites them. Edit the
monorepo copy and let the sync carry it.

## The core idea

GitHub release assets cap at 2 GiB. awrtifact splits larger artifacts into
`.partN` slices (1.9 GB default), records a manifest (`total`, per-part and
whole sha256), uploads missing parts resumably, and fetches them back
byte-verified. A generated Cloudflare Worker serves the slices behind the
original filename (CORS + HTTP Range + stitching), so clients ask for the
name, never the parts.

## Commands

```bash
awrtifact split FILE [--part-size N] [--out DIR]     # slice + manifest
awrtifact plan MANIFEST --repo O/R --release TAG     # what's missing
awrtifact upload MANIFEST --repo O/R --release TAG   # missing only, resumable
awrtifact verify MANIFEST [--dir DIR]                # byte-for-byte proof
awrtifact fetch NAME --url BASE --out DIR            # resume + sha256 TOFU
awrtifact serve-spec SPEC [--check]                  # generate the worker
awrtifact mirror <URL|FILE> --release TAG            # one-command mirror
```

Requires the `gh` CLI for plan/upload/backup. The `[spec]` extra adds
PyYAML for the spec-shaped commands.
