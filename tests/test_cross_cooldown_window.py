"""跨號冷卻窗改成看時鐘（2026-11-03 21:58 桃花源實測破口）。

破口：21:01:50 小天後「笑死，只會出一張嘴…」→ 21:58:30 小小「出一張嘴誰不會…」，
同一句慣用語隔 57 分鐘跨號重講。舊檢的跨號窗是「別人 4 則」，但這一群一小時
只有 12 則水軍發言，4 則＝20 分鐘前，冷卻還沒走完就被截掉，所以抓不到。

修法：跨號歷史改時間保留（100 分鐘）＋跨號慣用語窗 90 分鐘，則數只當保險絲。
以下用當晚真實 13 則回放，要求只退那一則、其餘 0 誤殺。
"""

import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001
NOW = time.time()

# 當晚真實水軍發言（時間, 帳號, 內容），末則就是該被退的破口
REAL = [
    ("21:01:50", "小天後", "笑死，只會出一張嘴真的很解，真見了面搞不好連牽手都結巴"),
    ("21:04:47", "Chen", "約我不用錢，看你本事讓我腿軟求饒"),
    ("21:06:08", "Chen", "辦好手續只是入場券，能不能讓姊姊盡興還得看實戰表現"),
    ("21:08:29", "Chen", "乳交夾緊也很舒服啊，沒試過的少囉嗦"),
    ("21:12:39", "小小", "小萬是想被治療了嗎，後庭癢就直說嘛"),
    ("21:16:17", "RICH", "一個個裝正常，私訊倒是挺誠實的嘛"),
    ("21:21:33", "小天後", "鱷魚皮夾克穿起來很帥啊，難怪捨不得放手"),
    ("21:24:49", "小小", "被鱷魚咬到可是會見血的，誰那麼大膽敢惹牠啊"),
    ("21:25:16", "小小", "這刺青滿背也太狂，手還放那邊是想引戰喔"),
    ("21:35:38", "Chen", "幫忙？先讓我看看你有幾兩重再說"),
    ("21:41:06", "Chen", "舔粉是什麼新招，弟弟你舌頭有這麼萬能嗎"),
    ("21:55:54", "RICH", "18公分不含頭？那實戰到底多長啊"),
    ("21:58:30", "小小", "出一張嘴誰不會，有種讓姊姊鑑定一下長短啊"),
]


def _worker(name: str = "小小") -> AccountWorker:
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
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def _minutes(clock: str) -> int:
    h, m, s = (int(x) for x in clock.split(":"))
    return h * 60 + m + s / 60


def _seed(w: AccountWorker, upto: int, base: float = 0.0) -> None:
    """把前 upto 則灌進跨號歷史，時間戳按真實間隔往前推（base＝最後一則的分鐘數）。"""
    w._recent_texts_by_group[GID] = [t for _, _, t in REAL[:upto]]
    w._recent_texts_at[GID] = [
        NOW - (base - _minutes(clock)) * 60 for clock, _, _ in REAL[:upto]
    ]


def test_current_breach_is_vetoed_across_57_minutes():
    w = _worker()
    _seed(w, len(REAL) - 1, base=_minutes(REAL[-1][0]))
    veto = w._local_veto(REAL[-1][2], [], "", GID)
    assert veto, "57 分鐘前的跨號慣用語沒抓到"
    assert "張嘴" in veto


def test_no_false_positive_on_the_same_real_hour():
    """同一批真實發言逐則回放，破口那則一定要退，其餘最多再多退播報過密的幾則。

    21:58 的破口靠慣用語抓；其餘多半是「連續好幾則都在講別人的事」被播報密度
    規則退掉——那一小時 13 則有 12 則確實是整排一句評論（鱷魚夾克、刺青、18 公分
    都是評人家的東西），屬於規則預期行為，不是誤殺。密度規則放寬到三則就判之後
    幾乎整排都該退，所以上限放到 10。"""
    fired = []
    for i, (_clock, name, text) in enumerate(REAL):
        w = _worker(name)
        _seed(w, i)
        own = [t for _, who, t in REAL[:i] if who == name][::-1][:3]
        if w._local_veto(text, own, "", GID):
            fired.append(i)
    assert len(REAL) - 1 in fired, f"破口那則漏抓：{fired}"
    assert len(fired) <= 10, f"播報密度殺太多：{fired}"
    assert 0 not in fired and 1 not in fired, f"開場兩則沒有前文卻被退：{fired}"


def test_history_is_kept_by_time_not_by_count():
    w = _worker()
    for i in range(30):
        w._record_sent_text(GID, f"第{i}則測句子")
        w._recent_texts_at[GID][-1] = NOW - (30 - i) * 60  # 每則間隔 1 分鐘
    assert len(w._recent_texts_by_group[GID]) == 30, "30 則全在 100 分鐘內卻被則數截掉"
    assert len(w._recent_texts_at[GID]) == 30


def test_stale_history_is_pruned():
    # 保留窗跟著最長的消費窗（場景軸 150 分鐘）放到 3 小時，超過才淘汰。
    w = _worker()
    w._record_sent_text(GID, "三小時前講的")
    w._recent_texts_at[GID][-1] = NOW - 200 * 60
    w._record_sent_text(GID, "剛講的")
    assert w._recent_texts_by_group[GID] == ["剛講的"]


def test_two_hour_old_history_is_kept_for_scene_axes():
    w = _worker()
    w._record_sent_text(GID, "兩小時前講的")
    w._recent_texts_at[GID][-1] = NOW - 120 * 60
    w._record_sent_text(GID, "剛講的")
    assert w._recent_texts_by_group[GID] == ["兩小時前講的", "剛講的"]


def test_cross_window_falls_back_without_timestamps():
    """單元測試直接灌清單（沒有時間戳）時，沿用整份清單，不能變空。"""
    w = _worker()
    w._recent_texts_by_group[GID] = ["甲句", "乙句"]
    assert w._cross_recent_texts(GID, 60) == ["甲句", "乙句"]


def test_frame_reuse_note_names_the_shared_frame():
    """Chen 21:04/21:06/21:35 三則都套「看＋評價」，逐詞比對抓不到、骨架抓得到。"""
    w = _worker("Chen")
    _seed(w, len(REAL) - 1, base=_minutes(REAL[-1][0]))
    own = [
        "乳交夾緊也很舒服啊，沒試過的少囉嗦",
        "辦好手續只是入場券，能不能讓姊姊盡興還得看實戰表現",
        "約我不用錢，看你本事讓我腿軟求饒",
    ]
    note = w._frame_reuse_note(own, GID)
    assert "看" in note and "骨架" in note


def test_frame_reuse_note_needs_three_lines():
    w = _worker("Chen")
    assert w._frame_reuse_note(["約我不用錢，看你本事"], GID) == ""


def test_mood_rotation_never_repeats_within_two():
    """心境曾經一小時全落在同一個挑戰式語域；輪替後近兩則抽過的一律排除。"""
    import random

    w = _worker("Chen")
    options = list(range(4))
    picks = []
    for _ in range(12):
        used = w._mood_history.setdefault("Chen", [])
        pool = [i for i in options if i not in used[-2:]]
        pick = random.choice(pool)
        used.append(pick)
        del used[:-3]
        picks.append(pick)
    for i in range(1, len(picks)):
        assert picks[i] != picks[i - 1]
        if i > 1:
            assert picks[i] != picks[i - 2]
