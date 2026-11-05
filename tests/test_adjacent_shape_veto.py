"""相鄰撞格與動作重複的硬自檢（2026-11-05 13:40–14:12 桃花源實測破口）。

- 13:40 小小「害我剛在位子伸個懶腰差點把扣子繃開」→ 13:41 小天後「剛補完防曬身上
  還熱著」：字面不同、同一分鐘，起頭字頻率還沒累積到 2 次，舊的頻率門檻攔不到。
- 14:06 RICH「吹著冷氣伸懶腰內衣勒得有點悶」：26 分鐘後再做一次同一個動作，
  場景字串不同，群友看到的是同一格。
- 13:15～13:52 十一則裡五則純評語，但沒有一則是連著的，連兩則才攔的規則整批放過。

全部動態：比對的是本群實際送出紀錄，不是禁用詞清單。
"""

from types import SimpleNamespace
from typing import Any, cast
import time

from app.worker import AccountWorker

GID = -1001


def _worker(name: str = "小天後") -> AccountWorker:
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
        selected_groups=[GID],
    )
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    """pairs＝[(幾秒前, 句子)]，由舊到新。"""
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def test_vetoes_same_opening_char_on_the_line_right_before():
    w = _worker()
    _seed(w, [(90, "這副看不起人的拽樣超欠捏，害我剛在位子伸個懶腰差點把扣子繃開")])
    veto = w._local_veto("剛補完防曬身上還熱著，那麼小塞進來我大概連叫都懶得叫", [], "", GID)
    assert veto
    assert "剛" in veto and "上一則" in veto


def test_same_opening_char_far_apart_in_window_is_not_blocked():
    w = _worker()
    _seed(w, [(11 * 60, "剛脫高跟鞋腳底板快斷了，只想找個人大力揉一揉")])
    veto = w._local_veto("蹲在冰箱前面找東西吃，冷氣吹得小腿起雞皮疙瘩", [], "", GID)
    assert veto == ""


def test_vetoes_body_gesture_done_by_another_account_26_minutes_ago():
    w = _worker("RICH")
    _seed(
        w,
        [
            (26 * 60, "這副看不起人的拽樣超欠捏，害我剛在位子伸個懶腰差點把扣子繃開"),
            (60, "你朋友對你這麼好"),
        ],
    )
    veto = w._local_veto(
        "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好", [], "", GID
    )
    assert veto
    assert "懶腰" in veto


def test_vetoes_press_box_when_two_thirds_of_recent_bots_are_press_box():
    w = _worker()
    _seed(
        w,
        [
            (25 * 60, "被鱷魚咬斷直接不用當男人，群裡弟弟誰敢拿雞雞去賭"),
            (22 * 60, "姊姊約過兩次，有些弟弟床上功夫硬是讓人忘不掉"),
            (18 * 60, "換這張妹子看到直接滑掉，你確定要拿來當門面嗎"),
            (7 * 60, "祈禱是哪招，真有心怎麼不叫外送熱飲過去"),
        ],
    )
    veto = w._local_veto("森森被誇可愛還急著反駁，傲嬌小狗欠調教是不是", [], "", GID)
    assert veto
    assert "講別人的事" in veto


def test_business_line_is_exempt_from_press_box_density():
    """帶人入會、找助理本來就該講外面的事，不逼她補自曝。"""
    w = _worker()
    _seed(
        w,
        [
            (25 * 60, "被鱷魚咬斷直接不用當男人，群裡弟弟誰敢拿雞雞去賭"),
            (20 * 60, "姊姊約過兩次，有些弟弟床上功夫硬是讓人忘不掉"),
            (7 * 60, "換這張妹子看到直接滑掉，你確定要拿來當門面嗎"),
        ],
    )
    veto = w._local_veto(
        "想吃得先去聯絡助理排隊，表現夠聽話姊姊才考慮給你嚐一口", [], "", GID
    )
    assert veto == ""


def test_press_box_passes_when_recent_bots_had_self_disclosure():
    w = _worker()
    _seed(
        w,
        [
            (30 * 60, "咬著手搖吸管摸魚，小腿站到發痠，出門不用付錢難道是拿腰力抵飯錢嗎"),
            (20 * 60, "剛補完防曬身上還熱著，那麼小塞進來我大概連叫都懶得叫"),
        ],
    )
    veto = w._local_veto("傲嬌小狗欠調教是不是", [], "", GID)
    assert veto == ""
