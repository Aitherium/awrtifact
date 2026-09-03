"""serve-spec determinism + escape correctness.

The generator must be deterministic (regenerate → identical bytes, the
diff-gate) and the rendered JS must contain the escapes the browser needs —
a single-backslash regex (dot-escaped) and a JS string newline escape. Both
are pinned here because the template's raw-string escaping is exactly the
kind of thing that looks right in review and breaks at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from awrtifact import serve_spec


def _spec() -> dict:
    return {
        "store": {
            "name": "awrtifact",
            "repo": "Aitherium/aitherkvcache",
            "compatibility_date": "2026-01-01",
        },
        "workers": [
            {
                "name": "awrtifact",
                "dir": "awrtifact",
                "route": "artifact.aitherium.com/*",
                "r2_binding": "WEIGHTS",
                "r2_bucket": "aither-artifacts",
            },
            {
                "name": "bonsai-weights",
                "dir": "bonsai-weights",
                "route": "weights.aitherium.com/*",
                "r2_binding": "WEIGHTS",
                "r2_bucket": "aither-model-weights",
            },
        ],
        "upstreams": [{"release": "artifact-v1"}],
        "allowlist": {"regex": r"^[A-Za-z0-9._-]+\.(gguf|bin)$", "whole": ["vae.bin"]},
        "artifacts": [
            {
                "id": "small",
                "name": "small.gguf",
                "source_url": "https://example.com/small.gguf",
                "total": 1000,
                "release": "artifact-v1",
            },
            {
                "id": "big",
                "name": "big.gguf",
                "source_url": "https://example.com/big.gguf",
                "total": 2_000_000_000 + 500_000_000,
                "release": "artifact-v1",
                "part_size": 1_900_000_000,
            },
        ],
    }


def test_render_deterministic(tmp_path):
    js1, toml1 = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    js2, toml2 = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    assert js1 == js2
    assert toml1 == toml2


def _spec_in(tmp_path) -> Path:
    """Real layout: the spec lives INSIDE its worker's dir, so worker dirs
    resolve one level up (spec dir's parent)."""
    d = tmp_path / "awrtifact"
    d.mkdir(exist_ok=True)
    spec_path = d / "awrtifact.yaml"
    spec_path.write_text("x")
    return spec_path


def test_emit_writes_every_worker(tmp_path):
    spec = _spec()
    spec_path = _spec_in(tmp_path)
    dirs = serve_spec.emit(spec, spec_path)
    assert {d.name for d in dirs} == {"awrtifact", "bonsai-weights"}
    for d in dirs:
        assert (d / "index.js").is_file()
        assert (d / "wrangler.toml").is_file()
    toml = (tmp_path / "bonsai-weights" / "wrangler.toml").read_text(encoding="utf-8")
    assert "weights.aitherium.com/*" in toml
    assert "aither-model-weights" in toml


def test_emit_check_passes_after_emit(tmp_path):
    spec = _spec()
    spec_path = _spec_in(tmp_path)
    serve_spec.emit(spec, spec_path)
    # A fresh emit-check must pass (determinism), and it must mutate nothing.
    target = tmp_path / "awrtifact" / "index.js"
    mtime = target.stat().st_mtime_ns
    serve_spec.emit(spec, spec_path, check=True)
    assert target.stat().st_mtime_ns == mtime


def test_emit_check_fails_on_tamper(tmp_path):
    spec = _spec()
    spec_path = _spec_in(tmp_path)
    serve_spec.emit(spec, spec_path)
    target = tmp_path / "awrtifact" / "index.js"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="drift"):
        serve_spec.emit(spec, spec_path, check=True)


def test_rendered_js_escapes(tmp_path):
    js, _ = serve_spec.render(_spec(), tmp_path / "awrtifact.yaml")
    # JS regex literal: single backslash before the dot.
    assert r"\.(?:esm\.)?" in js
    assert "\\\\.(?:esm\\\\.)?" not in js
    # JS string escape for newline.
    assert "'bad range\\n'" in js
    # The generated data sections carry real JSON, not sentinels.
    assert "__UPSTREAMS_JSON__" not in js
    assert "__CHUNKED_JSON__" not in js
    assert "aitherkvcache" in js
    # Chunked derivation: one whole (small) + one stitched (big, 2 parts).
    assert '"small.gguf"' in js  # WHOLE set member
    assert '"big.gguf.part1"' in js


def test_prefix_route_namespaces_duplicate_names(tmp_path):
    """Same-named files in DIFFERENT releases are legal (the /<release>/ prefix
    route is their namespace); the same name+release stays an error."""
    from awrtifact import spec as spec_mod

    base = _spec()
    dup = {
        "id": "second-tokenizer",
        "name": "tokenizer.json",
        "source_url": "https://example.com/tokenizer.json",
        "total": 700,
        "release": "other-v1",
    }
    base["artifacts"] = list(base["artifacts"]) + [dup]
    # Cross-release duplicate parses and serves via its own prefix.
    spec_mod.validate(base)
    paths = spec_mod.path_upstreams(base)
    assert paths["other-v1"].endswith("/releases/download/other-v1/")
    js, _ = serve_spec.render(base, tmp_path / "awrtifact.yaml")
    assert "PATH_UPSTREAMS" in js
    assert "other-v1" in js

    # Same name+release is still ambiguous even for the prefix route:
    # "small.gguf" already lives in artifact-v1.
    same = {"id": "dup-small", "name": "small.gguf",
            "source_url": "https://example.com/small.gguf",
            "total": 999, "release": "artifact-v1"}
    base["artifacts"] = list(base["artifacts"]) + [same]
    with pytest.raises(ValueError, match="same name"):
        spec_mod.validate(base)


def test_prefix_route_is_a_namespace_for_chunked_and_r2():
    """2026-09-03: /microembedder-v2/config.json served microembedder-v1's 650-byte config
    because R2 and CHUNKED were consulted by bare name BEFORE the release prefix. The
    generated worker must skip R2 for prefixed paths and answer a chunked entry only
    when its upstream IS the requested release."""
    from awrtifact import worker_template

    src = worker_template.__file__
    text = open(src, encoding="utf-8").read()
    assert "const fromR2 = baseOverride ? null : await serveFromR2(request, env, name);" in text
    assert "CHUNKED[name].upstream === baseOverride" in text
