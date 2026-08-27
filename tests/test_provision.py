"""provision-repo decisions + the snapshot-drift guard.

The command is a thin shell over `gh`, so the decision surface is what is
tested: creates the repo when missing (private by default), reuses an
existing one, seeds every file (the gate assets byte-pinned so a forgotten
mod update is loud), and treats a Pages refusal for a private repo as a
warning, not a failure.

The seeded mod/gate assets are snapshots of `public/gobbonet/` — the pinned
hashes here are the gate that keeps them in step.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess

import pytest
from awrtifact import gh, provision


def _proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], rc, stdout=out, stderr="")


class _FakeGh:
    """Deterministic gh: repo missing until `created`, Pages refused on demand."""

    def __init__(self) -> None:
        self.created = False
        self.refuse_pages = False
        self.seeded: list[tuple[str, str]] = []  # (path, content)

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        # gh._run prepends the "gh" binary — this receives the args after it.
        rest = args
        if rest[:2] == ["repo", "view"]:
            if not self.created:
                return _proc(1, "not found")
            return _proc(0, f"https://github.com/{rest[2]}")
        if rest[:2] == ["repo", "create"]:
            self.created = True
            return _proc(0)
        if rest[0] == "api":
            if "-X" in rest and "PUT" in rest:
                # repos/o/r/contents/PATH ... -f message=... -f content=...
                path = rest[3].split("/contents/", 1)[1]
                raw = rest[7].removeprefix("content=")
                content = base64.b64decode(raw).decode()
                self.seeded.append((path, content))
                return _proc(0)

            if "-X" in rest and "POST" in rest and "pages" in rest[3]:
                return _proc(1, "refused") if self.refuse_pages else _proc(0)
        raise AssertionError(f"unexpected gh args: {rest}")


@pytest.fixture(autouse=True)
def _fake_gh(monkeypatch: pytest.MonkeyPatch) -> _FakeGh:
    fake = _FakeGh()
    monkeypatch.setattr(gh, "_run", fake.run)
    return fake


def test_creates_private_when_missing(_fake_gh: _FakeGh) -> None:
    result = provision.provision_repo("you/backups")
    assert _fake_gh.created
    assert result["html_url"] == "https://github.com/you/backups"
    assert result["pages"] is True
    assert result["gate_url"] == "https://you.github.io/backups/backup-gate.html"


def test_reuses_existing_repo(_fake_gh: _FakeGh) -> None:
    _fake_gh.created = True
    result = provision.provision_repo("you/backups")
    assert result["seeded"] == ["README.md", "gobbonet-backup.js", "backup-gate.html"]
    assert result["html_url"] == "https://github.com/you/backups"


def test_public_flag(_fake_gh: _FakeGh) -> None:
    seen: list[str] = []

    def run(args: list[str]) -> subprocess.CompletedProcess:
        if args[:2] == ["repo", "create"]:
            seen.append(args[4])
            return _proc(0)
        return _FakeGh().run(args)

    import awrtifact.gh as gh_mod  # noqa: PLC0415

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gh_mod, "_run", run)
    try:
        provision.provision_repo("you/backups", public=True)
    finally:
        monkeypatch.undo()
    assert seen == ["--public"]


def test_pages_refusal_is_warning_not_failure(_fake_gh: _FakeGh) -> None:
    _fake_gh.refuse_pages = True
    result = provision.provision_repo("you/backups")
    assert result["pages"] is False
    assert result["gate_url"] is None
    assert result["html_url"]  # repo still provisioned


def test_repo_must_be_owner_name() -> None:
    with pytest.raises(ValueError):
        provision.provision_repo("backups")


def test_seeded_assets_byte_pinned() -> None:
    """The embedded mod/gate are snapshots of public/gobbonet — a change in
    either must be deliberate. Pin their sha256 here."""
    pinned = {
        "gobbonet-backup.js": "0988cca66798dcb83a6abcf71f5b70d77b02080503d1e600c3bf3548de347981",
        "backup-gate.html": "e82764f1b3825a3844cadead5713868dd58345351050d402e62224b66f79a526",
    }
    for name, want in pinned.items():
        data = (provision.DATA_DIR / name).read_bytes()
        got = hashlib.sha256(data).hexdigest()
        assert got == want, (
            f"{name} changed (was {want[:12]}…, now {got[:12]}…) — re-copy from "
            f"public/gobbonet/ deliberately, then update this pin"
        )


def test_readme_carries_the_gate_url(_fake_gh: _FakeGh) -> None:
    provision.provision_repo("you/backups")
    readme = next(c for p, c in _fake_gh.seeded if p == "README.md")
    assert "https://you.github.io/backups/backup-gate.html" in readme
    assert "passphrase" in readme
