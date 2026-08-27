import json
from pathlib import Path

from app.voice_assets import VoiceAssetLibrary


def test_voice_asset_library_selects_only_the_accounts_own_clip_and_avoids_adjacent_repeat(tmp_path: Path):
    account_dir = tmp_path / "acct-1"
    account_dir.mkdir()
    (account_dir / "a.ogg").write_bytes(b"voice-a")
    (account_dir / "b.ogg").write_bytes(b"voice-b")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "accounts": {
                    "acct-1": {
                        "clips": [
                            {"id": "a", "path": "acct-1/a.ogg", "text": "甲"},
                            {"id": "b", "path": "acct-1/b.ogg", "text": "乙"},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    library = VoiceAssetLibrary(tmp_path)
    today = library.asset_for_day("acct-1", 100)
    tomorrow = library.asset_for_day("acct-1", 101)

    assert today is not None
    assert tomorrow is not None
    assert today.kind == "voice"
    assert today.data in {b"voice-a", b"voice-b"}
    assert tomorrow.data in {b"voice-a", b"voice-b"}
    assert today.filename != tomorrow.filename
    assert library.asset_for_day("acct-2", 100) is None


def test_voice_asset_library_rejects_manifest_path_outside_asset_root(tmp_path: Path):
    outside = tmp_path.parent / "outside.ogg"
    outside.write_bytes(b"must-not-be-read")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "accounts": {
                    "acct-1": {
                        "clips": [
                            {"id": "escape", "path": "../outside.ogg", "text": ""}
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    library = VoiceAssetLibrary(tmp_path)

    assert library.asset_for_day("acct-1", 100) is None
