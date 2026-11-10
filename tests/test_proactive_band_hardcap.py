"""短碎硬上限與閘門計數跨重啟留存。

21:36–21:59 四號全隱形：那一小時每一則都還是 23~26 字（short 帶 8~16 形同無效）、
而且三次部署重啟把「卡在哪一關」的計數全部抹掉，只能靠猜。這兩條守住證據。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import load_settings
from app.worker import AccountWorker


def _worker() -> AccountWorker:
    config = load_settings()
    config.min_typing_delay = 0.0
    config.max_typing_delay = 0.0
    return AccountWorker(
        account_id="101",
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=SimpleNamespace(),
        db=SimpleNamespace(),
        config=config,
        managed_ids={101},
        active_ids={101},
        managed_origins={},
        human_owners={},
        recent_proactive_owners={},
        last_human_activity={},
        on_status_change=None,
    )


def test_short_band_drops_the_scene_preamble():
    # 前情 + 挑釁二段式是本群長年的模子，短碎模式要真的只剩後半
    assert (
        AccountWorker._strip_lead_to_tail(
            "剛點好香氛蠟燭大腿還微酸，想不想吃我的雞雞", 18
        )
        == "想不想吃我的雞雞"
    )
    # 只有一句、沒有逗點 → 沒得砍
    assert AccountWorker._strip_lead_to_tail("想不想吃我的雞雞", 18) == ""
    # 後半太短（「真的假的」5 字）不算，交給標點裁切
    assert AccountWorker._strip_lead_to_tail("剛洗完澡頭髮還滴水，真的假的", 18) == ""
    # 後半還是太長 → 不算
    assert (
        AccountWorker._strip_lead_to_tail(
            "剛洗完澡頭髮還滴水，多人運動真的母湯一對一才專心", 12
        )
        == ""
    )
    # 尾端句號要先去掉，不然長度多算一個字
    assert AccountWorker._strip_lead_to_tail("擦著乳液看戲，換你上場了吧。", 12) == "換你上場了吧"


def test_pcount_is_flushed_to_db_and_survives_restart_key():
    worker = _worker()
    worker.db = SimpleNamespace(
        get_runtime_setting=AsyncMock(return_value=None),
        set_runtime_settings=AsyncMock(),
    )
    # 擋掉自動 flush，手動跑，才不會跟排程任務搶
    worker._pcount_flushed_at = time.time() + 3600
    worker._pcount("band_short")
    worker._pcount("band_short")
    worker._pcount("pskip_hot")
    asyncio.run(worker._flush_pcounts())
    key, payload = worker.db.set_runtime_settings.await_args.args[0].popitem()
    assert key == f"pskip:{worker._today_index()}:101"
    assert '"band_short": 2' in payload
    assert worker._pcount_pending == {}
    # 記憶體統計照舊要跳
    assert worker.stats["band_short"] == 2


def test_pcount_flush_merges_with_existing_day_counter():
    worker = _worker()
    worker.db = SimpleNamespace(
        get_runtime_setting=AsyncMock(return_value='{"pskip_slot": 5}'),
        set_runtime_settings=AsyncMock(),
    )
    worker._pcount_flushed_at = time.time() + 3600
    worker._pcount("pskip_slot")
    asyncio.run(worker._flush_pcounts())
    _, payload = worker.db.set_runtime_settings.await_args.args[0].popitem()
    assert '"pskip_slot": 6' in payload


def test_restart_backfills_last_human_activity():
    worker = _worker()
    worker.selected_groups = {-1002229799107}
    now = time.time()
    worker.db = SimpleNamespace(group_last_human_ts=AsyncMock(return_value=now - 30))
    asyncio.run(worker._restore_last_human_activity())
    # 沒有回填時 last_human_activity 是空的 → tier 判成 paused，額度一回填就整組閉嘴
    assert worker.last_human_activity[-1002229799107] == now - 30
    assert worker._proactive_rate_limit_ok(-1002229799107) == "normal"


def test_restart_backfill_keeps_newer_in_memory_value():
    worker = _worker()
    worker.selected_groups = {-1002229799107}
    now = time.time()
    worker.last_human_activity[-1002229799107] = now
    worker.db = SimpleNamespace(group_last_human_ts=AsyncMock(return_value=now - 600))
    asyncio.run(worker._restore_last_human_activity())
    assert worker.last_human_activity[-1002229799107] == now


# ---- 反向棘輪：主動路徑也吃（15:13–16:13 實測 45 則全在 8~17、零長句）---
GROUP = -1001


def test_short_flow_forces_dirty_band_on_proactive(monkeypatch):
    """整批縮死時主動發言不准再抽短碎，至少要有一則出帶。"""

    async def main():
        worker = _worker()
        now = time.time()
        worker._recent_texts_by_group[GROUP] = [
            "聽得我背脊發涼欸",
            "這數字看得我想睡",
            "下午犯睏，想被人按在床上",
            "也太衰了吧",
        ]
        worker._recent_texts_at[GROUP] = [now - 60] * 4

        class _DB:
            async def get_group_messages(self, group_id, limit=100):
                return [
                    {
                        "sender_name": "風口浪尖",
                        "sender_id": 5,
                        "role": "user",
                        "content": "真的假的",
                        "timestamp": time.time(),
                    }
                ]

            async def get_group_shared_notes(self, group_id, account_id, limit=10):
                return []

            async def recent_bot_texts_by_group(self, group_id):
                return []

        worker.db = _DB()
        worker._call_ai = AsyncMock(return_value="想被人按在牆上親")
        monkeypatch.setattr("app.worker.random.random", lambda: 0.1)
        await worker._generate_context_topic(GROUP)

        assert worker.stats.get("band_flow_short_rebound") == 1
        assert "band_dirty" in worker.stats
        assert "band_short" not in worker.stats

    asyncio.run(main())
