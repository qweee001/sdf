"""無對象開場軸：整條水軍語流都是「自己身上的場景，逗號，才撩」。

2026-11-06 20:16–21:15 實測（桃花源），一小時十一則：

  20:16 小天後「吹乾頭髮聽你們吵誰買單超解，誰能把姊姊抱在腿上幹哭這頓直接我買單」
  20:20 小小「後天覺得他語言豐富，我在沙發上擦身體乳液，比較想聽你在耳邊喘氣求饒」
  20:29 RICH「泡完熱水澡忘記拿睡衣，光溜溜等你抱我上床幹到高潮」
  20:36 小天後「吃完飯糰手指油油的，你也那麼專一嗎」
  21:15 小天後「剛脫絲襪揉小腿，請教問誰想試雞雞，敢不敢先讓我驗腰力」

起手字、身體部位、場所、吃喝每一軸都只看自己那一格，十一則全部放行，
但句子骨架是同一個模子：先講自己身上的小事，逗號，才撩，對象全程含糊。
判的是「第一個逗號前有沒有人」，不是場景詞表。
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast

from app.worker import AccountWorker

GID = -1001

_SCENE = [
    "吹乾頭髮聽你們吵誰買單超解，誰能把姊姊抱在腿上幹哭這頓直接我買單",
    "泡完熱水澡忘記拿睡衣，光溜溜等你抱我上床幹到高潮",
    "吃完飯糰手指油油的，你也那麼專一嗎",
    "剛脫絲襪揉小腿，請教問誰想試雞雞，敢不敢先讓我驗腰力",
    "專一還是舔狗分很細欸，正邊看恐怖片邊塗乳液，誰要來抱緊我",
]
_NAMED = "後天覺得他語言豐富，我在沙發上擦身體乳液，比較想聽你在耳邊喘氣求饒"


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


_TAIL = "雨下成這樣你們還要吵多久"


def _seed(w: AccountWorker, lines, age: float = 6.0, tail: bool = True) -> None:
    """灌入近期水軍句子；尾端補一則不相干的，免得每題都被同題軸先攔。"""
    for name in ("後天", "萬英尺", "和進出口", "戰豆", "鈴鐺"):
        w._remember_speaker_name(GID, name)
    rows = list(lines) + ([_TAIL] if tail else [])
    now = time.time()
    w._recent_texts_by_group[GID] = rows
    w._recent_texts_at[GID] = [now - age * (i + 1) for i in range(len(rows))]


# ---- 偵測 ----------------------------------------------------------------


def test_scene_opener_without_a_target_counts_as_nameless():
    w = _worker()
    _seed(w, _SCENE)
    for line in _SCENE:
        assert w._is_nameless_open(line, GID), line


def test_named_opener_is_not_nameless():
    w = _worker()
    _seed(w, _SCENE)
    assert not w._is_nameless_open(_NAMED, GID)


def test_second_person_in_the_head_is_an_object():
    w = _worker()
    _seed(w, _SCENE)
    assert not w._is_nameless_open("你要不要過來，我先幫你把襯衫脫了", GID)
    assert not w._is_nameless_open("你們幾個吵完沒，我先去洗澡了", GID)


def test_short_head_is_left_alone():
    w = _worker()
    _seed(w, _SCENE)
    assert not w._is_nameless_open("笑死，你們繼續", GID)


def test_cold_start_forces_a_target_even_with_no_sample():
    """2026-11-07 10:01 開群第一則 RICH「剛買冰美式進辦公室就開始摸魚，哥哥今天不忙嗎」：
    窗裡零則水軍，樣本湊不到五則，舊版直接放行，整段開群熱潮全是旁白。"""
    w = _worker()
    w._remember_speaker_name(GID, "後天")
    w._recent_texts_by_group[GID] = []
    w._recent_texts_at[GID] = []
    assert w._nameless_open_flow(GID, "剛買冰美式進辦公室就開始摸魚，哥哥今天不忙嗎")
    # 這句自己有名字也一樣：軸是看語流，名字由 _local_veto 放行
    assert w._nameless_open_flow(GID, _NAMED)
    assert w._local_veto(_NAMED, [], "", GID, nameless_flow=True, hot_names=("後天",)) == ""


def test_partial_sample_ramps_up_the_requirement():
    w = _worker()
    _seed(w, _SCENE[:2])
    assert w._nameless_open_flow(GID, _SCENE[0])
    _seed(w, [_NAMED, "萬英尺你嘴這麼硬，先來我這邊", _SCENE[1]])
    assert not w._nameless_open_flow(GID, _SCENE[0])


# ---- 餓號退讓：隔夜開群不能退 --------------------------------------------


class _SpeakDB:
    def __init__(self, last: float):
        self._last = last

    async def group_bot_last_spoke(self, _gid: int) -> dict:
        return {0: self._last}


def test_starved_account_still_forced_on_a_cold_open():
    """每一號都餓了十二小時，窗裡卻一則水軍都沒有 → 餓不算數。"""
    w = _worker()
    w.db = cast(Any, _SpeakDB(0.0))
    w._recent_texts_by_group[GID] = []
    w._recent_texts_at[GID] = []
    assert asyncio.run(w._nameless_flow_active(GID, _FRESH))


def test_starved_account_is_still_forced_once_the_window_is_populated():
    """10:56 小天後餓 29 分鐘被舊的餓號退讓放掉，又出一句無對象的話。"""
    w = _worker()
    w.db = cast(Any, _SpeakDB(0.0))
    _seed(w, _SCENE)
    assert asyncio.run(w._nameless_flow_active(GID, _FRESH))


def test_fed_account_is_forced_inside_a_nameless_stream():
    w = _worker()
    w.db = cast(Any, _SpeakDB(time.time()))
    _seed(w, _SCENE)
    assert asyncio.run(w._nameless_flow_active(GID, _FRESH))


def test_flow_needs_a_density_once_the_sample_is_full():
    w = _worker()
    # 樣本滿了（六則）卻只有一半是旁白 → 不算在自語
    _seed(
        w,
        [
            _NAMED,
            "萬英尺你嘴這麼硬，先來我這邊",
            "鈴鐺說得對，你也只會出一張嘴",
            _SCENE[0],
            _SCENE[1],
            _SCENE[2],
        ],
        tail=False,
    )
    assert not w._nameless_open_flow(GID, _SCENE[0])
    _seed(w, _SCENE)
    assert w._nameless_open_flow(GID, _SCENE[0])


def test_flow_is_judged_on_the_stream_not_on_this_line():
    """10:31 Chen：主題有點名，寫出來卻退回自己身上 → 軸要照樣啟動。"""
    w = _worker()
    _seed(w, _SCENE)
    named_line = "剛用蒸氣熨鬥燙好展示架的洋裝，那包子看起來很可以，只問妹妹不打算連姊姊的嘴一起餵飽嗎"
    assert w._nameless_open_flow(GID, named_line)
    assert "第一個逗號前" in w._local_veto(named_line, [], "", GID, nameless_flow=True, hot_names=("和進出口",))


def test_flow_off_when_the_stream_is_already_objected():
    w = _worker()
    _seed(w, [_NAMED, "萬英尺你嘴這麼硬，等一下先來我這邊", "鈴鐺說得對，你也只會出一張嘴"])
    assert not w._nameless_open_flow(GID, _SCENE[0])


# ---- 閘門 ----------------------------------------------------------------


_FRESH = "剛脫絲襪揉小腿，敢不敢先讓我驗腰力"


def test_nameless_candidate_in_a_nameless_stream_is_vetoed():
    w = _worker()
    _seed(w, _SCENE)
    veto = w._local_veto(
        _FRESH, [], "", GID, nameless_flow=True, hot_names=("萬英尺", "和進出口")
    )
    assert "第一個逗號前" in veto
    assert "萬英尺" in veto


def test_named_candidate_passes_the_flow_axis():
    w = _worker()
    _seed(w, _SCENE)
    assert (
        w._local_veto(_NAMED, [], "", GID, nameless_flow=True, hot_names=("萬英尺",))
        == ""
    )


def test_flow_axis_off_means_no_veto():
    w = _worker()
    _seed(w, _SCENE)
    # 這個場其他軸（部位／動作）照樣會攔，這裡只看這軸有沒有出手
    assert "第一個逗號前" not in w._local_veto(_FRESH, [], "", GID, nameless_flow=False)


def test_flow_veto_gets_its_own_code():
    assert AccountWorker._veto_code("最近好幾則…這則把名字放進第一個逗號前，直接接萬英尺") == (
        "nameless_open"
    )


# ---- 同題連發 ------------------------------------------------------------


def test_same_topic_as_the_line_before_is_caught():
    """20:35 小小「專一還是舔狗分很細欸」→ 20:36 小天後「你也那麼專一嗎」，26 秒。"""
    w = _worker()
    _seed(w, ["專一還是舔狗分很細欸，正邊看恐怖片邊塗乳液"], age=8.0, tail=False)
    assert w._same_topic_as_prev(GID, "吃完飯糰手指油油的，你也那麼專一嗎") == "專一"


def test_different_topic_passes():
    w = _worker()
    _seed(w, ["專一還是舔狗分很細欸，正邊看恐怖片邊塗乳液"], age=8.0, tail=False)
    assert w._same_topic_as_prev(GID, "戰豆連關心都不敢，哥哥先來抱我暖床") == ""


def test_same_topic_needs_the_short_window():
    w = _worker()
    _seed(w, ["專一還是舔狗分很細欸，正邊看恐怖片邊塗乳液"], age=5 * 60)
    assert w._same_topic_as_prev(GID, "吃完飯糰手指油油的，你也那麼專一嗎") == ""


def test_same_topic_veto_message():
    w = _worker()
    _seed(w, ["專一還是舔狗分很細欸"], age=8.0, tail=False)
    veto = w._local_veto("你也那麼專一嗎", [], "", GID)
    assert "別接著同一個詞" in veto
