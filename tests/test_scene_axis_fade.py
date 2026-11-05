"""場景軸重播與拼裝句自檢（2026-11-05 15:32–16:08 桃花源實測破口）。

- 14:06 RICH「吹著冷氣伸懶腰內衣勒得有點悶」→ 16:01 小小「下午摸魚伸個懶腰內衣
  勒得好緊，出一張嘴就想上喔」：隔了 115 分鐘，一則句子把 14:06 的懶腰＋內衣、
  14:10 的摸魚、14:20 的「出一張嘴」重新拼起來。單軸 30 分鐘窗每個字都已經過期，
  逐字比對整句放行，群友看到的是同一格重播。
- 15:41 Chen「發情想亂咬就說…想咬姊姊哪裡直接講」→ 15:44 小天後「還想咬，弟弟你
  到底斷奶了沒」：相隔 2.5 分鐘同一個嘴上動作，身體部位清單抓不到「咬」。
- 15:37／15:41 兩則都是純評語，第三則 15:44 還是評語，播報密度門檻卡在 4 則樣本
  就永遠不觸發。

軸的壽命依共用軸數放大，全部由本群實際送出紀錄統計，不是禁用詞表。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001


def _worker(name: str = "小小") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 22}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    """pairs＝[(幾秒前, 句子)]，由舊到新。"""
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def _veto(w: AccountWorker, candidate: str) -> str:
    return w._local_veto(candidate, [], "", GID)


def test_two_shared_axes_survive_the_short_window():
    """16:01 小小：懶腰＋內衣都來自 115 分鐘前的同一則，兩軸一起借。"""
    w = _worker()
    _seed(
        w,
        [
            (115 * 60, "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好"),
            (111 * 60, "咬著手搖吸管摸魚，小腿站到發痠，出門不用付錢是拿腰力抵飯錢嗎"),
            (300, "喵喵喵"),
        ],
    )
    veto = _veto(w, "下午摸魚伸個懶腰內衣勒得好緊，出一張嘴就想上喔")
    assert veto
    assert "懶腰" in veto and "內衣" in veto


def test_mosaic_from_three_old_sentences_is_vetoed():
    """三則舊句各借一軸，拼起來還是同一格。"""
    w = _worker()
    _seed(
        w,
        [
            (140 * 60, "剛拉下鐵門換掉包臀裙，倒了杯紅酒"),
            (130 * 60, "高跟鞋踢在門口，腳底板熱到發燙"),
            (120 * 60, "噴了香水自己聞著都心跳好快"),
        ],
    )
    veto = _veto(w, "鐵門拉下來高跟鞋還沒脫，香水味都散了")
    assert veto
    assert "拼過" in veto


def test_single_axis_inside_the_short_window_still_vetoes():
    """單軸一小時內算撞，舊行為不能因為改寫而放寬。"""
    w = _worker("RICH")
    _seed(w, [(26 * 60, "害我剛在位子伸個懶腰差點把扣子繃開"), (60, "你朋友對你這麼好")])
    veto = _veto(w, "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好")
    assert veto
    assert "懶腰" in veto


def test_single_axis_after_the_short_window_is_released():
    """一軸超過一小時就放行，不是永久封鎖。"""
    w = _worker()
    _seed(w, [(75 * 60, "下午吹冷氣腳痠想睡，真脫了衣服不知道能不能幹到我爽"), (120, "真假")])
    assert _veto(w, "洗完澡擦完身體乳，只想躺平") == ""


def test_mouth_gesture_repeat_two_minutes_apart():
    """15:41→15:44 同一個「咬」，身體部位清單抓不到。"""
    w = _worker("小天後")
    _seed(
        w,
        [
            (150, "發情想亂咬就說，少拿狂犬病當藉口，想咬姊姊哪裡直接講"),
            (30, "肯定是被狗咬傷了"),
        ],
    )
    veto = _veto(w, "人家都澄清不是她了還想咬，弟弟你到底斷奶了沒啊")
    assert veto
    assert "咬" in veto


def test_press_box_density_fires_on_three_samples():
    """只有三則水軍樣本、其中兩則純評語，第三則不准再評。"""
    w = _worker()
    _seed(
        w,
        [
            (7 * 60, "要舔也是往上舔敏感帶，舔腳到底什麼怪癖，有夠噁心"),
            (3 * 60, "發情想亂咬就說，少拿狂犬病當藉口，想咬姊姊哪裡直接講"),
            (40, "狂犬病發作了"),
        ],
    )
    veto = _veto(w, "人家都澄清不是她了還想咬，弟弟你到底斷奶了沒啊")
    assert veto
    assert "講別人的事" in veto or "咬" in veto


def test_tired_state_repeated_within_the_hour_is_vetoed():
    """一整個下午都在講累，第三則不能再拿累當開場。"""
    w = _worker("小天後")
    _seed(w, [(18 * 60, "當人太累，騎車吹到頭髮超塌只想躺著廢"), (60, "真假")])
    veto = _veto(w, "下班累到不想講話，飯都懶得吃")
    assert veto
    assert "累" in veto
