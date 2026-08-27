import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.media import MediaAsset
from app.worker import AccountWorker


class _VoiceDB:
    def __init__(self):
        self.messages = []
        self.activities = []
        self.claims = []

    async def claim_daily_voice(self, *args, **kwargs):
        self.claims.append((args, kwargs))
        return True

    async def add_message(self, *args):
        self.messages.append(args)

    async def touch_activity(self, *args):
        self.activities.append(args)


def _rt_config(**over):
    base = dict(
        voice_media_enabled=True,
        media_enabled=True,
        voice_realtime_url="https://tunnel.example",
        voice_realtime_token="tok",
        voice_realtime_daily_max=3,
        voice_assets_dir="/nonexistent",
        voice_daily_pre_gen=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _rt_worker(
    persona_age: int = 29,
    config: SimpleNamespace | None = None,
    due: bool = True,
):
    db = _VoiceDB()
    worker = AccountWorker(
        account_id="acct-rt",
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=None,
        db=db,
        config=config or _rt_config(),
        managed_ids=set(),
        on_status_change=lambda *args: None,
        selected_groups=[-1009],
        voice_library=None,
    )
    worker.is_running = True
    worker.tg_client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
    worker.tg_user_id = 77
    worker.persona = dict(worker.persona, age=persona_age)
    worker._realtime_voice_due = lambda current: due  # noqa: SLF001
    return worker, db


def test_voice_profile_key_maps_age_to_authorized_profiles():
    assert AccountWorker._voice_profile_key({"age": 21}) == "21"
    assert AccountWorker._voice_profile_key({"age": 25}) == "25"
    assert AccountWorker._voice_profile_key({"age": 29}) == "29"
    assert AccountWorker._voice_profile_key({"age": 34}) == "34"
    assert AccountWorker._voice_profile_key({"age": None}) == "21"


def test_realtime_voice_requires_enabled_config_and_quota():
    async def main():
        w2, _ = _rt_worker()
        w2._realtime_voice_due = AccountWorker._realtime_voice_due.__get__(w2)
        assert w2._realtime_voice_due(time.time()) is True
        # 額度用畢
        w2._realtime_voice_today = 3
        assert w2._realtime_voice_due(time.time()) is False
        # 間隔 45 分鐘內
        w2._realtime_voice_today = 0
        w2._last_realtime_voice = time.time() - 10 * 60
        assert w2._realtime_voice_due(time.time()) is False
        # 20:00-22:00 高峰（HKT）
        w2._last_realtime_voice = 0
        import datetime

        busy_hkt = datetime.datetime(2026, 8, 27, 20, 30, tzinfo=datetime.timezone.utc)
        assert w2._realtime_voice_due(busy_hkt.timestamp() - 8 * 3600) is False
        # 沒有 selected_groups
        w3, _ = _rt_worker()
        w3._realtime_voice_due = AccountWorker._realtime_voice_due.__get__(w3)
        w3.selected_groups = set()
        assert w3._realtime_voice_due(time.time()) is False
        # 未設定 url/token 時直接不可用
        w4, _ = _rt_worker(config=_rt_config(voice_realtime_url=""))
        w4._realtime_voice_due = AccountWorker._realtime_voice_due.__get__(w4)
        assert w4._realtime_voice_due(time.time()) is False

    asyncio.run(main())


def test_realtime_voice_sends_voice_note_and_counts_quota():
    async def main():
        worker, db = _rt_worker(persona_age=29)
        ogg = b"OggS" + b"\x00" * 5000
        worker._synthesize_realtime_voice = AsyncMock(return_value=MediaAsset(
            "voice", ogg, "rt.ogg", "audio/ogg"
        ))
        assert await worker._send_realtime_voice(-1009, "今天天氣不錯") is True
        worker._synthesize_realtime_voice.assert_awaited_once()
        assert worker.tg_client.send_file.await_count == 1
        kwargs = worker.tg_client.send_file.await_args.kwargs
        assert kwargs["voice_note"] is True
        assert worker._realtime_voice_today == 1
        assert db.activities == [("acct-rt", -1009, "voice_realtime")]
        # 再發一次仍成功（額度 3）
        assert await worker._send_realtime_voice(-1009, "第二條") is True
        assert worker._realtime_voice_today == 2

    asyncio.run(main())


def test_realtime_voice_failure_returns_false_and_keeps_quota():
    async def main():
        worker, db = _rt_worker()
        worker._synthesize_realtime_voice = AsyncMock(return_value=None)
        assert await worker._send_realtime_voice(-1009, "合成失敗") is False
        assert worker._realtime_voice_today == 0
        assert db.messages == []
        worker.tg_client.send_file.assert_not_awaited()

    asyncio.run(main())


def test_synthesize_realtime_voice_fails_closed_on_http_errors(monkeypatch):
    async def main():
        worker, _ = _rt_worker()

        class _Resp:
            status_code = 503
            content = b""

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        assert await worker._synthesize_realtime_voice("測試文字") is None
        assert worker.stats["voice_realtime_errors"] == 1

        # 過短的回應也視為失敗
        class _ShortResp:
            status_code = 200
            content = b"OggS123"

        class _ShortClient(_Client):
            async def post(self, *a, **k):
                return _ShortResp()

        monkeypatch.setattr(httpx, "AsyncClient", _ShortClient)
        assert await worker._synthesize_realtime_voice("測試文字") is None
        assert worker.stats["voice_realtime_errors"] == 2

    asyncio.run(main())
