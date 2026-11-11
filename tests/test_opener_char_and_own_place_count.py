"""11-11 早上破口：起手字隔幾則再撞、自己場所換字連發。

實測（10:00–11:00 桃花源，200 則裡水軍 16 則）：

- 起手字「剛」三連發：RICH 10:03「剛泡完澡好熱」→ 小天後 10:11「…我也剛到
  櫃上摸魚」→ RICH 10:26「剛揉眼醒來」。中間隔著八分與十五分，前兩次還落在
  十二分鐘窗外的第三次，舊的「比上一則」「十二分鐘兩次」兩個條件一個都沒中。
- 小天後十點半內四則有三則在報店裡：10:11 櫃上、10:20 櫃位、10:21 站櫃。
  自撞軸只比場所字串，三個字串全不同，等於全放行。
- 兩件事都是「換字不換格」，所以修法量次數，不寫死「剛」或「站櫃」。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker, _COMMON_HEAD_CHARS

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
    w.persona = {"name": name, "gender": "女", "age": 38}
    return w


def _seed(w: AccountWorker, own=(), others=()) -> None:
    now = time.time()
    w._own_scene_by_group[GID] = [(t, now - age) for age, t in own]
    w._recent_texts_by_group[GID] = [t for _a, t in others]
    w._recent_texts_at[GID] = [now - a for a, _t in others]


def test_gan_is_not_in_the_common_opener_set():
    """「剛」要留在軸上，否則寬窗一進來就被跳過。"""
    assert "剛" in "剛泡完澡好熱"[0]
    assert "剛" not in _COMMON_HEAD_CHARS


def test_dao_qv_shang_counts_as_place():
    """10:11 實句「剛到櫃上摸魚」：前導字是「到」不是「在」，原本判不出場所。"""
    from app.worker import _place_opening

    assert _place_opening("吃飽有力氣撐過今天，我也剛到櫃上摸魚") == "櫃上"


def test_own_place_switch_wording_still_counts():
    """櫃上／櫃位／站櫃是三個字串，同一件事：自己又在店裡。"""
    w = _worker()
    _seed(
        w,
        own=[
            (1320, "吃飽有力氣撐過今天，我也剛到櫃上摸魚"),
            (90, "靠在櫃位偷喝手搖，看你們聊生病我腿心都發燙了"),
        ],
    )
    veto = w._local_veto("站櫃腳超酸", [], "", GID)
    assert "已經報過" in veto and "站櫃" in veto
    assert "換成「" in veto


def test_own_place_count_does_not_leak_across_accounts():
    """別人的場所不算自己的自撞。"""
    w = _worker()
    _seed(w, own=[(1320, "在店裡拆包裹")], others=[(90, "靠在櫃位偷喝手搖")])
    assert w._own_place_hits(GID, 7200) == 1


def test_opener_char_repeats_across_several_messages():
    """10:03 剛泡 → 10:26 剛揉：中間隔二十分鐘也算撞，換一個字起頭。"""
    w = _worker("RICH")
    _seed(w, others=[(1380, "剛泡完澡好熱")])
    veto = w._local_veto("剛揉眼醒來", [], "", GID)
    assert "「剛」" in veto


def test_wide_opener_rule_ignores_common_chars():
    """很普通的起手字（我／這）往前看三則時不當依據，免得每則都被攔。"""
    w = _worker()
    _seed(w, others=[(1380, "我們這群真的沒一個正經的")])
    assert w._local_veto("我今天不想上班", [], "", GID) == ""


def test_wide_opener_window_expires():
    """超過二十五分鐘就該放行，不能一路鎖死。"""
    w = _worker("RICH")
    _seed(w, others=[(1800, "剛泡完澡好熱")])
    assert w._local_veto("剛揉眼醒來", [], "", GID) == ""
