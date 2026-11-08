"""群友性別提示：真人沒有性別欄位，模型照著猜就會把男的叫成姊姊。

20:45 Chen「萬英尺姊姊在店裡整理新衣服」，但萬英尺自己 19:52 才講「我這雞雞專打
背信棄義之人」、20:08「把小和的雞雞用鋸子割下來」。判不出來就別講，判錯比不講更假。
"""

from app.worker import AccountWorker

G = AccountWorker._sender_gender_hint

MALE_ID = 8857666885
FEMALE_ID = 999


def _h(rows, sid, incoming=""):
    return G([{"sender_id": s, "content": c} for s, c in rows], sid, incoming)


def test_self_reported_cock_is_male():
    rows = [(MALE_ID, "我這雞雞專打背信棄義之人"), (MALE_ID, "非常硬")]
    assert _h(rows, MALE_ID) == "男"


def test_wife_and_army_are_male():
    rows = [(MALE_ID, "我老婆昨天才唸我"), (MALE_ID, "當兵那兩年")]
    assert _h(rows, MALE_ID) == "男"


def test_husband_and_period_are_female():
    rows = [(FEMALE_ID, "我老公都不碰我"), (FEMALE_ID, "月經來肚子悶")]
    assert _h(rows, FEMALE_ID) == "女"


def test_one_mention_each_way_is_unknown():
    """一票對一票不硬猜，寧可回空字串。"""
    rows = [(FEMALE_ID, "老公昨天買的"), (FEMALE_ID, "他說我雞雞小")]
    assert _h(rows, FEMALE_ID) == ""


def test_other_senders_do_not_count():
    rows = [(MALE_ID, "別人的雞雞很小"), (MALE_ID, "我老婆很兇")]
    assert _h(rows, 12345) == ""


def test_incoming_line_counts_too():
    assert G([], MALE_ID, "雞雞") == ""
    assert G([], MALE_ID, "我這雞雞") == "男"
    assert G([{"sender_id": MALE_ID, "content": "我這雞雞很硬"}], MALE_ID, "對啊") == "男"


def test_no_sender_id_is_unknown():
    assert G([{"sender_id": MALE_ID, "content": "我這雞雞"}], None) == ""
