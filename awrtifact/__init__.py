"""awrtifact — deliberately chunk artifacts into GitHub release assets.

Aither World Artifact is the productized aitherkvcache mirror lane: ANY artifact
(model weights, datasets, builds, backups) is split into `.partN` slices under
GitHub's 2 GiB per-asset cap, stored as a versioned GitHub release, served by a
generated Cloudflare Worker (CORS + HTTP Range + `.partN` stitching), and
fetched back byte-verified.

The core is stdlib-only; the spec-shaped commands need PyYAML (extra: `spec`).
"""

__version__ = "0.1.0"
