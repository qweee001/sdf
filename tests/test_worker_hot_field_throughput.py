"""熱場吞吐：插話取消預算、超長就地收尾、播報重寫的開場去重。

20:00 前一小時群裡 209 則真人、水軍只發出 7 則（3.1%），Chen 整整一小時零發言。
audit 顯示同一小時取消 48 次、too_long 丟棄 23 次——不是內容壞掉，是生成還沒回來
就被下一個路人的閒聊殺掉，或寫長一點整則作廢。這幾條守住吞吐。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


def test_interrupt_budget_scales_with_human_rate_and_caps():
    now = time.time()
    assert AccountWorker._interrupt_budget([], now) == 25.0
    assert AccountWorker._interrupt_budget([now - 5], now) == 25.0
    assert AccountWorker._interrupt_budget([now - 5, now - 20], now) == 37.0
    assert AccountWorker._interrupt_budget(
        [now - 1, now - 10, now - 30], now
    ) == 49.0
    # 這一群實測 3.6 則／分，必須明顯大於原本的 25 秒
    hot = [now - i for i in range(1, 8)]
    assert AccountWorker._interrupt_budget(hot, now) == 65.0
    # 窗外舊訊息不計入速率
    assert AccountWorker._interrupt_budget([now - 300], now) == 25.0


def test_trim_to_cap_cuts_at_comma_instead_of_dropping():
    long_line = "剛洗完澡頭髮還在滴水，多人運動真的母湯，一對一才能專心感受深淺嘛，還有那個聲音"
    out = AccountWorker._trim_to_cap(long_line, 20)
    # 收在 20 字以內的最後一個逗號，能留多少留多少
    assert out == "剛洗完澡頭髮還在滴水，多人運動真的母湯"
    assert len(out) <= 20
    # 沒有好逗點可收就交回重寫
    assert AccountWorker._trim_to_cap("字" * 40, 20) == ""
    # 本來就沒超長
    assert AccountWorker._trim_to_cap("短短的", 20) == ""
    # 剪完太短也不採用
    assert AccountWorker._trim_to_cap("短，後面超長" + "字" * 40, 10) == ""


def test_unowned_self_scene_opener_is_not_press_box():
    """「剛敷完面膜躺下」沒有身體字、也沒有「我」，但那是她自己正在做的事。

    誤判成播報會白跑一次重寫（多十幾秒，熱場等於整條被取消）。
    """
    assert AccountWorker._is_press_box("剛敷完面膜躺下，單相思還敢叫老婆，問過人家了嗎") is False
    assert AccountWorker._is_press_box("剛下班順路買了杯珍奶，甜到有點膩") is False
    assert AccountWorker._is_press_box("剛洗完澡頭髮還在滴水，一對一才能專心") is False
    # 純評語照樣要被判播報
    assert AccountWorker._is_press_box("黑的才性感啊，哥哥們不懂欣賞") is True
    assert AccountWorker._is_press_box("酒都不會喝還想聊穴，先長大再說嘛") is True


def test_cross_text_seed_backfills_only_bot_lines_and_throttles():
    """部署／重啟後共享桶是空的，跨號閘門會全部失效（20:31 重啟、20:33 立刻又
    寫出純評語）。回填只吃水軍句子，且 90 秒內不重打 DB。"""
    import time as _time

    async def main():
        worker = _worker()
        now = _time.time()
        rows = [
            {"role": "assistant", "content": "剛洗完澡頭髮還在滴水", "timestamp": now - 60},
            {"role": "user", "content": "真人講的話", "timestamp": now - 30},
            {"role": "assistant", "content": "黑的才性感啊", "timestamp": now - 120},
            {"role": "assistant", "content": "黑的才性感啊", "timestamp": now - 120},
            {"role": "assistant", "content": "太久以前講的", "timestamp": now - 99999},
        ]
        worker.db = SimpleNamespace(get_group_messages=AsyncMock(return_value=rows))
        await worker._seed_cross_texts(-100)
        # 桶子是時序（舊到新），後面的判定才把最後一則當「剛剛」
        assert worker._recent_texts_by_group[-100] == ["黑的才性感啊", "剛洗完澡頭髮還在滴水"]
        assert worker._recent_texts_at[-100][0] == now - 120
        # 90 秒內再呼叫不重打 DB
        await worker._seed_cross_texts(-100)
        assert worker.db.get_group_messages.await_count == 1

    asyncio.run(main())


def test_recent_opener_note_lists_only_the_opening_action():
    async def main():
        worker = _worker()
        worker._recent_bot_lines = AsyncMock(
            return_value="[小小] 剛洗完澡頭髮還在滴水，多人運動真的母湯\n[RICH] 剛敷完面膜躺下，單相思還敢叫老婆"
        )
        note = await worker._recent_opener_note(-100)
        assert "剛洗完澡頭髮還在滴水" in note
        assert "剛敷完面膜躺下" in note
        assert "母湯" not in note
        assert note.count("、") == 1

    asyncio.run(main())


def test_press_box_rewrite_feeds_used_openers_and_stays_cheap():
    async def main():
        worker = _worker()
        worker._echo_shape_hits = Mock(return_value=3)
        worker._recent_bot_lines = AsyncMock(return_value="[小小] 剛洗完澡頭髮還在滴水，母湯")
        worker._recent_opener_note = AsyncMock(return_value="剛洗完澡頭髮還在滴水")
        captured = {}

        async def fake_pick(context, directive, base_hint, generate, **kwargs):
            captured["hint"] = base_hint
            captured["limit"] = kwargs.get("limit")
            return "在等外送的時候一直聞到隔壁的鹹酥雞味，餓到肚子在叫"

        worker._pick_passing_candidate = fake_pick
        worker._mark_echo_if_any = Mock()
        out = await worker._proactive_echo_rewrite(-100, "打成蛋餅是怎樣", "", [], "想被打成蛋餅了")
        assert out.startswith("在等外送")
        assert "剛洗完澡頭髮還在滴水" in captured["hint"]
        assert "換一個不一樣的當下場景" in captured["hint"]
        # 熱場一次生成就要有結果，不再跑兩條策略各審一遍
        assert captured["limit"] == 1

    asyncio.run(main())


def test_press_box_rewrite_skipped_when_field_is_not_echoing():
    async def main():
        worker = _worker()
        worker._echo_shape_hits = Mock(return_value=0)
        worker._pick_passing_candidate = AsyncMock(return_value="不該被叫到")
        assert await worker._proactive_echo_rewrite(-100, "黑的才性感啊", "", [], "") == ""

    asyncio.run(main())
