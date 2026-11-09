"""相鄰撞格與動作重複的硬自檢（2026-11-05 13:40–14:12 桃花源實測破口）。

- 13:40 小小「害我剛在位子伸個懶腰差點把扣子繃開」→ 13:41 小天後「剛補完防曬身上
  還熱著」：字面不同、同一分鐘，起頭字頻率還沒累積到 2 次，舊的頻率門檻攔不到。
- 14:06 RICH「吹著冷氣伸懶腰內衣勒得有點悶」：26 分鐘後再做一次同一個動作，
  場景字串不同，群友看到的是同一格。
- 13:15～13:52 十一則裡五則純評語，但沒有一則是連著的，連兩則才攔的規則整批放過。

全部動態：比對的是本群實際送出紀錄，不是禁用詞清單。
"""

from types import SimpleNamespace
from typing import Any, cast
import time

from app.worker import AccountWorker

GID = -1001


def _worker(name: str = "小天後") -> AccountWorker:
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
    """pairs＝[(幾秒前, 句子)]，由舊到新。"""
    now = time.time()
    w._recent_texts_by_group[GID] = [t for _age, t in pairs]
    w._recent_texts_at[GID] = [now - age for age, _t in pairs]


def test_vetoes_same_opening_char_on_the_line_right_before():
    w = _worker()
    _seed(w, [(90, "這副看不起人的拽樣超欠捏，害我剛在位子伸個懶腰差點把扣子繃開")])
    veto = w._local_veto("剛補完防曬身上還熱著，那麼小塞進來我大概連叫都懶得叫", [], "", GID)
    assert veto
    assert "剛" in veto and "上一則" in veto


def test_same_opening_char_far_apart_in_window_is_not_blocked():
    w = _worker()
    _seed(w, [(11 * 60, "剛脫高跟鞋腳底板快斷了，只想找個人大力揉一揉")])
    veto = w._local_veto("蹲在冰箱前面找東西吃，冷氣吹得小腿起雞皮疙瘩", [], "", GID)
    assert veto == ""


def test_vetoes_body_gesture_done_by_another_account_26_minutes_ago():
    w = _worker("RICH")
    _seed(
        w,
        [
            (26 * 60, "這副看不起人的拽樣超欠捏，害我剛在位子伸個懶腰差點把扣子繃開"),
            (60, "你朋友對你這麼好"),
        ],
    )
    veto = w._local_veto(
        "吹著冷氣伸懶腰內衣勒得有點悶，天天吃烤肉體力是有多好", [], "", GID
    )
    assert veto
    assert "懶腰" in veto


def test_scene_veto_names_a_concrete_alternative_scene():
    """撞動作時要把還能用的場景念出來，不能只叫她「換一個動作」。

    只給抽象指令，模型換字不換格，三輪重寫照樣端回洗澡／床上，最後走 hold
    照發（20:08 小小第三次「剛洗完澡」就是這樣出貨的）。
    """
    w = _worker("小天後")
    _seed(w, [(12 * 60, "剛洗完澡好熱，誰講你壞話")])
    veto = w._local_veto("洗完澡床上等你約我實戰", [], "", GID)
    assert "洗完澡" in veto
    assert "「" in veto.split("或直接改用")[-1]


def test_scene_group_veto_blocks_same_routine_different_words():
    """字面不同的同一件事要判成撞，並給不同組的場景。

    20:42–21:25 四個號輪流報備同一套睡前美容：擦乳液／洗完澡擦頭髮／敷面膜／
    吹頭髮／躺床／開暖氣。動作字面全不同，群友看到的是四個人共用一張行程表。
    """
    w = _worker("RICH")
    _seed(
        w,
        [
            (35 * 60, "擦著乳液聽小萬聊吃辣我都流汗想被抱緊了"),
            (18 * 60, "吹頭髮吹到好熱，你舌頭夠靈活嗎"),
        ],
    )
    veto = w._local_veto("敷著面膜配冰紅酒最讚了", [], "", GID)
    assert "睡前" in veto
    assert "不同組的場景" in veto


