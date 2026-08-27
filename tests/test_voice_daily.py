import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import Database
from app.media import MediaAsset
from app.worker import AccountWorker


class _VoiceLibrary:
    def asset_for_day(self, account_id: str, day_index: int) -> MediaAsset:
        return MediaAsset("voice", b"ogg-audio", f"{account_id}-{day_index}.ogg", "audio/ogg")


class _VoiceDB:
    def __init__(self):
        self.claimed = False
        self.claim_calls = []
        self.messages = []
        self.activities = []

    async def claim_daily_voice(self, account_id: str, day_index: int) -> bool:
        self.claim_calls.append((account_id, day_index))
        if self.claimed:
            return False
        self.claimed = True
        return True

    async def add_message(self, *args):
        self.messages.append(args)

    async def touch_activity(self, *args):
        self.activities.append(args)


def _voice_worker(db: _VoiceDB, enabled: bool = True) -> AccountWorker:
    worker = AccountWorker(
        account_id="acct-1",
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=None,
        db=db,
        config=SimpleNamespace(voice_media_enabled=enabled),
        managed_ids=set(),
        on_status_change=lambda *args: None,
        selected_groups=[-1001],
        voice_library=_VoiceLibrary(),
    )
    worker.is_running = True
    worker.tg_client = SimpleNamespace(send_file=AsyncMock())
    worker.tg_user_id = 42
    return worker


def _hkt_timestamp(day_index: int, hour: int, minute: int = 0) -> float:
    return float(day_index * 86400 - 8 * 3600 + hour * 3600 + minute * 60)


def test_daily_voice_claim_is_atomic_per_account_and_day_across_connections(tmp_path: Path):
    async def main():
        path = str(tmp_path / "voice-claims.db")
        first = Database(path)
        second = Database(path)
        await first.connect()
        await second.connect()
        try:
            results = await asyncio.gather(
                first.claim_daily_voice("acct-1", 100),
                second.claim_daily_voice("acct-1", 100),
            )
            assert sorted(results) == [False, True]
            assert await first.claim_daily_voice("acct-2", 100) is True
            assert await second.claim_daily_voice("acct-1", 101) is True
        finally:
            await first.close()
            await second.close()

    asyncio.run(main())


def test_worker_sends_one_daily_voice_note_only_when_enabled_and_due():
    async def main():
        db = _VoiceDB()
        worker = _voice_worker(db, enabled=False)
        now = _hkt_timestamp(100, 23, 59)

        assert await worker._maybe_send_daily_voice(now=now) is False
        assert db.claim_calls == []
        worker.tg_client.send_file.assert_not_awaited()

        worker.config.voice_media_enabled = True
        assert await worker._maybe_send_daily_voice(now=now) is True
        assert await worker._maybe_send_daily_voice(now=now) is False
        worker.tg_client.send_file.assert_awaited_once()
        args, kwargs = worker.tg_client.send_file.await_args
        assert args[0] == -1001
        assert kwargs["voice_note"] is True
        assert len(db.messages) == 1
        assert db.activities == [("acct-1", -1001, "voice_proactive")]

    asyncio.run(main())


def test_worker_suppresses_daily_voice_during_taohuayuan_busy_hours_or_recent_human_activity():
    async def main():
        db = _VoiceDB()
        worker = _voice_worker(db)
        busy = _hkt_timestamp(100, 20, 30)
        assert await worker._maybe_send_daily_voice(now=busy) is False
        assert db.claim_calls == []

        quiet = _hkt_timestamp(100, 23, 59)
        worker.last_human_activity[-1001] = quiet - 60
        assert await worker._maybe_send_daily_voice(now=quiet) is False
        assert db.claim_calls == []
        worker.tg_client.send_file.assert_not_awaited()

    asyncio.run(main())
