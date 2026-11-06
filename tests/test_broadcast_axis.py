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


# ---- 13:22~13:37 第二輪實測：提示不落地 --------------------------------

_BREAK_LINE = "臉不像沒關係，把褲子脫下來看看雞雞跟腹肌像不像彭于晏比較實際"


def test_ruling_on_a_third_man_is_still_broadcast():
    """13:37 小小「把褲子脫下來看看…比較實際」：在講華發吹牛，但整句沒有「你」。"""
    w = _worker("小小")
    assert w._is_broadcast(_BREAK_LINE)
    assert w._is_broadcast("舔狗被弄哭也是很可憐欸，誰去安慰一下🤣")


def test_two_broadcasts_force_a_target_retry():
    w = _worker("小小")
    _seed(w, [(47 * 60, _BROADCASTS[1]), (14 * 60, "舔狗被弄哭也是很可憐欸，誰去安慰一下🤣")])
    assert w._needs_target_retry(GID, _BREAK_LINE)
    assert not w._needs_target_retry(GID, _TO_ONE)


def test_target_retry_needs_pressure():
    w = _worker()
    _seed(w, [(600, _ANCHOR)])
    assert not w._needs_target_retry(GID, _BREAK_LINE)


# ---- 重寫鏈：換策略還端回判決就再換一條 ---------------------------------

def test_broadcast_candidate_is_dropped_when_a_target_is_required():
    import asyncio

    w = _worker("小小")
    bodies = iter([_BREAK_LINE, "你這句講得我腿都軟了，剛坐著寫東西腰超痠"])
    seen: list[str] = []

    async def generate(_hint: str) -> str:
        body = next(bodies)
        seen.append(body)
        return body

    async def review(_ctx, cand, *_a, **_k):
        return {"sendable": 0.9, "issue": "none"}

    w._review_candidate = review  # type: ignore[assignment]
    out = asyncio.run(
        w._pick_passing_candidate(
            "ctx", "", "hint", generate, group_id=GID, limit=2, require_target=True
        )
    )
    assert out == "你這句講得我腿都軟了，剛坐著寫東西腰超痠"
    assert w.stats["broadcast_rewrite_rejected"] == 1
    assert len(seen) == 2


# ---- 裁判帶著這條判 -----------------------------------------------------

class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        msg = SimpleNamespace(content='{"sendable":0.9,"issue":"none","reason":""}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_judge_is_told_to_demand_a_single_target():
    import asyncio

    w = _worker()
    client = _FakeClient()
    w.ai_client = cast(Any, client)
    asyncio.run(w._review_candidate_gateway("ctx", _BREAK_LINE, require_target=True))
    body = client.calls[0]["messages"][1]["content"]
    assert "硬要求" in body and "單數的「你」" in body


def test_judge_has_no_target_requirement_by_default():
    import asyncio

    w = _worker()
    client = _FakeClient()
    w.ai_client = cast(Any, client)
    asyncio.run(w._review_candidate_gateway("ctx", _BREAK_LINE))
    assert "單數的「你」" not in client.calls[0]["messages"][1]["content"]


# ---- 複讀比對不吃襯字 ---------------------------------------------------

def test_verbatim_run_ignores_filler_chars():
    """13:04:35 真人「簡單吃點啦」→ 13:05:18 Chen「簡單吃一點」，只差一個「一」。"""
    assert AccountWorker._verbatim_run("剛在店裡整理新衣服，簡單吃一點就被嫌很一般", "簡單吃點啦", 4)


# ---- 主動發言路徑（這一群幾乎全靠主動） ---------------------------------

def _stub_pick(w: AccountWorker, result: str, calls: list[Any]) -> None:
    async def fake(*_a, **_k):
        calls.append(1)
        return result

    w._pick_passing_candidate = fake  # type: ignore[assignment]


def test_proactive_broadcast_is_rewritten_even_with_self_disclosure():
    """13:30 RICH「請客就想白幹嗎，體力能讓我腿軟再說誰買單吧」：有自曝、但沒有對象。"""
    import asyncio

    w = _worker("RICH")
    _seed(w, [
        (27 * 60, "剛坐在櫃檯腳背點到麻了，你們這幾個是要吵到什麼時候"),
        (3 * 60, "請客就想白幹嗎，體力能讓我腿軟再說誰買單吧"),
    ])
    calls: list[Any] = []
    _stub_pick(w, "你這句講得我腿都軟了", calls)
    out = asyncio.run(w._proactive_echo_rewrite(GID, _BREAK_LINE, "", [], ""))
    assert out == "你這句講得我腿都軟了"
    assert calls


def test_proactive_targeted_line_is_not_rewritten():
    import asyncio

    w = _worker("RICH")
    _seed(w, [
        (27 * 60, "剛坐在櫃檯腳背點到麻了，你們這幾個是要吵到什麼時候"),
        (3 * 60, "請客就想白幹嗎，體力能讓我腿軟再說誰買單吧"),
    ])
    calls: list[Any] = []
    _stub_pick(w, "不該被用到", calls)
    assert asyncio.run(w._proactive_echo_rewrite(GID, _TO_ONE, "", [], "")) == ""
    assert not calls
