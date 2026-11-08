"""兩段式節奏軸＋短碎窄帶（2026-11-08 10:00–10:17 桃花源實測）。

破口：開群後九則水軍全是同一個節奏——先報備自己在幹嘛、逗號後面才對人撩或嗆
（「剛點好香氛蠟燭大腿還微酸，…」「冷氣好冷，要肉身貼緊保護我嗎」）。既有的
無對象軸、播報軸、引述軸逐條放行，因為每一軸單看都合格：有自曝、有指名、
有對象、不是問句。病灶在**順序**，只有整段看節奏才抓得到。

字數同樣沒散開：九則 11~26 字、0 則 ≤8 字，模型永遠寫到字數帶上緣。
"""
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
NOW = time.time()
NAMES = ["和進出口", "後天", "風口浪尖", "華發", "萬英尺", "戰豆"]

# 當天九則真實水軍發言（時間, 帳號, 內容）
REAL = [
    ("10:00:21", "Chen", "少在那邊出一張嘴，有本事當面來驗貨啊"),
    ("10:02:32", "RICH", "上班摸魚還要聽廢話真煩"),
    ("10:05:31", "小天後", "上班偷喝冰美式才醒得來"),
    ("10:08:23", "RICH", "後天，煮雞蛋麵不如煮你自己給姊姊吃"),
    ("10:12:03", "Chen", "風口浪尖，弟弟被你戳到敏感帶了"),
    ("10:12:54", "RICH", "我都邊喝咖啡邊看，你真的會怕喔"),
    ("10:13:29", "RICH", "冷氣好冷，要肉身貼緊保護我嗎"),
    ("10:15:36", "小天後", "光看顏色哪夠，敢不敢進來試試看我有多緊"),
    ("10:17:41", "Chen", "華發少自作多情，我正對鏡子試高跟鞋看你急著往臉上貼金"),
]

# 前段是報備生活、後段才落到人 → 兩段式模子
PREAMBLE = [
    "剛點好香氛蠟燭大腿還微酸，和進出口他一直纏著人家當然會被嫌煩",
    "脫掉內衣揉著胸看你一直吵能不煩嘛",
    "擦著乳液看地暖鋪著自己打手槍，留給姊姊用小穴榨不是更爽嗎",
    "我都邊喝咖啡邊看，你真的會怕喔",
    "冷氣好冷，要肉身貼緊保護我嗎",
    # 名字前置也是同一個骨架，只是鋪的那截換成人名（14:23 兩號連發同一組）
    "和進出口，煮給我吃再床上見真章",
    "和進出口，不穿日料改把我剝光在流理台吃個夠",
]

# 單句、或前段就已經在對人開嗆 → 不算
NOT_PREAMBLE = [
    "上班偷喝冰美式才醒得來",
    "少在那邊出一張嘴，有本事當面來驗貨啊",
    "真的假的",
    "我會笑死",
]


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
    w._speaker_names_by_group[GID] = list(NAMES)
    return w


def test_preamble_beat_detects_real_preambles():
    w = _worker()
    for text in PREAMBLE:
        assert w._is_preamble_beat(text, GID), text


def test_preamble_beat_passes_single_clause_and_open_strike():
    w = _worker()
    for text in NOT_PREAMBLE:
        assert not w._is_preamble_beat(text, GID), text


def test_note_fires_when_last_three_are_preamble_beats():
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "擦著乳液看地暖鋪著自己打手槍，留給姊姊用小穴榨不是更爽嗎",
        "冷氣好冷，要肉身貼緊保護我嗎",
        "我都邊喝咖啡邊看，你真的會怕喔",
    ]
    w._recent_texts_at[GID] = [NOW - 120, NOW - 60, NOW - 10]
    note = w._sentence_type_note([], GID, None)
    assert "先講自己在幹嘛" in note


def test_note_quiet_when_rhythm_is_mixed():
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "上班偷喝冰美式才醒得來",
        "少在那邊出一張嘴，有本事當面來驗貨啊",
        "真的假的",
    ]
    w._recent_texts_at[GID] = [NOW - 120, NOW - 60, NOW - 10]
    note = w._sentence_type_note([], GID, None)
    assert "先講自己在幹嘛" not in note


def test_short_tight_cap_is_ten_chars():
    w = _worker()
    w._struct_mode = "short"
    w._short_tight = True
    assert w._struct_char_cap(90) == 10
    w._short_tight = False
    assert w._struct_char_cap(90) == 20


def test_strip_keeps_only_the_tail_half():
    w = _worker()
    assert (
        w._strip_preamble_head("冷氣好冷，要肉身貼緊保護我嗎", GID)
        == "要肉身貼緊保護我嗎"
    )
    # 非兩段式不動刀
    assert w._strip_preamble_head("上班偷喝冰美式才醒得來", GID) == ""
    assert w._strip_preamble_head("少在那邊出一張嘴，有本事當面來驗貨啊", GID) == ""


def test_flow_counts_only_preamble_beats():
    w = _worker()
    w._recent_texts_by_group[GID] = [
        "冷氣好冷，要肉身貼緊保護我嗎",
        "我都邊喝咖啡邊看，你真的會怕喔",
        "真的假的",
    ]
    w._recent_texts_at[GID] = [NOW - 120, NOW - 60, NOW - 10]
    assert w._preamble_flow(GID) == 2


def test_name_first_head_keeps_the_name_when_cut():
    """名字在前：剪掉的是名字後面那截動作，不是名字本身。"""
    w = _worker()
    assert (
        w._strip_preamble_head("後天，煮雞蛋麵還邊滑手機不如煮你自己給姊姊吃", GID)
        == "後天，煮雞蛋麵還邊滑手機不如煮你自己給姊姊吃"
    )
    assert w._strip_preamble_head("和進出口，煮給我吃再床上見真章", GID) == (
        "和進出口，煮給我吃再床上見真章"
    )


def test_flow_needs_three_of_last_five():
    """詞換光也照算，但單則不動刀：五則裡要三則才是語流。"""
    w = _worker()
    beats = [
        "換新內衣勒得好緊，怕壓壞娃娃不如改把哥哥按在床上搖",
        "剛試穿新裙子拉鍊卡住，不裝了誰幫我剝掉它",
        "練過深蹲夾得更緊，今晚敢不敢來試我的姿勢",
    ]
    w._recent_texts_at[GID] = [NOW - 240, NOW - 180, NOW - 60, NOW - 30, NOW - 10]
    w._recent_texts_by_group[GID] = beats + ["真的假的", "我會笑死"]
    assert w._preamble_flow(GID) == 3
    w._recent_texts_by_group[GID] = beats[:2] + ["真的假的", "我會笑死", "上班偷喝冰美式才醒得來"]
    assert w._preamble_flow(GID) == 2
