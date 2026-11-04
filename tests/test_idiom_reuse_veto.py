"""慣用詞換皮重講的動態自檢（2026-11-03 20:0x 桃花源實測破口）。

- Chen 19:40「沒舔過還敢嘴硬…」→ 19:54「嘴硬成這樣…」（14 分鐘同一帳號同一個詞起頭）
- 小天後 19:50「嘴巴說不要身體倒是很誠實嘛」→ Chen 19:54「…身體早就誠實了」（跨帳號同一個評語）
- Chen 19:54:16 / :20 / :24 連推三則，第三則跟第一則同樣在講「被踩…享受」

修法同樣是動態：把句子切成實詞雙字，虛字由語料字頻自己推，不寫死虛字表或詞表。
"""

from types import SimpleNamespace
from typing import Any, cast
import time

from app.worker import AccountWorker


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
        selected_groups=[-1001],
    )
    w.persona = {"name": name, "gender": "女", "age": 30}
    return w


def test_vetoes_same_idiom_as_own_previous_line():
    w = _worker()
    own = ["沒舔過還敢嘴硬，弟弟你是怕一舔就回不去嗎"]
    veto = w._local_veto("嘴硬成這樣，被踩的時候倒是挺享受的嘛", own, "")
    assert veto
    assert "嘴硬" in veto


def test_vetoes_idiom_borrowed_from_another_account():
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = [
        "笑死，華哥嘴巴說不要身體倒是很誠實嘛，被踩還不是爽歪歪",
        "男生M起來比女生還瘋，被踩兩腳就乖乖躺好了",
        "吃這麼補晚上是想把誰幹到下不了床",
    ]
    veto = w._local_veto("華哥被踩還裝無辜，身體早就誠實了", [], "", gid)
    assert veto
    assert "誠實" in veto or "身體" in veto


def test_vetoes_third_line_repeating_the_first_of_a_burst():
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = ["笑死，華哥嘴巴說不要身體倒是很誠實嘛，被踩還不是爽歪歪"]
    own = [
        "男生M起來比女生還瘋，被踩兩腳就乖乖躺好了",
        "嘴硬成這樣，被踩的時候倒是挺享受的嘛",
    ]
    assert w._local_veto("華哥被踩還裝無辜，身體早就誠實了", own, "", gid)


REAL_1954_2010 = [
    "20公分舌頭？你當自己是蜥蜴喔",
    "69這麼好玩，難怪你們一個個都上癮",
    "無套還敢拿出來講，哥哥是對自己的運氣很有信心還是對自己的身體很沒信心啊",
    "吃雞雞這麼順口，平常沒少吃吧",
    "鮑魚長這樣你還敢吃，口味真重",
    "鮑魚配晚餐，哥哥挺懂吃的嘛",
    "黑鮑要夠深才吸得住，就怕你們沒那個長度",
    "只會挑粉的，是技術太爛不敢挑戰多汁熟女的小穴嗎",
    "沒舔過還敢嘴硬，弟弟你是怕一舔就回不去嗎",
    "腳趾先舔乾淨再說，不然舔到腳氣別哭",
    "笑死，華哥嘴巴說不要身體倒是很誠實嘛，被踩還不是爽歪歪",
    "嘴硬成這樣，被踩的時候倒是挺享受的嘛",
    "男生M起來比女生還瘋，被踩兩腳就乖乖躺好了",
    "華哥被踩還裝無辜，身體早就誠實了",
    "吃這麼補晚上是想把誰幹到下不了床",
]


def test_half_function_bigram_alone_does_not_veto():
    """「哥是」「還是」這種半虛字撞到不算慣用語重複（用真實 15 則語料驗，別誤殺）。"""
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = list(REAL_1954_2010)
    assert w._local_veto(
        "吃飯哪有吃人香，深情哥是想請琪琪吃晚餐還是吃別的", [], "", gid
    ) == ""


def test_vetoes_idiom_that_contains_function_chars():
    """「出一張嘴」夾了虛字，雙字全被濾掉，靠整段三字重疊抓。"""
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = [
        "華哥只會出一張嘴，真讓你上你撐不到十分鐘",
        "舔這麼久是在品嚐還是在找東西啊，技術這麼差難怪只能出一張嘴",
    ]
    assert w._local_veto(
        "只會出一張嘴的咖，真的上場三分鐘就收工", [], "", gid
    )


