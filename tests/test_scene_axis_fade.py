"""場景軸重播與拼裝句自檢（2026-11-05 15:32–16:08 桃花源實測破口）。

- 14:06 RICH「吹著冷氣伸懶腰內衣勒得有點悶」→ 16:01 小小「下午摸魚伸個懶腰內衣
  勒得好緊，出一張嘴就想上喔」：隔了 115 分鐘，一則句子把 14:06 的懶腰＋內衣、
  14:10 的摸魚、14:20 的「出一張嘴」重新拼起來。單軸 30 分鐘窗每個字都已經過期，
  逐字比對整句放行，群友看到的是同一格重播。
- 15:41 Chen「發情想亂咬就說…想咬姊姊哪裡直接講」→ 15:44 小天後「還想咬，弟弟你
  到底斷奶了沒」：相隔 2.5 分鐘同一個嘴上動作，身體部位清單抓不到「咬」。
- 15:37／15:41 兩則都是純評語，第三則 15:44 還是評語，播報密度門檻卡在 4 則樣本
  就永遠不觸發。

軸的壽命依共用軸數放大，全部由本群實際送出紀錄統計，不是禁用詞表。
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
    w.persona = {"name": name, "gender": "女", "age": 22}
    return w


def _seed(w: AccountWorker, pairs) -> None:
    """pairs＝[(幾秒前, 句子)]，由舊到新。"""
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def _veto(w: AccountWorker, candidate: str) -> str:
    return w._local_veto(candidate, [], "", GID)


def test_two_shared_axes_survive_the_short_window():
    """16:01 小小：懶腰＋內衣都來自 115 分鐘前的同一則，兩軸一起借。"""
    w = _worker()
    _seed(
        w,
        [
            (115 * 60, "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好"),
            (111 * 60, "咬著手搖吸管摸魚，小腿站到發痠，出門不用付錢是拿腰力抵飯錢嗎"),
            (300, "喵喵喵"),
        ],
    )
    veto = _veto(w, "下午摸魚伸個懶腰內衣勒得好緊，出一張嘴就想上喔")
    assert veto
    assert "懶腰" in veto and "內衣" in veto


def test_mosaic_from_three_old_sentences_is_vetoed():
    """三則舊句各借一軸，拼起來還是同一格。"""
    w = _worker()
    _seed(
        w,
        [
            (140 * 60, "剛拉下鐵門換掉包臀裙，倒了杯紅酒"),
            (130 * 60, "高跟鞋踢在門口，腳底板熱到發燙"),
            (120 * 60, "噴了香水自己聞著都心跳好快"),
        ],
    )
    veto = _veto(w, "鐵門拉下來高跟鞋還沒脫，香水味都散了")
    assert veto
    assert "拼過" in veto


def test_single_axis_inside_the_short_window_still_vetoes():
    """單軸一小時內算撞，舊行為不能因為改寫而放寬。"""
    w = _worker("RICH")
    _seed(w, [(26 * 60, "害我剛在位子伸個懶腰差點把扣子繃開"), (60, "你朋友對你這麼好")])
    veto = _veto(w, "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好")
    assert veto
    assert "懶腰" in veto


def test_single_axis_after_the_short_window_is_released():
    """一軸超過一小時就放行，不是永久封鎖。"""
    w = _worker()
    _seed(w, [(75 * 60, "下午吹冷氣腳痠想睡，真脫了衣服不知道能不能幹到我爽"), (120, "真假")])
    assert _veto(w, "洗完澡擦完身體乳，只想躺平") == ""


def test_mouth_gesture_repeat_two_minutes_apart():
    """15:41→15:44 同一個「咬」，身體部位清單抓不到。"""
    w = _worker("小天後")
    _seed(
        w,
        [
            (150, "發情想亂咬就說，少拿狂犬病當藉口，想咬姊姊哪裡直接講"),
            (30, "肯定是被狗咬傷了"),
        ],
    )
    veto = _veto(w, "人家都澄清不是她了還想咬，弟弟你到底斷奶了沒啊")
    assert veto
    assert "咬" in veto


def test_press_box_density_fires_on_three_samples():
    """只有三則水軍樣本、其中兩則純評語，第三則不准再評。"""
    w = _worker()
    _seed(
        w,
        [
            (7 * 60, "要舔也是往上舔敏感帶，舔腳到底什麼怪癖，有夠噁心"),
            (3 * 60, "發情想亂咬就說，少拿狂犬病當藉口，想咬姊姊哪裡直接講"),
            (40, "狂犬病發作了"),
        ],
    )
    veto = _veto(w, "人家都澄清不是她了還想咬，弟弟你到底斷奶了沒啊")
    assert veto
    assert "講別人的事" in veto or "咬" in veto


def test_tired_state_repeated_within_the_hour_is_vetoed():
    """一整個下午都在講累，第三則不能再拿累當開場。"""
    w = _worker("小天後")
    _seed(w, [(18 * 60, "當人太累，騎車吹到頭髮超塌只想躺著廢"), (60, "真假")])
    veto = _veto(w, "下班累到不想講話，飯都懶得吃")
    assert veto
    assert "累" in veto


def test_life_anchor_avoids_scene_another_account_just_used():
    """挑錨點就把別人的句子算進去，不要生成完才被自檢退掉。

    16:25 小小挑到「跑業務」（絲襪＋體力好的哥哥），但 15:25 才剛講過「還有體力把我
    抓著雙手幹到求饒」、16:22 小天後講腳底燙，等於選了一個注定要重寫的場景。
    """
    w = _worker()
    _seed(
        w,
        [
            (60 * 60, "力氣都耗在貨上了，晚上還有體力把我抓著雙手幹到求饒嗎"),
            (6 * 60, "脫了鞋腳底好燙，真有本事就去聯絡助理排我"),
        ],
    )
    anchors = [
        ("跑業務", "剛跑完業務回到家累死，脫掉高跟鞋和絲襪，只想找個體力好的哥哥來按摩"),
        ("酒吧", "週末好想去酒吧喝一杯，小妹子喝了酒最不設防"),
    ]
    picks = {w._pick_life_anchor(anchors, "小小", [], [], group_id=GID) for _ in range(20)}
    assert picks == {anchors[1][1]}

def test_same_account_repeating_the_gesture_after_the_short_window_is_vetoed():
    """17:32 小小又伸懶腰，16:01 才伸過（91 分鐘）：同一個號重做比別號撞更明顯。"""
    w = _worker("小小")
    old = "下午摸魚伸個懶腰內衣勒得好緊，出一張嘴就想上喔"
    _seed(w, [(91 * 60, old), (300, "真假")])
    w._own_scene_by_group[GID] = [(old, time.time() - 91 * 60)]
    veto = _veto(w, "在位子上伸懶腰胸口勒得好緊，想跑去陪妹子吃鍋就承認嘛")
    assert veto
    assert "懶腰" in veto


def test_same_gesture_from_another_account_keeps_the_short_window():
    """別的號 91 分鐘前伸過同一個懶腰，單軸照樣放行，不因為改寫變永久封鎖。"""
    w = _worker("小小")
    _seed(w, [(91 * 60, "下午摸魚伸個懶腰內衣勒得好緊"), (300, "真假")])
    assert _veto(w, "在位子上扭腰，椅子都坐不住了") == ""


def test_traditional_acid_variant_hits_the_state_axis():
    """17:16 腰「酸」背痛 → 18:01 腰「酸」死了，只收『痠』的偵測整條失效。"""
    w = _worker("Chen")
    _seed(w, [(45 * 60, "誰跟你在收錢，我站櫃整天腰酸背痛，只想找個硬的"), (60, "真假")])
    veto = _veto(w, "剛理完店裡的衣服腰酸死了，吃大餐不帶妹")
    assert veto
    assert "痠" in veto


def test_state_saturation_forces_a_non_body_scene():
    """兩小時三則都在講痠累，第四則換一個新字（喘）一樣算同一個心境。"""
    w = _worker("RICH")
    _seed(
        w,
        [
            (50 * 60, "站櫃站了一整天，腰酸背痛只想找個硬的"),
            (25 * 60, "摸魚按著發酸的肩膀，鱷魚當然剝皮做小廢包"),
            (12 * 60, "腳底板熱到發燙，脫了鞋就著地板散熱"),
        ],
    )
    veto = _veto(w, "提著兩袋東西爬上樓，進門靠著牆喘")
    assert veto
    assert "身體痠累" in veto


def test_anchor_picker_avoids_fatigue_anchor_when_the_group_is_saturated():
    """飽和時挑錨點就把疲勞場景排到後面，不要生成完才被退。"""
    w = _worker("小小")
    _seed(
        w,
        [
            (50 * 60, "站櫃站了一整天，腰酸背痛只想找個硬的"),
            (25 * 60, "摸魚按著發酸的肩膀，鱷魚當然剝皮做小廢包"),
            (12 * 60, "腳底板熱到發燙，脫了鞋就著地板散熱"),
        ],
    )
    anchors = [
        ("站櫃", "站櫃站了一整天，腳底板和小腿超痠，好想有個耐操的男人"),
        ("自｜夜市", "逛完夜市手提著鹹酥雞，紙袋油到透出來，涼拖鞋啪嗒啪嗒走回騎樓"),
    ]
    picks = {w._pick_life_anchor(anchors, "小天後", [], [], group_id=GID) for _ in range(20)}
    assert picks == {anchors[1][1]}
