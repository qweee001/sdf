import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import Database
from app.media import MediaAsset
from app.worker import AccountWorker


class _VoiceLibrary:
    def __init__(self):
        self.calls = []

    def asset_for_day(self, account_id: str, day_index: int) -> MediaAsset:
        self.calls.append((account_id, day_index))
        return MediaAsset("voice", b"ogg-audio", f"{account_id}-{day_index}.ogg", "audio/ogg")


class _VoiceDB:
    def __init__(self):
        self.claimed = False
        self.claim_calls = []
        self.messages = []
        self.activities = []

    async def claim_daily_voice(
        self,
        account_id: str,
        day_index: int,
        group_id: int | None = None,
        min_interval_seconds: float = 30 * 60,
    ) -> bool:
        self.claim_calls.append((account_id, day_index, group_id))
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


def test_worker_daily_pregenerated_voice_is_always_fail_closed():
    async def main():
        db = _VoiceDB()
        worker = _voice_worker(db, enabled=False)
        now = _hkt_timestamp(100, 23, 59)

        assert await worker._maybe_send_daily_voice(now=now) is False
        assert db.claim_calls == []
        worker.tg_client.send_file.assert_not_awaited()

        worker.config.voice_media_enabled = True
        assert await worker._maybe_send_daily_voice(now=now) is False
        assert worker.voice_library.calls == []
        assert db.claim_calls == []
        worker.tg_client.send_file.assert_not_awaited()
        assert db.messages == []
        assert db.activities == []
        assert worker.stats["voice_proactive_sent"] == 0

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


def test_daily_voice_group_separation_blocks_second_account_within_30_minutes(tmp_path: Path):
    """同一群 30 分鐘內只允許一個帳號的每日語音（防止 14:15/14:16 同時兩條）。"""
    async def main():
        path = str(tmp_path / "voice-group.db")
        first = Database(path)
        second = Database(path)
        await first.connect()
        await second.connect()
        try:
            assert await first.claim_daily_voice("acct-1", 100, group_id=-1001) is True
            await first.touch_activity("acct-1", -1001, "voice_proactive")
            # 同群另一個帳號：30 分鐘內被拒
            assert await second.claim_daily_voice("acct-2", 100, group_id=-1001) is False
            # 同群同一帳號重試也被拒（每日一次）
            assert await first.claim_daily_voice("acct-1", 100, group_id=-1001) is False
            # 30 分鐘前之後的 activity 不影響：把 activity 時點改舊
            await first._c.execute(
                "UPDATE activity SET at = at - 31 * 60 "
                "WHERE group_id = -1001 AND kind = 'voice_proactive'"
            )
            await first._c.commit()
            assert await second.claim_daily_voice("acct-2", 100, group_id=-1001) is True
            # 不同群不受影響
            assert await first.claim_daily_voice("acct-3", 100, group_id=-1002) is True
        finally:
            await first.close()
            await second.close()

    asyncio.run(main())


def test_worker_does_not_send_when_group_claim_is_refused():
    async def main():
        db = _VoiceDB()

        async def refuse(*args, **kwargs):
            return False

        db.claim_daily_voice = refuse
        worker = _voice_worker(db)
        now = _hkt_timestamp(100, 23, 59)
        assert await worker._maybe_send_daily_voice(now=now) is False
        worker.tg_client.send_file.assert_not_awaited()
        assert db.messages == []

    asyncio.run(main())


def test_worker_start_never_creates_daily_voice_loop_even_if_flag_true():
    import inspect

    assert "_daily_voice_loop" not in inspect.getsource(AccountWorker.start)
