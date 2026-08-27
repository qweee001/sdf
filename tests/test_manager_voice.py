import asyncio
import json
import os

from app.config import load_settings
from app.crypto import SecretBox
from app.database import Database
from app.manager import AccountManager
from app.media import MediaAsset

DB = "/tmp/sdf_test/manager_voice_test.db"


def _config(tmp_path):
    s = load_settings()
    s.ai_model = ""
    s.voice_assets_dir = str(tmp_path)
    return s


def _write_manifest(tmp_path, account_id: str):
    ogg = tmp_path / f"{account_id}.ogg"
    ogg.write_bytes(b"\x00\x01\x02fake-opus")
    manifest = {
        "schema_version": 1,
        "accounts": {
            account_id: {
                "clips": [{"path": ogg.name, "sha256": "x"}]
            }
        },
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_manager_exposes_voice_available_only_when_assets_exist(tmp_path):
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config(tmp_path)
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        assert manager.feature_status()["voice_available"] is False
        assert manager.feature_status()["voice_enabled"] is False

        # 無素材時不允許開啟
        err = await manager.update_feature_flags(
            media_enabled=True, voice_enabled=True
        )
        assert err != ""
        assert manager.config.voice_media_enabled is False

        # 素材就緒後才能開啟
        _write_manifest(tmp_path, "acct-1")
        err = await manager.update_feature_flags(
            media_enabled=True, voice_enabled=True
        )
        assert err == ""
        assert manager.config.voice_media_enabled is True
        assert manager.feature_status()["voice_available"] is True

        # 關閉後 fail-closed
        err = await manager.update_feature_flags(
            media_enabled=True, voice_enabled=False
        )
        assert err == ""
        assert manager.config.voice_media_enabled is False
        await db.close()

    asyncio.run(main())


def test_manager_starts_worker_with_shared_voice_library(tmp_path):
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config(tmp_path)
        _write_manifest(tmp_path, "acct-2")
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        encrypted = box.encrypt("session-string")
        await db.create_account("acct-2", "測試", encrypted, "{}")
        await db.update_account("acct-2", groups="[-10099]")
        acc = await db.get_account("acct-2")
        assert acc is not None
        worker_holder: dict = {}

        async def fake_start(self):
            self.is_running = True
            self._voice_task = None
            worker_holder["w"] = self

        from unittest.mock import patch

        from app.worker import AccountWorker

        with patch.object(
            AccountWorker, "start", new=fake_start
        ):
            await manager._start_account(acc)
        worker = worker_holder.get("w")
        assert worker is not None
        # worker 共用 manager 的素材庫，且能找到自己的片段
        asset = worker.voice_library.asset_for_day("acct-2", 0)
        assert isinstance(asset, MediaAsset)
        assert asset.data == b"\x00\x01\x02fake-opus"
        await db.close()

    asyncio.run(main())
