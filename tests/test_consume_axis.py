"""喝的東西一小時四則：逐字不重、起手詞也全不同，群友看到的還是同一件事。

2026-11-06 10:00–11:25 實測，四個號輪流：

  10:00 Chen「剛開店門陰天好想睡覺，先喝杯熱拿鐵醒腦」
  10:03 RICH「只吃雞蛋我肚子會叫，先點手搖飲壓驚了」
  10:12 小天後「意淫六次當早餐吃嗎，我剛在櫃上喝完黑咖啡」
  11:25 RICH「剛喝完冰美式看到你說不是老婆這麼敢講，害我坐在位子上耳根都有點發燙」

拿鐵／手搖／黑咖啡／冰美式是四個字串，場景軸的逐字比對一個都中不到，
起手詞軸看到「剛開／只吃／意淫／剛喝」也全放行。八十五分鐘整場都在喝飲料。

修法走分群不走禁用詞：同群的品項算同一件事，貼身二十分鐘或兩小時內第二則就換一類。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001

_COFFEE = "剛開店門陰天好想睡覺，先喝杯熱拿鐵醒腦"
_DRINK = "只吃雞蛋我肚子會叫，先點手搖飲壓驚了"
_COFFEE2 = "意淫六次當早餐吃嗎，我剛在櫃上喝完黑咖啡"


def _worker(name: str = "RICH") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 28}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


# ---- 分群本身 ------------------------------------------------------------


def test_same_group_different_item_shares_the_group():
    w = _worker()
    assert w._consume_groups("先喝杯熱拿鐵") == {"coffee"}
    assert w._consume_groups("剛在櫃上喝完黑咖啡") == {"coffee"}
    assert w._consume_groups("先點手搖飲壓驚了") == {"drink"}
    assert w._consume_groups("剛喝完冰美式") == {"coffee"}


def test_plain_line_has_no_group():
    w = _worker()
    assert w._consume_groups("你們連貓咪的位子都要搶") == set()


# ---- 閘門 ----------------------------------------------------------------


def test_coffee_then_coffee_minutes_later_is_vetoed():
    """10:00 拿鐵 → 10:12 黑咖啡，十二分鐘，換品項不算換。"""
    w = _worker("小天後")
    _seed(w, [(12 * 60, _COFFEE)])
    assert "喝" in w._local_veto(_COFFEE2, [], "", GID)


def test_third_drink_in_two_hours_is_vetoed_even_if_not_adjacent():
    """11:25 冰美式：前兩則都在七十分鐘以前，貼身窗過了，密度窗要接得住。"""
    w = _worker("RICH")
    _seed(w, [(85 * 60, _COFFEE), (82 * 60, _DRINK), (73 * 60, _COFFEE2)])
    line = "剛喝完冰美式看到你說不是老婆這麼敢講，害我坐在位子上耳根都有點發燙"
    assert "喝" in w._local_veto(line, [], "", GID)


def test_single_old_drink_does_not_block():
    """只有一則兩小時以前的飲料，這則照發。"""
    w = _worker()
    _seed(w, [(150 * 60, _COFFEE)])
    line = "剛喝完冰美式看到你說不是老婆這麼敢講"
    assert "喝" not in w._local_veto(line, [], "", GID)


def test_two_food_lines_in_the_same_minute_are_blocked():
    """12:39 小小連兩則吃食（中午買的便當 → 我正啃雞排），秒差、同一個人、同一個群。"""
    w = _worker("小小")
    _seed(w, [(12 * 60, "後天嘴巴這麼挑，中午買的便當我也才吃兩口就不想吃了")])
    assert "喝" in w._local_veto("我正啃雞排嘴脣超油，你留著自己啃", [], "", GID)


def test_different_group_does_not_block():
    """上一則是喝咖啡，這則吃鹹酥雞，不同群放行。"""
    w = _worker()
    _seed(w, [(5 * 60, _COFFEE)])
    line = "逛完夜市手提著鹹酥雞，紙袋油到透出來"
    assert "喝" not in w._local_veto(line, [], "", GID)


def test_line_without_consumable_is_untouched():
    w = _worker()
    _seed(w, [(60, _COFFEE), (120, _COFFEE2)])
    assert "喝" not in w._local_veto("你們連貓咪的位子都要搶，這麼想被抱在懷裡摸嗎", [], "", GID)
