"""場所自撞軸（2026-11-07 14:39–14:57 桃花源實測破口）。

- Chen 六則水軍裡五則開在店裡（14:03 坐櫃檯吹指甲油、14:28 在店裡燙蕾絲短裙、
  14:45 在店裡拆包裹、14:57 在店裡整理新衣服），兩則還是同一句構
  「害我在店裡＋動作＋笑到」。小天後三則全站櫃。
- 場地軸原本只看得到跨號，且 14:39「下午站櫃聽到這句」連判都判不出來（站櫃不在詞表）。
- 更關鍵：14:45 那則其實有被自檢攔下，但三個策略改回來還是店裡，退路照樣出貨，
  等於攔了沒改。所以退路本身要挑場景、重寫要拿到具體替代場景。

全部用實際語料當測資，不寫死「店裡」以外的規則。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker, _place_opening

GID = -1001


def _worker(name: str = "Chen") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 38}
    return w


def _seed(w: AccountWorker, own=(), others=()) -> None:
    """own／others＝[(幾秒前, 句子)]。"""
    now = time.time()
    w._own_scene_by_group[GID] = [(t, now - age) for age, t in own]
    w._recent_texts_by_group[GID] = [t for _a, t in others]
    w._recent_texts_at[GID] = [now - a for a, _t in others]


CHEN_IN_STORE = "風口浪尖連人家名字都打成後庭，害我在店裡拆新包裹笑到手抖"
CHEN_AGAIN = "萬英尺，拿去墊桌腳也太奢侈，害我在店裡整理新衣服看到差點笑出來"
TIAN_ZHAN_GUI = "和進出口還敢問哪裡噁心，下午站櫃聽到這句白眼都翻過去"


def test_zhan_gui_is_a_place():
    """14:39 小天後那則原本整條軸回空字串，等於沒人管。"""
    assert _place_opening(TIAN_ZHAN_GUI) == "站櫃"


def test_own_place_repeat_is_vetoed():
    """自己兩小時內在店裡講過，第二則同一個地方就要退。"""
    w = _worker()
    _seed(w, own=[(700, CHEN_IN_STORE)])
    veto = w._local_veto(CHEN_AGAIN, [], "", GID)
    assert "自己兩小時內已經報過" in veto
    assert "店裡" in veto


def test_own_place_veto_carries_a_concrete_escape_scene():
    """只叫櫃姐「別再報自己在哪」它還是寫店裡，退路理由要給得出別的场景。"""
    w = _worker()
    _seed(w, own=[(700, CHEN_IN_STORE)])
    veto = w._local_veto(CHEN_AGAIN, [], "", GID)
    assert "換成「" in veto
    assert "店裡" not in veto.split("換成「")[1].split("」")[0]


def test_fresh_place_passes():
    """自己沒講過的場所照樣放行（瑜珈不該被店裡連坐）。"""
    w = _worker()
    _seed(w, own=[(700, CHEN_IN_STORE)])
    cand = "萬英尺，收工在樓下空地拉了十分鐘筋，你那種吹法我膝蓋都替你痠"
    assert w._local_veto(cand, [], "", GID) == ""


def test_cross_account_place_still_vetoed_and_gets_target():
    """跨號撞場所的舊行為不能掉，理由同樣要帶替代場景。"""
    w = _worker()
    _seed(w, own=[], others=[(400, "下午在店裡燙新進的蕾絲短裙，等下讓你伺候到腿軟")])
    veto = w._local_veto(CHEN_AGAIN, [], "", GID)
    assert "場地開頭" in veto
    assert "換成「" in veto


def test_carry_hold_skips_the_repeated_place():
    """退路排序：被攔的候選若還是店裡，讓有名字但沒撞場所的那條優先。"""
    import asyncio

    w = _worker()
    _seed(w, own=[(700, CHEN_IN_STORE)])

    seq = [
        "萬英尺，拿去墊桌腳也太奢侈，害我在店裡整理新衣服看到差點笑出來",
        "萬英尺，拿去墊桌腳也太奢侈，厲害到害我從椅子上坐直",
    ]

    async def gen(_hint: str) -> str:
        return seq.pop(0) if seq else ""

    async def review(*_a, **_k):
        return {"sendable": 0.5, "issue": "none"}

    w._review_candidate = review  # type: ignore[assignment]
    picked = asyncio.run(
        w._pick_passing_candidate(
            "ctx", "", "hint", gen, own_texts=[], group_id=GID, limit=2,
            hot_names=("萬英尺",),
        )
    )
    assert "店裡" not in picked
    assert "萬英尺" in picked

