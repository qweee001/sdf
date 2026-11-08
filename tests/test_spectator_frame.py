"""「動作＋看」旁觀模子：剪短了字數，句型卻整批塌成同一個。

12:44~13:10 十二則水軍有六則是「（動作）看／聽（對象）」開場：
喝著冰美式聽萬英尺揪團、咬著便當看、趴在桌上滑手機看萬英尺貪心、
吸著冰手搖看你們嫌。每一則都有對象、都有自曝，四道單軸檢查逐條放行，
群友看到的卻是同一個模子——連「看」這個字都一樣。

判語流不判單句：近三則有兩則同模子才動手，剪掉前面那截動作，只留對人的那句。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1002

FRAMES = [
    "吸著冰手搖看你們嫌，沒胃口不如來吸姊姊多汁的小穴",
    "咬著便當看，不如來吃姊姊的嫩肉",
    "喝著冰美式聽萬英尺揪團都濕了",
    "揉著痠腳看萬英尺連搶老婆都只剩張嘴",
    "後天，誰要聽他唱歌，我現在靠在櫃台伸懶腰比較想聽你在床上喘",
]

NOT_FRAMES = [
    "萬英尺談戀愛要把人按在床上親才算數吧",
    "應該讓你們來舔我才對吧",
    "嫌他雞雞小，那哥哥拿你的來塞滿我小穴看看有多大",
    "還是滷味香吧",
]


def _worker() -> AccountWorker:
    config = SimpleNamespace(
        ai_model="m",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=1024,
        decision_enabled=False,
    )
    w = AccountWorker(
        account_id="w1",
        session_key="k",
        tg_api_id=1,
        tg_api_hash="h",
        ai_client=cast(Any, None),
        db=cast(Any, SimpleNamespace()),
        config=config,
        managed_ids=set(),
        on_status_change=lambda *_a, **_k: None,
        selected_groups=[GID],
    )
    w.persona = {"name": "Chen", "gender": "女", "age": 38}
    return w


def test_action_plus_watch_is_the_frame():
    for text in FRAMES:
        assert AccountWorker._is_watch_frame(text), text


def test_direct_lines_are_not_the_frame():
    for text in NOT_FRAMES:
        assert not AccountWorker._is_watch_frame(text), text


def test_frame_needs_two_of_the_last_three():
    w = _worker()
    now = time.time()
    w._recent_texts_by_group[GID] = FRAMES[:2]
    w._recent_texts_at[GID] = [now - 30, now - 90]
    assert w._watch_frame_flow(GID) == 2
    w._recent_texts_by_group[GID] = NOT_FRAMES[:2]
    assert w._watch_frame_flow(GID) == 0


def test_strip_keeps_the_part_aimed_at_people():
    assert (
        AccountWorker._strip_spectator_head("吸著冰手搖看你們嫌，沒胃口不如來吸姊姊多汁的小穴")
        == "沒胃口不如來吸姊姊多汁的小穴"
    )
    # 沒有逗點可剪就回空字串，不硬切
    assert AccountWorker._strip_spectator_head("喝著冰美式聽萬英尺揪團都濕了") == ""
