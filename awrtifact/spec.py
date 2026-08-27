"""awrtifact.yaml — the declarative store definition (source of truth).

The spec is what `serve-spec` turns into the worker: allowlist, upstreams and
the split manifest are DATA here, never code edits. Adding an artifact is a
spec edit + regenerate — the deliberate replacement for the bonsai-weights
worker's hand-edited CHUNKED map (the stale-orchestrator defect: a new build
published under a name the hardcoded map did not know).

Shape:

    store:
      name: awrtifact
      repo: Aitherium/aitherkvcache      # owner/repo holding the releases
      worker_name: awrtifact             # default worker (single-worker form)
      route: "artifact.aitherium.com/*"  # optional; workers_dev stays on
      r2_binding: WEIGHTS                # R2 bucket binding (miss → GitHub)
      r2_bucket: aither-artifacts
    workers:                             # multi-worker form (overrides store.*)
      - name: awrtifact
        dir: awrtifact                   # relative to .DEPLOYMENT/workers/
        route: "artifact.aitherium.com/*"
        r2_binding: WEIGHTS
        r2_bucket: aither-artifacts
      - name: bonsai-weights
        dir: bonsai-weights              # the weights.aitherium.com lane
        route: "weights.aitherium.com/*"
        r2_binding: WEIGHTS
        r2_bucket: aither-model-weights
    upstreams:                           # whole-file lane, tried in order
      - release: bonsai-wasm-v1
    allowlist:
      regex: "^[A-Za-z0-9._-]+\\.(gguf|safetensors|bin|js|json)$"
      whole: []                          # extra whole-file names (opt-in)
    artifacts:
      - id: qwen3-4b-encoder
        name: qwen3-4b-encoder.hqq4.gguf # served asset base name
        source_url: https://…            # where the bytes come from (backup)
        total: 2822072544                # exact size — fail-loud source check
        release: bonsai-image-v1         # release tag to serve from
        part_size: 1900000000            # optional; default 1.9 GiB
        parts: []                        # optional explicit sizes; else derived

An artifact whose total exceeds GitHub's 2 GiB cap is CHUNKED (parts derived
uniformly: N full slices + one tail). An artifact at or under the cap is
served WHOLE unless explicit `parts` are given.
"""

from __future__ import annotations

import re
from pathlib import Path

from .manifest import DEFAULT_PART_SIZE, GITHUB_ASSET_CAP, derive_parts

SCHEMA_VERSION = 1

_SPEC_DEFAULTS = {
    "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.(gguf|safetensors|bin|js|json)$",
                  "whole": []},
    "upstreams": [],
    "artifacts": [],
}


def _require_yaml():
    try:
        import yaml  # noqa: PLC0415 — optional extra, loaded where needed
    except ImportError as exc:
        raise ValueError(
            "PyYAML is required for the spec commands; install it with "
            "`pip install -e 'AitherOS/packages/awrtifact[spec]'`"
        ) from exc
    return yaml


def load(path: Path) -> dict:
    yaml = _require_yaml()
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read spec {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"spec {path} is not a YAML mapping")
    return validate(raw)


def dump(spec: dict) -> str:
    yaml = _require_yaml()
    return yaml.safe_dump(spec, sort_keys=False, default_flow_style=False)


def validate(spec: dict) -> dict:
    """Refuse a spec whose data would generate a wrong worker."""
    store = spec.get("store") or {}
    repo = store.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise ValueError("spec.store.repo must be 'owner/repo'")
    if not spec.get("store", {}).get("name"):
        raise ValueError("spec.store.name is required")
    if not isinstance(spec.get("artifacts"), list):
        raise ValueError("spec.artifacts must be a list")
    for w in spec.get("workers") or []:
        if not isinstance(w.get("name"), str) or not w.get("name"):
            raise ValueError("spec.workers[].name is required")
        if not isinstance(w.get("dir"), str) or not w.get("dir"):
            raise ValueError(f"spec.workers[].dir is required for {w.get('name')}")
    regex_src = spec.get("allowlist", {}).get("regex")
    if regex_src:
        try:
            re.compile(regex_src)
        except re.error as exc:
            raise ValueError(f"spec.allowlist.regex does not compile: {exc}") from exc
    seen: set[str] = set()
    for art in spec["artifacts"]:
        name = art.get("name")
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise ValueError(f"artifact name must be a bare filename: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate artifact name: {name}")
        seen.add(name)
        total = art.get("total")
        if not isinstance(total, int) or total <= 0:
            raise ValueError(f"artifact {name}: total must be a positive int")
        parts = art.get("parts") or []
        if parts:
            if sum(parts) != total:
                raise ValueError(
                    f"artifact {name}: explicit parts sum {sum(parts)} != total {total}"
                )
            if any(p > GITHUB_ASSET_CAP for p in parts):
                raise ValueError(f"artifact {name}: a part exceeds the 2 GiB cap")
        else:
            part_size = art.get("part_size") or DEFAULT_PART_SIZE
            if part_size > GITHUB_ASSET_CAP:
                raise ValueError(f"artifact {name}: part_size exceeds the 2 GiB cap")
    return spec


