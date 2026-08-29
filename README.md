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

## One command to a working artifact store repo

`provision-repo` creates the repo if missing, seeds it with an init README
plus the share gate page, enables Pages, and prints the shareable URL:

```bash
awrtifact provision-repo --repo you/store                # private
awrtifact provision-repo --repo you/store --public       # Pages always works
```

The store is then ready for every lane — `awrtifact upload`, `awrtifact
mirror`, the GobboNet backup mod, any aw* client that speaks the chunk
contract. A brand-new repo is the ordinary FIRST use, and GitHub refuses to
publish releases in an empty repo — the seeding is what makes it real.

The repo's GitHub Pages can carry a share gate for **passphrase-encrypted**
backups: `https://you.github.io/store/backup-gate.html` shows a tokenless
manifest preview of an encrypted backup release and decrypts in the browser
for anyone with the passphrase. Encryption is a property of the CLIENT that
writes a backup, not of the store — the CLI's parts are plaintext (verified
by sha256), the GobboNet backup mod writes AES-GCM ciphertext, and both are
releases in the same store, fetchable byte-verified by either side. Private
repos work immediately (Pages needs a paid plan there for the gate half —
the command warns, it never fails).

**See the gate live** (the GobboNet mod's own Pages deployment):
[wizzense.github.io/GobboNet/backup-gate.html](https://wizzense.github.io/GobboNet/backup-gate.html)
— pick any encrypted backup release from the drop-down, enter the
passphrase, and watch it verify + decrypt in the browser.

## Clients of the contract

The chunk contract (`.partN` slices + per-part and whole sha256, under
GitHub's 2 GiB asset cap) is the interoperability boundary — anything that
speaks it can read anything that writes it:

- **the CLI** (`split`/`upload`/`mirror`/`fetch`/`verify`) — programmatic,
  plaintext, resumable
- **the workers** (`serve-spec` → artifact.aitherium.com / weights.aitherium.com)
  — Range-stitched serving of the same releases
- **the GobboNet backup mod** (`gobbonet-backup.js`) — browser-side,
  passphrase-encrypted backups into your own store, sharing via the gate page

One store, three clients, one manifest contract.

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
