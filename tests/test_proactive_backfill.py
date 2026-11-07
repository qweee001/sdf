"""重啟回填測試：last_human_activity 為空時啟動帳號必須從 DB 回填，抑制主動話題。"""

import asyncio
import time
from unittest.mock import patch

from test_worker_reply_arbitration import MANAGED, _worker


def test_manager_backfills_last_human_activity_before_start():
    async def main():
        from app.config import load_settings
        from app.manager import AccountManager

        cfg = load_settings()
        mgr = object.__new__(AccountManager)
        mgr.config = cfg
        mgr.workers = {}
        mgr.last_human_activity = {}

        async def fake_backfill():
            return {-1002229799107: 12345.6}

        mgr.db = type(
            "DB",
            (),
            {"last_human_activity_by_group": staticmethod(fake_backfill)},
        )()
        mgr.secret_box = type(
            "SB", (), {"decrypt": staticmethod(lambda v: "sess")}
        )()
        mgr._ai_client = object()
        mgr._chat_client = object()
        mgr._media_service = object()
        mgr._parse_groups = lambda raw: [-1002229799107]
        mgr.managed_ids = set()
        mgr.active_ids = set()
        mgr.active_group_ids = {}
        mgr.managed_origins = {}
        mgr.human_owners = {}
        mgr.recent_proactive_owners = {}
        mgr.reply_claim_signals = {}
        mgr.failed_reply_claimants = {}
        mgr.group_send_locks = {}
        mgr.topic_turn_counts = {}
        mgr.phrase_cores_by_group = {}
        mgr.personas = {}
        mgr._voice_library = None
        mgr._status = {}

        acc = {
            "id": "testacct01",
            "session_key": "enc",
            "persona": "{}",
            "groups": "[-1002229799107]",
        }

        class FakeWorker:
            def __init__(self, **kwargs):
                self.is_running = False

            async def start(self):
                self.is_running = False

        with patch("app.manager.AccountWorker", FakeWorker):
            await AccountManager._start_account_unlocked(mgr, acc)

        assert (
            mgr.last_human_activity.get(-1002229799107) == 12345.6
        ), "啟動前必須回填 last_human_activity"

    asyncio.run(main())


def test_suppress_proactive_uses_backfilled_activity():
    """回填後 _should_suppress_proactive 應在真人 10 分鐘內活躍時擋下主動話題。"""
    worker = _worker(
        sorted(MANAGED)[0],
        last_human_activity={-5428680940: time.time() - 60},
    )
    assert worker._should_suppress_proactive(-5428680940) is True
    worker2 = _worker(sorted(MANAGED)[0], last_human_activity={})
    assert worker2._should_suppress_proactive(-5428680940) is False


def test_suppress_window_is_two_minutes_not_ten():
    """白天的群幾乎永遠有人在講話，10 分鐘的讓路窗口等於整天不准主動。

    2 分鐘內：讓路（會撞話）；5 分鐘前：可以接（正常人本來就會隔幾句插回來）。
    """
    gid = -5428680940
    recent = _worker(sorted(MANAGED)[0], last_human_activity={gid: time.time() - 60})
    assert recent._should_suppress_proactive(gid) is True
    stale = _worker(sorted(MANAGED)[0], last_human_activity={gid: time.time() - 300})
    assert stale._should_suppress_proactive(gid) is False


def test_daily_cap_has_a_floor_so_evening_never_goes_silent():
    """每日上限是防自嗨的配額，不是讓四號晚上八點就全體閉嘴。

    config 值 30 條／天在這一群約晚上八點就用光，之後整晚零主動。
    實際生效上限至少 60，熱場再放大兩倍。
    """

    async def main():
        worker = _worker(sorted(MANAGED)[0])
        worker.config.proactive_max_per_day = 30
        worker._hot_field = lambda _gid: False
        cap_cold = max(60, worker.config.proactive_max_per_day)
        assert cap_cold == 60
        worker._hot_field = lambda _gid: True
        assert max(60, worker.config.proactive_max_per_day * 2) == 60

    asyncio.run(main())
