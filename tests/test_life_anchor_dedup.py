"""Life Anchor 動態去重：同一個帳號不能連兩則用同一個生活場景開場。

實測破口（2026-11-03 15:xx 桃花源）：
- 小天後 14:45「下午站櫃快睡著了」→ 15:37「下午站櫃犯睏」（同一個錨點，52 分鐘）
- Chen 14:06「店裡剛燙完新進的連身裙」→ 15:44「整理完店裡衣服」→ 15:57「店裡摸魚」
- 15:30 RICH 嗎收尾 → 15:37 小天後 嗎收尾（連續兩則反問）
根因：anchors 每帳號只有 4 條，random.choice 沒有記憶，3 小時內必撞。
"""

from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker


def _worker(persona_name: str = "Chen") -> AccountWorker:
    config = SimpleNamespace(
        ai_model="test-model",
        ai_temperature=0.8,
        ai_max_tokens=200,
        ai_timeout=17,
        ai_disable_thinking=True,
        memory_max_messages=10,
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        media_max_input_bytes=8 * 1024 * 1024,
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
        selected_groups=[-1001],
    )
    w.persona = {"name": persona_name, "gender": "女", "age": 38}
    return w


def _history(name: str, lines: list[str]) -> list[dict]:
    return [
        {"role": "assistant", "sender_name": name, "content": line, "timestamp": 1000.0 + i}
        for i, line in enumerate(lines)
    ]


_ANCHORS = [
    ("店裡", "「服飾店剛拉下鐵門，換掉包臀裙只穿睡袍，倒了杯紅酒，熟女家裡沒人的時候最耐不住寂寞」"),
    ("瑜珈", "「練完瑜珈全身筋骨都拉開了，筋軟的女人有多好伺候，就看哪個識貨的弟弟有福氣體會」"),
    ("店裡", "「整理進口蕾絲睡衣，看著鏡子裡的腰身線條，小女生哪懂怎麼讓男人舒服到不想下床」"),
    ("床", "「空蕩蕩的雙人床好涼，姊姊現在慾火正旺，哪個體能強悍的小狼狗敢進來交作業」"),
]


def test_used_scene_is_skipped():
    w = _worker()
    history = _history("Chen", [
        "整理完店裡衣服正想偷懶，看到你們又在當舔狗，在床上乖一點姊姊還比較愛",
        "男人腦子不裝這種事難道裝珍珠奶茶嗎，害我店裡摸魚滑手機看你們排隊搶人都看餓了",
    ])
    for _ in range(30):
        picked = w._pick_life_anchor(_ANCHORS, "Chen", history)
        assert "整理進口蕾絲睡衣" not in picked
        assert "服飾店剛拉下鐵門" not in picked


def test_other_account_history_does_not_block():
    """別的女號講過不算你用過，池子不能被別人吃掉。"""
    w = _worker()
    history = [
        {"role": "assistant", "sender_name": "RICH", "content": "整理進口蕾絲睡衣好累", "timestamp": 1.0},
    ]
    picked = w._pick_life_anchor(_ANCHORS, "Chen", history)
    assert picked in [sc for _, sc in _ANCHORS]


def test_empty_history_still_returns_anchor():
    w = _worker()
    assert w._pick_life_anchor(_ANCHORS, "Chen", []) in [sc for _, sc in _ANCHORS]


def test_directors_note_lists_own_recent_lines():
    w = _worker()
    history = _history("Chen", ["整理完店裡衣服正想偷懶，看到你們又在當舔狗"])
    note = w._build_directors_note(SimpleNamespace(sender_id=999, sender=None), history)
    assert "你最近講過" in note


def test_directors_note_forbids_question_ending_after_question():
    w = _worker()
    history = [
        {"role": "assistant", "sender_name": "RICH", "content": "還是哥哥平常也這麼好請客嗎", "timestamp": 1.0},
        {"role": "user", "sender_name": "華發", "content": "以後別吃鐵鍋燉了", "timestamp": 2.0},
    ]
    note = w._build_directors_note(SimpleNamespace(sender_id=999, sender=None), history)
    assert "結尾一定要落在斷言或動作" in note


def test_overused_terms_counts_cross_account_reuse():
    w = _worker()
    gid = -1001
    history = [
        {"role": "assistant", "sender_name": "小小", "content": "下午摸魚看男生的身材", "timestamp": 1.0},
        {"role": "assistant", "sender_name": "Chen", "content": "害我店裡摸魚滑手機", "timestamp": 2.0},
        {"role": "assistant", "sender_name": "RICH", "content": "剛在位置上伸懶腰", "timestamp": 3.0},
        {"role": "assistant", "sender_name": "小天後", "content": "站櫃站一整天腳很痠", "timestamp": 4.0},
        {"role": "assistant", "sender_name": "小小", "content": "摸魚被抓到就慘了", "timestamp": 5.0},
        {"role": "assistant", "sender_name": "Chen", "content": "誰先來餵飽我", "timestamp": 6.0},
    ]
    note = w._overused_terms_note(gid, history)
    assert "講爛的詞" in note
    assert "摸魚" in note


def test_overused_terms_empty_when_history_short():
    w = _worker()
    assert w._overused_terms_note(-1001, []) == ""


def test_overused_terms_prefers_recorded_group_texts():
    """本群實際送出紀錄優先：對話窗只有兩則水軍也要抓得出來。"""
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = [
        "下午摸魚看男生的身材",
        "害我店裡摸魚滑手機",
        "誰先來餵飽我",
    ]
    note = w._overused_terms_note(gid, [])
    assert "摸魚" in note
