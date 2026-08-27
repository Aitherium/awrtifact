# awrtifact — Aither World Artifact

Deliberately chunk artifacts into GitHub release assets and fetch them back
byte-verified. The productized aitherkvcache mirror lane: any artifact (model
weights, datasets, builds, backups) → `.partN` slices under GitHub's 2 GiB
per-asset cap → versioned release → served by a generated Cloudflare Worker
(CORS + HTTP Range + stitching) → fetched back with size and sha256 checks.

## The loop

```bash
# 1. Split a local artifact into .partN slices + manifest.json
awrtifact split DeepSeek-V4-Flash.gguf --out parts/

# 2. What is missing from the release? (resumable — re-runs upload only gaps)
awrtifact plan parts/manifest.json --repo Aitherium/aitherkvcache --release fleet-v1

# 3. Upload the missing parts (size-checked before upload, --create makes the release)
awrtifact upload parts/manifest.json --repo Aitherium/aitherkvcache \
    --release fleet-v1 --dir parts --parallel 8 --create

# 4. Prove the local parts match the manifest byte-for-byte
awrtifact verify parts/manifest.json --dir parts

# 5. Fetch it back anywhere (resume + size check + sha256 TOFU lockfile)
awrtifact fetch DeepSeek-V4-Flash.gguf --url https://weights.example.com/ \
    --out /srv/models --expected 49999999999

# 6. Or let the spec drive everything: generate the serving worker from it
awrtifact serve-spec awrtifact.yaml --check   # drift gate
awrtifact serve-spec awrtifact.yaml           # write the generated worker

# 7. Bring the backup up deliberately (dispatch mirror runs for gaps)
awrtifact backup-catalog awrtifact.yaml --dry-run
```

## The newbie half — one command to a working backup repo

`provision-repo` creates the repo if missing, seeds it with an init README
plus the GobboNet backup mod and the gate page, enables Pages, and prints
the shareable URL:

```bash
awrtifact provision-repo --repo you/backups              # private
awrtifact provision-repo --repo you/backups --public     # Pages always works
```

The repo's GitHub Pages **is** the gate: `https://you.github.io/backups/
backup-gate.html` shows a tokenless manifest preview of any backup release
and decrypts in the browser for anyone with the passphrase. Private repos
work for backups immediately (Pages needs a paid plan there — the command
warns, it never fails). A brand-new repo is the ordinary FIRST backup, and
GitHub refuses to publish releases in an empty repo — the seeding is what
makes it real.

## First mirror into a repo that is not aitherkvcache

The mirror lane dispatches a GitHub Actions workflow, and the workflow must
live in the TARGET repo first. For your own repo (public or private):

1. Copy `mirror-to-release.yml` and `hash-release-object.yml` (from the
   awrtifact source tree, `.DEPLOYMENT/workers/awrtifact/`) into your repo's
   `.github/workflows/` and push.
2. Then `awrtifact mirror <URL|FILE> --repo you/your-repo --release <tag>`
   works as usual.

Without the workflow the mirror refuses loudly (it never silently does
nothing) — the refusal names the two files to copy.

## Why the checks exist

- **2 GiB cap.** GitHub release assets top out at 2 GiB; larger artifacts are
  split at 1.9 GB. The worker stitches `.partN` slices behind the original
  filename, so clients ask for the name, never the parts.
- **Size is the truncation detector.** A short file is not a download error —
  the loader reports a corrupt artifact. Every step verifies sizes; the
  manifest's `total` is the number the client checks.
- **sha256 per part and whole.** Split records both; verify proves each slice
  and that the slices stitch back into the original.
- **Resume everywhere.** Uploads skip present parts; fetches resume from the
  byte count on disk via Range.

## Spec (the declarative store)

`awrtifact.yaml` names the store's repo, releases, allowlist and artifacts
(see `awrtifact/spec.py` for the full shape). The worker's data sections are
GENERATED from it — adding an artifact is a spec edit + regenerate, never a
worker code edit. `serve-spec --check` is the drift gate.

## Install

```bash
pip install awrtifact                   # core (stdlib-only)
pip install 'awrtifact[spec]'           # + spec commands (PyYAML)
```

Requires the `gh` CLI for plan/upload/backup commands (your existing auth).

## License

Apache-2.0
