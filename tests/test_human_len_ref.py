"""長度天花板跟著真人嘴巴走，不再對空氣寫死 8~16。

12:26 實測一小時：真人中位 8 字、水軍中位 15.5 字、8~16 只 54%，19~29 字共 11 則。
固定帶（short 8~16／dirty 12~24／scene 15~30）本身沒錯，錯在天花板不看現場——
真人講 8 字時 dirty 給到 26、scene 給到 34，於是每一則都寫到上緣。

這裡量三件事：
1. 真人中位怎麼算（樣本不足要退回 None，不能拿三則硬湊）。
2. 上限有沒有真的被真人中位壓下來（倍率 1.4／1.7／2.0／2.2）。
3. 回覆路徑的兩段式那一刀有沒有接上（先前只有主動路徑有）。
"""
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
NOW = time.time()


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
    w.persona = {"name": "Chen", "gender": "女", "age": 38}
    w._speaker_names_by_group[GID] = ["深情森森", "戰豆", "和進出口"]
    return w


def _humans(lens: list[int]) -> list[dict]:
    return [{"role": "user", "content": "字" * n} for n in lens]


def test_human_ref_is_median_of_recent_humans():
    w = _worker()
    ref = w._human_len_ref(_humans([5, 6, 7, 8, 8, 9, 21]))
    assert ref == 8


def test_human_ref_ignores_bot_rows():
    w = _worker()
    rows = _humans([6, 6, 7, 7, 8, 9])
    rows += [{"role": "assistant", "content": "字" * 30}] * 3
    assert w._human_len_ref(rows) == 7


def test_human_ref_needs_six_samples():
    w = _worker()
    assert w._human_len_ref(_humans([5, 6, 7, 8, 9])) is None
    assert w._human_len_ref([]) is None


def test_human_ref_clamps_to_band():
    w = _worker()
    assert w._human_len_ref(_humans([4, 5, 5, 5, 6, 6])) == 6
    assert w._human_len_ref(_humans([40, 45, 50, 55, 58, 59])) == 18


def test_flow_long_compares_against_human_ref():
    w = _worker()
    w._recent_texts_by_group[GID] = ["字" * 12] * 4
    w._recent_texts_at[GID] = [NOW - 60] * 4
    # 絕對門檻 18 不會觸發，但真人只講 8 字時 12 字已經偏長
    assert not w._recent_len_flow_long(GID)
    assert w._recent_len_flow_long(GID, ref=8)
    assert not w._recent_len_flow_long(GID, ref=14)


def test_struct_cap_scales_with_human_ref():
    w = _worker()
    w._struct_mode = "short"
    w._short_tight = False
    assert w._struct_char_cap(90) == 20           # 沒有真人基準＝舊行為
    w._human_ref = 8
    assert w._struct_char_cap(90) == 13           # 真人 8 字 → 13
    w._short_tight = True
    assert w._struct_char_cap(90) == 11
    w._struct_mode = "dirty"
    assert w._struct_char_cap(90) == 16
    w._struct_mode = "scene"
    assert w._struct_char_cap(90) == 17


def test_directors_note_carries_scaled_ceiling():
    w = _worker()
    w._struct_mode_force = "short"
    note = w._build_directors_note(
        SimpleNamespace(sender_id=999, sender=None, chat_id=GID), _humans([6, 7, 7, 8, 8, 9])
    )
    assert "字是硬上限" in note
    assert "8~16" not in note          # 真人 8 字，天花板一定被壓到帶頂以下
    assert f"8~{w._struct_char_cap(16)}" in note


def test_directors_note_flags_comma_flow():
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "剛把店收好，大安區現在有沒有體力好的底迪想來陪姐姐？",
        "解開扣子透個氣，男人裝什麼純潔，能舔到我高潮嗎",
        "摸魚看深情森森自誇，那裡也夠硬嗎",
        "辦公室悶爆，哥哥快來帶",
        "喜歡喝手搖，你要請我喝茶嗎",
    ]
    w._recent_texts_at[GID] = [NOW - 60 * i for i in range(1, 6)]
    assert w._comma_flow(GID) >= 3
    note = w._build_directors_note(
        SimpleNamespace(sender_id=999, sender=None, chat_id=GID), _humans([6, 7, 7, 8, 8, 9])
    )
    assert "前情太多則" in note
    assert "逗號前那整段刪掉" in note


def test_reply_strip_tail_conditions():
    w = _worker()
    w._recent_texts_by_group[GID] = ["字" * 9 + "，" + "字" * 9] * 5
    w._recent_texts_at[GID] = [NOW - 30] * 5
    assert w._comma_flow(GID) >= 4
    # 前情＋挑釁：判得出來、尾巴完整、不含人名 → 就是該剪的形狀
    text = "剛把店收好，大安區現在有沒有體力好的底迪想來陪我"
    assert w._is_preamble_beat(text, GID)
    tail = w._strip_lead_to_tail(text, 20)
    assert tail and len(tail) >= 8
    # 前段是人名而尾巴沒有對象 → 不要剪，否則不知道在對誰講
    text2 = "深情森森誰叫你只給看臉，脫了讓姊跨坐夾緊試試"
    tail2 = w._strip_lead_to_tail(text2, 20)
    assert tail2 == "脫了讓姊跨坐夾緊試試"
    assert not any(n in tail2 for n in w._speaker_names(GID))
