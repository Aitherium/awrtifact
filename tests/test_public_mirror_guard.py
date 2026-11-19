"""The public mirror refuses brand/Discord packs (owner 2026-11-17)."""

import pytest
from awrtifact import gh


@pytest.mark.parametrize(
    "release,path",
    [
        ("discord-pack-2026-11-13", "x/aitherium-collective-discord-pack.zip.part0"),
        ("discord-expression-pack-v1", "pack.zip.part0"),
        ("fleet-v1", "x/stickers-v2.zip.part0"),
        ("fleet-v1", "aither_emoji.zip"),
    ],
)
def test_pack_upload_to_public_mirror_is_refused(monkeypatch, release, path):
    calls = []
    monkeypatch.setattr(gh, "_api", lambda: False)
    monkeypatch.setattr(gh, "_run", lambda args: calls.append(args))
    with pytest.raises(gh.PublicMirrorRefusedError):
        gh.upload("Aitherium/AitherKVCache", release, path)
    with pytest.raises(gh.PublicMirrorRefusedError):
        gh.create_release("aitherium/aitherkvcache", release, f"awrtifact: {path}", "n")
    assert calls == []


def test_model_mirror_and_other_repos_still_pass():
    gh.refuse_pack_on_public_mirror(
        "Aitherium/aitherkvcache", "hf-hexgrad--Kokoro-82M-f3ff357", "kokoro.part0"
    )
    gh.refuse_pack_on_public_mirror(
        "Aitherium/brand-packs", "discord-pack-2026-11-13", "discord.zip.part0"
    )
