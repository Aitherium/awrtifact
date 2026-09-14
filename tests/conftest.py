"""Test-wide pins.

Every gh-shaped test monkeypatches `gh._run`; the backend selector must
therefore never route those fakes to the real REST API — a CI box that
carries a GH_TOKEN but no `gh` binary would otherwise hit api.github.com
with fixture data. Pin `cli` for the whole suite; ghapi's own tests fake
`_request` directly.
"""

import pytest


@pytest.fixture(autouse=True)
def _pin_cli_backend(monkeypatch):
    monkeypatch.setenv("AWRTIFACT_GH_BACKEND", "cli")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