def artifact_release(spec: dict, art: dict) -> str:
    """The release tag an artifact serves from (artifact wins, else first)."""
    if art.get("release"):
        return art["release"]
    upstreams = spec.get("upstreams") or []
    if upstreams and upstreams[0].get("release"):
        return upstreams[0]["release"]
    raise ValueError(f"artifact {art.get('name')}: no release and no upstreams")


def upstream_bases(spec: dict) -> list[str]:
    """ALLOWED-lane base URLs, tried in order: distinct release downloads."""
    repo = spec["store"]["repo"]
    bases: list[str] = []
    for up in spec.get("upstreams") or []:
        base = f"https://github.com/{repo}/releases/download/{up['release']}/"
        if base not in bases:
            bases.append(base)
    for art in spec.get("artifacts") or []:
        base = (
            f"https://github.com/{repo}/releases/download/"
            f"{artifact_release(spec, art)}/"
        )
        if base not in bases:
            bases.append(base)
    return bases


def whole_names(spec: dict) -> list[str]:
    """Names served whole: opt-in list + artifacts at or under the cap."""
    names = list(spec.get("allowlist", {}).get("whole") or [])
    for art in spec.get("artifacts") or []:
        if art.get("parts"):
            continue
        if art["total"] <= GITHUB_ASSET_CAP:
            names.append(art["name"])
    return names


def chunked_map(spec: dict) -> dict:
    """name → {"upstream": base-url, "parts": [{name, size}]} for big artifacts."""
    repo = spec["store"]["repo"]
    out: dict[str, dict] = {}
    for art in spec.get("artifacts") or []:
        name = art["name"]
        total = art["total"]
        explicit = art.get("parts") or []
        if not explicit and total <= GITHUB_ASSET_CAP:
            continue
        if explicit:
            # Explicit parts are SIZES (the spec contract); name them .partN.
            sizes = [
                {"name": f"{name}.part{idx}", "size": size}
                for idx, size in enumerate(explicit)
            ]
        else:
            sizes = derive_parts(name, total, art.get("part_size")
                                 or DEFAULT_PART_SIZE)
        release = artifact_release(spec, art)
        out[name] = {
            "upstream": f"https://github.com/{repo}/releases/download/{release}/",
            "parts": [{"name": p["name"], "size": p["size"]} for p in sizes],
        }
    return out


def zone_for(route: str) -> str:
    """'artifact.aitherium.com/*' → 'aitherium.com' (the registrable part)."""
    host = route.split("/")[0]
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def workers(spec: dict) -> list[dict]:
    """Worker deployment entries; the `workers:` list wins over store.*.

    Each entry carries name, dir (relative to the spec's parent), route
    (optional — workers.dev only when absent), r2_binding, r2_bucket. The
    multi-worker form is how ONE spec drives both artifact.aitherium.com and
    weights.aitherium.com from the same data.
    """
    listed = spec.get("workers")
    if listed:
        return listed
    store = spec["store"]
    return [
        {
            "name": store.get("worker_name") or store["name"],
            "dir": store.get("worker_name") or store["name"],
            "route": store.get("route"),
            "r2_binding": store.get("r2_binding", "WEIGHTS"),
            "r2_bucket": store.get("r2_bucket", "aither-artifacts"),
        }
    ]
