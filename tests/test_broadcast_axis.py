"""對象軸：有自曝不等於有對象。

2026-11-06 10:36–11:25 一小時四則，三則都是「我剛…」開頭（自曝免責成立），
主體卻還是對全場下判決：

  10:36 Chen「我剛把店裡新衣服掛好手超痠，妹妹不理你們這幾個弟弟，姊姊來疼你們要不要」
  10:57 小小「你們連貓咪的位子都要搶，這麼想被抱在懷裡摸嗎」
  11:04 小天後「只會在群裡亂舔有什麼用…我趁站櫃空檔偷滑手機都看膩了」
  11:25 RICH「剛喝完冰美式看到你說不是老婆這麼敢講，害我坐在位子上耳根都有點發燙」

四則裡三則整場都在當裁判，群裡十三個男人互相開火，沒有一則落在一個具體的人身上。
press box 只看「有沒有自己」，有自曝就放行，抓不到「有自己但沒有對象」。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001

_ANCHOR = "難得今天穿得這麼騷"

_BROADCASTS = (
    "我剛把店裡新衣服掛好手超痠，妹妹不理你們這幾個弟弟，姊姊來疼你們要不要🤣",
    "你們連貓咪的位子都要搶，這麼想被抱在懷裡摸嗎🤣",
    "只會在群裡亂舔有什麼用，真正實戰時舌頭沒力一樣被刷掉，我趁站櫃空檔偷滑手機都看膩了",
)
_TO_ONE = "剛喝完冰美式看到你說不是老婆這麼敢講，害我坐在位子上耳根都有點發燙了"


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


def test_plural_second_person_is_broadcast():
    w = _worker()
    for line in _BROADCASTS:
        assert w._is_broadcast(line), line


def test_line_to_one_man_is_not_broadcast():
    w = _worker()
    assert not w._is_broadcast(_TO_ONE)


def test_singular_you_inside_an_otherwise_plural_sentence_is_not_broadcast():
    """有一個「你」就是對著某個人講，不再算對群。"""
    w = _worker()
    assert not w._is_broadcast("你們都排後面，森森你先來")


def test_short_line_is_not_broadcast():
    w = _worker()
    assert not w._is_broadcast("真假")


def test_two_broadcasts_ask_for_one_target():
    w = _worker("小天後")
    _seed(w, [(49 * 60, _BROADCASTS[0]), (28 * 60, _BROADCASTS[1])])
    note = w._sentence_type_note([], GID, [])
    assert "單一男人" in note
    assert "「你」" in note


def test_one_broadcast_is_not_enough():
    w = _worker()
    _seed(w, [(600, _ANCHOR)])
    assert "單一男人" not in w._sentence_type_note([], GID, [])


def test_broadcast_window_expires():
    """半小時以外的舊播報不再算。"""
    w = _worker()
    _seed(w, [(80 * 60, _BROADCASTS[0]), (70 * 60, _BROADCASTS[1])])
    assert w._broadcast_hits(GID) == 0