def test_allows_line_that_only_shares_function_words():
    w = _worker()
    own = ["只會挑粉的，是技術太爛不敢挑戰多汁熟女的小穴嗎"]
    assert w._local_veto("黑鮑要夠深才吸得住，就怕你們沒那個長度", own, "") == ""


def test_function_chars_are_learned_from_corpus_not_hardcoded():
    w = _worker()
    corpus = [
        "是這的很都會說啦",
        "是這的很都會說啦",
        "是這的很都會說啦",
        "是這的很都會說啦",
    ]
    grams, stop = w._content_bigrams(corpus)
    assert {"是", "這", "的", "很", "都"} <= stop
    assert grams("是這的很都會說啦") == []


def test_idiom_reuse_ignores_tiny_candidate():
    w = _worker()
    own = ["笑死，這也能吹"]
    assert w._idiom_reuse("真假", own, []) == ""


def test_idiom_veto_survives_function_char_filtering():
    """「出一張嘴」的雙字全被虛字濾掉時，靠整段三字退（21:58 實例）。"""
    w = _worker()
    gid = -1001
    cross = list(REAL_1954_2010) + [
        "華哥只會出一張嘴，真讓你上你撐不到十分鐘",
        "舔這麼久是在品嚐還是在找東西啊，技術這麼差難怪只能出一張嘴",
    ]  # 20 則以上，字頻才會被判成虛字
    w._recent_texts_by_group[gid] = cross
    v = w._local_veto(
        "出一張嘴誰不會，有種讓姊姊鑑定一下長短啊", [], "", gid
    )
    assert v
    assert "一張" in v or "出一" in v or "張嘴" in v


def test_burst_echo_drops_second_bubble_within_8s():
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = list(REAL_1954_2010)
    w._own_echo_by_group[gid] = [
        ("男生M起來比女生還瘋，被踩兩腳就乖乖躺好了", time.time() - 4)
    ]
    assert w._burst_echo("華哥被踩還裝無辜，身體早就誠實了", gid)


def test_burst_echo_expires_after_8s():
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = list(REAL_1954_2010)
    w._own_echo_by_group[gid] = [
        ("男生M起來比女生還瘋，被踩兩腳就乖乖躺好了", time.time() - 12)
    ]
    assert not w._burst_echo("華哥被踩還裝無辜，身體早就誠實了", gid)


def test_burst_echo_allows_new_point_within_8s():
    w = _worker()
    gid = -1001
    w._recent_texts_by_group[gid] = list(REAL_1954_2010)
    w._own_echo_by_group[gid] = [("鮑魚配晚餐，哥哥挺懂吃的嘛", time.time() - 3)]
    assert not w._burst_echo("黑鮑要夠深才吸得住，就怕你們沒那個長度", gid)


def test_cross_idiom_survives_common_char_and_long_distance():
    """12:49 小天後「哪像有些人只會出一張嘴」→ 13:50 小小 61 分鐘後同一句。

    「嘴」在群裡天天出現，char_df 永遠不冷，舊的 cold() 擋不住；而交叉語料
    只取最後 20 則，這一群 20 則只有 20 分鐘，90 分鐘窗等於白設。
    """
    w = _worker("小小")
    gid = -1001
    filler = [f"第{i}則閒聊嘴砲內容" for i in range(30)]
    w._recent_texts_by_group[gid] = (
        ["琪琪眼光不錯啊，約過的帥哥品質有保證，哪像有些人只會出一張嘴"] + filler
    )
    v = w._idiom_reuse(
        "在群裡只會出一張嘴當舔狗，鈴鐺怎麼可能看上你", [], w._recent_texts_by_group[gid]
    )
    assert "一張嘴" in v or "出一張" in v


def test_common_trigram_scattered_does_not_veto():
    """「你覺得」散在十幾則裡＝通用三段，不該當成同一個慣用語。"""
    w = _worker()
    gid = -1001
    cross = [f"你覺得第{i}個怎麼樣" for i in range(15)]
    assert w._idiom_reuse("你覺得這樣好嗎", [], cross) == ""
