"""19:06–19:34 實測破口：回覆全被插話取消、部位連撞、連三則純評語漏攔。

- 19:06–19:34 一小時 183 則真人：claimed 13、sent 0、cancel 13（24h 審計 claimed 61、
  cancel 34、sent 13），因為取消預算是固定秒數，句子生成好也照砍。
- 19:00 小天後「內衣濕貼在胸口」→ 19:06 RICH「緊身裙胸口被勒出紅印」，六分鐘同部位。
- 19:13／19:19／19:32 三則純評語：19:32「怕自己硬不起來」的「自己」是那個男人，
  被判成自曝放行。

全部動態：看本群實際送出紀錄與實測延遲，不寫死秒數。
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
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def test_same_body_part_within_ten_minutes_is_vetoed():
    w = _worker("RICH")
    _seed(w, [(6 * 60, "躲更衣室內衣濕貼在胸口，快來幫我舔乾淨")])
    veto = w._local_veto("脫掉緊身裙胸口被勒出紅印超敏感，吵架好煩", [], "", GID)
    assert "胸口" in veto


def test_other_body_part_is_allowed():
    w = _worker()
    _seed(w, [(6 * 60, "躲更衣室內衣濕貼在胸口，快來幫我舔乾淨")])
    assert w._local_veto("黑絲長腿一伸，眼睛都不知道往哪擺", [], "", GID) == ""


def test_body_part_window_expires():
    w = _worker()
    _seed(w, [(15 * 60, "內衣濕貼在胸口")])
    assert w._local_veto("低頭綁鞋帶頭暈了一下", [], "", GID) == ""


def test_reflexive_pronoun_about_the_man_is_press_box():
    # 「自己」指的是那個男人，不是她的身體 → 仍算播報
    assert AccountWorker._is_press_box(
        "下春藥是怕自己硬不起來嗎，真男人就直接用大雞雞把人弄到高潮啊"
    )


def test_reflexive_pronoun_with_own_action_is_self_disclosure():
    assert not AccountWorker._is_press_box("剛自己脫到剩內衣，冷氣一吹皮膚都縮起來")


def test_consecutive_press_box_counts_three():
    w = _worker()
    _seed(
        w,
        [
            (15 * 60, "男人嘴巴說討厭，看到黑絲長腿還不是兩隻手直接貼上來揉"),
            (13 * 60, "哪來的自信覺得會有人求你，邊敷面膜看你破防"),
            (1 * 60, "下春藥是怕自己硬不起來嗎，真男人就直接用大雞雞把人弄到高潮啊"),
        ],
    )
    assert w._consecutive_press_box(GID) == 3


def test_interrupt_budget_grows_with_measured_generation_time():
    w = _worker()
    now = time.time()
    humans = [now - 3, now - 20, now - 40]  # 三則／分 → 靜態預算 49 秒
    before = w._adaptive_interrupt_budget(humans, now, GID)
    assert before == 49.0
    for sec in (60, 70, 80):
        w._note_reply_latency(GID, sec)
    after = w._adaptive_interrupt_budget(humans, now, GID)
    assert after > before
    assert after <= 120.0


def test_interrupt_budget_capped_even_for_slow_model():
    w = _worker()
    now = time.time()
    for sec in (200, 200, 200):
        w._note_reply_latency(GID, sec)
    assert w._adaptive_interrupt_budget([], now, GID) == 120.0


def test_latency_samples_are_group_isolated():
    w = _worker()
    now = time.time()
    for sec in (10, 10, 10):
        w._note_reply_latency(GID, sec)
    assert w._adaptive_interrupt_budget([], now, -2002) == 25.0


def test_veto_code_classifies_reason():
    assert AccountWorker._veto_code("「胸口」這個部位十分鐘內才有人寫過") == "body_part"
    assert AccountWorker._veto_code("「懶腰」自己四小時內做過了") == "own_scene"
    assert AccountWorker._veto_code("「懶腰」這動作最近才有人做過") == "scene_dup"
    assert AccountWorker._veto_code("起手詞「剛」自己已經用過 2 次") == "opening"
    assert AccountWorker._veto_code("亂七八糟") == "other"


def test_seconds_since_any_bot_without_history_is_large():
    import asyncio

    w = _worker()

    async def _spoke(_gid):
        return {}

    w.db = SimpleNamespace(group_bot_last_spoke=_spoke)
    assert asyncio.run(w._seconds_since_any_bot(GID)) > 600


def test_seconds_since_any_bot_reads_latest_speaker():
    import asyncio

    w = _worker()

    async def _spoke(_gid):
        return {1: time.time() - 30, 2: time.time() - 120}

    w.db = SimpleNamespace(group_bot_last_spoke=_spoke)
    assert 25 < asyncio.run(w._seconds_since_any_bot(GID)) < 35
