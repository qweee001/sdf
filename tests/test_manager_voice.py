import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

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


def test_manager_exposes_voice_available_only_with_realtime_credentials(tmp_path):
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config(tmp_path)
        cfg.voice_realtime_url = ""
        cfg.voice_realtime_token = ""
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        assert manager.feature_status()["voice_available"] is False
        assert manager.feature_status()["voice_enabled"] is False

        # 即時 IndexTTS2 credentials 不完整時不允許開啟。
        err = await manager.update_feature_flags(
            media_enabled=True, voice_enabled=True
        )
        assert err != ""
        assert manager.config.voice_media_enabled is False

        cfg.voice_realtime_url = "https://voice.example"
        cfg.voice_realtime_token = "voice-token"
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


def test_manager_never_injects_pregenerated_voice_library(tmp_path):
    if os.path.exists(DB):
        os.remove(DB)

    async def main():
        db = Database(DB)
        await db.connect()
        cfg = _config(tmp_path)
        cfg.voice_realtime_url = "https://voice.example"
        cfg.voice_realtime_token = "voice-token"
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

        from app.worker import AccountWorker

        with patch.object(
            AccountWorker, "start", new=fake_start
        ):
            await manager._start_account(acc)
        worker = worker_holder.get("w")
        assert worker is not None
        assert worker.voice_library is None
        await db.close()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("account_id", "age"),
    [
        ("2ce525dfb0d4", 21),
        ("faa9a202f96e", 25),
        ("038632e4395b", 29),
        ("e63e27a4340d", 34),
    ],
)
def test_fixed_persona_cannot_update_or_regenerate_while_live_test_active(
    tmp_path, account_id, age
):
    async def main():
        db = Database(str(tmp_path / "fixed-persona-active.db"))
        await db.connect()
        cfg = _config(tmp_path)
        box = SecretBox(cfg.account_encryption_key)
        manager = AccountManager(cfg, db, box)
        original = {
            "name": f"固定{age}",
            "gender": "女",
            "age": age,
            "city": "台北",
        }
        await db.create_account(
            account_id,
            f"固定{age}",
            box.encrypt("session-string"),
            json.dumps(original, ensure_ascii=False),
        )
        worker = type("Worker", (), {})()
        worker.persona = dict(original)
        worker.name = original["name"]
        worker.is_running = True
        manager.workers[account_id] = worker
        manager.live_test.outbound_gate.activate(
            run_id="persona-lock",
            account_ids=[account_id],
            group_id=-5428680940,
        )

        changed = dict(original, name="被竄改", age=99)
        assert await manager.update_persona(account_id, changed) is None
        assert await manager.regen_persona(account_id) is None
        assert manager.live_test.outbound_gate.lockdown("persona-lock") is True
        assert await manager.update_persona(account_id, changed) is None
        manager.live_test.outbound_gate.deactivate("persona-lock")
        with patch.object(
            db,
            "get_live_test_reconciliation_run",
            AsyncMock(
                return_value={
                    "id": "persona-lock",
                    "status": "needs_reconciliation",
                    "account_ids": [account_id],
                }
            ),
        ):
            assert await manager.regen_persona(account_id) is None
        persisted = await db.get_account(account_id)
        assert json.loads(persisted["persona"]) == original
        assert worker.persona == original
        await manager._media_service.aclose()
        await manager._ai_client.close()
        await db.close()

    asyncio.run(main())