def test_scene_group_passes_when_group_is_fresh():
    """同組最近沒人做過就放行，不是看到洗澡就一律擋。"""
    w = _worker("RICH")
    _seed(w, [(30 * 60, "剛跑完業務回到家累死"), (20 * 60, "夜市紙袋油到透出來")])
    veto = w._local_veto("剛剛在試穿新買的蕾絲丁字褲勒得好緊", [], "", GID)
    assert "睡前" not in veto


def test_preamble_flow_cuts_the_head_physically():
    """語流連三則報備前情時，直接剪掉逗號前那段，不是只寫在提示裡。

    提示詞寫了「這則不要交代前情」，模型照寫（21:00–21:25 六則）；剪前情留後半句，
    對象與撩撥都還在後半句。
    """
    w = _worker("小小")
    _seed(
        w,
        [
            (9 * 60, "擦著乳液聽小萬聊吃辣我都流汗想被抱緊了"),
            (6 * 60, "規範是多久沒射才這麼暴躁，剛洗完澡擦頭髮看琪琪罵人超狠"),
            (3 * 60, "吹頭髮吹到好熱，你舌頭夠靈活嗎"),
        ],
    )
    assert w._anchor_flow(GID) >= 3
    cut = w._strip_anchor_head("剛擦完乳液躺上床，這件睡衣是不是太露", GID)
    assert cut and "剛擦完乳液" not in cut
    assert "睡衣" in cut


def test_anchor_flow_counts_no_comma_anchor_lines():
    """沒逗號的整句也算前情（擦著乳液…想被抱緊了），密度才量得準。"""
    w = _worker("小小")
    _seed(
        w,
        [
            (8 * 60, "擦著乳液聽小萬聊吃辣我都流汗想被抱緊了"),
            (5 * 60, "吹頭髮吹到好熱，你舌頭夠靈活嗎"),
            (2 * 60, "趴在床上看森森罵狗，害我好想被按著撞"),
        ],
    )
    assert w._anchor_flow(GID) >= 2


def test_preamble_head_kept_when_flow_is_short():
    w = _worker("小小")
    _seed(w, [(5 * 60, "三上悠亞不好嗎")])
    assert w._strip_preamble_head("剛擦完乳液躺上床，這件睡衣是不是太露", GID)


def test_vetoes_press_box_when_two_thirds_of_recent_bots_are_press_box():
    w = _worker()
    _seed(
        w,
        [
            (25 * 60, "被鱷魚咬斷直接不用當男人，群裡弟弟誰敢拿雞雞去賭"),
            (22 * 60, "姊姊約過兩次，有些弟弟床上功夫硬是讓人忘不掉"),
            (18 * 60, "換這張妹子看到直接滑掉，你確定要拿來當門面嗎"),
            (7 * 60, "祈禱是哪招，真有心怎麼不叫外送熱飲過去"),
        ],
    )
    veto = w._local_veto("森森被誇可愛還急著反駁，傲嬌小狗欠調教是不是", [], "", GID)
    assert veto
    assert "講別人的事" in veto


def test_business_line_is_exempt_from_press_box_density():
    """帶人入會、找助理本來就該講外面的事，不逼她補自曝。"""
    w = _worker()
    _seed(
        w,
        [
            (25 * 60, "被鱷魚咬斷直接不用當男人，群裡弟弟誰敢拿雞雞去賭"),
            (20 * 60, "姊姊約過兩次，有些弟弟床上功夫硬是讓人忘不掉"),
            (7 * 60, "換這張妹子看到直接滑掉，你確定要拿來當門面嗎"),
        ],
    )
    veto = w._local_veto(
        "想吃得先去聯絡助理排隊，表現夠聽話姊姊才考慮給你嚐一口", [], "", GID
    )
    assert veto == ""


def test_press_box_passes_when_recent_bots_had_self_disclosure():
    w = _worker()
    _seed(
        w,
        [
            (30 * 60, "咬著手搖吸管摸魚，小腿站到發痠，出門不用付錢難道是拿腰力抵飯錢嗎"),
            (20 * 60, "剛補完防曬身上還熱著，那麼小塞進來我大概連叫都懶得叫"),
        ],
    )
    veto = w._local_veto("傲嬌小狗欠調教是不是", [], "", GID)
    assert veto == ""
