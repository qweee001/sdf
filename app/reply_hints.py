"""回覆提示詞的純函數層（從 worker.py 抽出的第一批）。

worker.py 13.5k 行、2800+ 分支點，這些 hint 是裡面最「純」的一塊：
只吃歷史訊息與幾個注入的依賴，不碰 DB、不碰 Telegram、不改狀態。
抽出來之後可以單獨測、單獨改，worker 的 method 只剩一行委託。

命名刻意與 worker 的方法一一對應，既有測試不必改。
"""

from __future__ import annotations

from collections.abc import Callable

# 露骨熱度詞：判「這一群是不是整段都在聊色」，過熱就降溫或轉場
HEAT_WORDS = (
    "深淺", "撞到底", "餵飽", "交作業", "吸緊", "兩張嘴",
    "蕾絲", "舔", "剝光", "腿軟", "騷", "硬", "濕", "內衣",
    "保險套", "套套", "懷孕", "推屁股", "雞巴", "屌", "小穴",
    "騷穴", "吹", "含", "約嗎", "開房", "床上", "幹", "胸",
)

# 連續幾則就該降溫
TOPIC_HEAT_MAX_ROUNDS = 4
# 講到這裡還算「現場正在燒」的則數上限（超過就真的轉場，不再留半檔）
TOPIC_HEAT_STILL_HOT_LIMIT = 7

# 小圈圈：同圈講話更熟更酸，跨圈有距離感
CREW_MAP = {
    "小小": "毒舌姐妹淘（小小＋小天後）",
    "小天後": "毒舌姐妹淘（小小＋小天後）",
    "RICH": "輕熟氣質組（RICH＋Chen）",
    "Chen": "輕熟氣質組（RICH＋Chen）",
}

# 對方明確求約的字面（這是意圖，不是尺度）
BOOKING_CUES = (
    "怎麼約", "哪裡約", "多少錢", "今晚可以嗎", "可以約嗎",
    "想約你", "約出來", "跟妳睡",
)


def topic_heat_hint(
    history: list[dict],
    *,
    group_id: int,
    axes_fn: Callable[[str], int],
    stats: dict,
    name: str = "",
) -> str:
    """連續色情話題超過閾值 → 回一句降溫或轉場指令（否則回空字串）。

    真人聊完色會自然疲勞轉回日常，只有機器人會在同一個色情閉環裡無限打轉。
    兩階處理：真人最後一句還在講色就只降半檔，最後一句已轉日常或整段過長才真轉場。
    """
    rows = list(history or [])[-8:]
    if not rows:
        return ""
    hot = 0
    for row in rows:
        body = str(row.get("content") or "")
        if any(word in body for word in HEAT_WORDS):
            hot += 1
    if hot < TOPIC_HEAT_MAX_ROUNDS:
        return ""
    humans = [r for r in rows if str(r.get("role")) != "assistant"]
    last_human = str((humans[-1] if humans else {}).get("content") or "")
    still_hot = axes_fn(last_human) >= 1
    if still_hot and hot < TOPIC_HEAT_STILL_HOT_LIMIT:
        stats["topic_cooldown_half"] = int(stats.get("topic_cooldown_half", 0)) + 1
        return (
            "【話題溫度計・降半檔】：群裡這幾則都在講色，但真人最後一句還在同一個坑裡。"
            "這一輪不要整則轉成日常，留一個露骨形容詞或一個身體感覺，"
            "再接一件很小的事（例：「腳底板冰到發麻，小穴卻還有點熱」）。"
            "一句話講完，不要開新場景、不要變成生活報告。"
        )
    stats["topic_cooldown_forced"] = int(stats.get("topic_cooldown_forced", 0)) + 1
    print(
        f"[{name}] topic-cooldown: 群 {group_id} 色情話題連續 {hot} 則，強制降溫",
        flush=True,
    )
    return (
        "【話題溫度計過熱｜這一輪強制冷卻轉場】：最近群裡已經連續太多則都在聊色情梗了，"
        "真人姐妹聊到這裡會自然膩掉、自然轉回生活！"
        "這一輪絕對禁止再聊性、器官、約炮或任何露骨內容！"
        "請改講一件真實的日常瑣事來降溫（例如：剛点的外送到嗎、晚餐吃什麼、"
        "今天加班到幾點、追的劇看到第幾集、外面下雨不想出門、生理期肚子好痛），"
        "讓群裡感覺妳們是活人不是發情機器！"
    )


def tension_ladder_hint(
    history: list[dict],
    incoming_text: str,
    *,
    axes_fn: Callable[[str], int],
    density: float,
) -> str:
    """情節張力階梯 L1–L4：無場景機時的尺度來源（每則現算，無記憶）。

    只有「被明確求約」用字面判；尺度層用語料現算的色度，不靠靜態詞表——
    詞表永遠落後現場，還會把「洗澡」這種生活詞誤判成 L3。
    """
    if any(word in incoming_text for word in BOOKING_CUES):
        level = 4
    elif axes_fn(incoming_text) >= 2 or density >= 0.5:
        level = 3
    elif axes_fn(incoming_text) >= 1 or density >= 0.2:
        level = 2
    else:
        level = 1

    if level == 1:
        return (
            "【情節張力階梯・L1日常調情與生活發騷】：以真實女生的隨性口吻接話，但處處帶刺帶鉤！"
            "帶點調皮、傲嬌或微騷（如抱怨天氣悶熱想找人吹冷氣、問誰要請喝手搖飲再來床上報答），接住對方的梗，展現真實撩人的台灣女生！"
        )
    if level == 2:
        return (
            "【情節張力階梯・L2日常曖昧與男女挑釁】：從生活細節切入男女張力與身體對抗！"
            "挑釁群裡男生的體力、耐力與尺寸（例如「嘴巴這麼狂，真上場能撐過五分鐘嗎」「別光會吹，敢不敢讓我腿軟」），"
            "帶點微醺傲嬌與肉體暗示，強烈挑起男人的勝負欲與性遐想！"
        )
    if level == 3:
        return (
            "【情節張力階梯・L3情慾拉扯與性感色誘】：尺度完全放開，充滿肉體張力與性感誘惑！"
            "順著對方的話題自然推進身體反應與性感細節（如貼身衣物滑開、敏感帶碰觸的微喘、小穴濕熱發燙、想被用力抱緊揉捏、抓破床單、深入撞擊），"
            "言語大膽騷浪、調情自然，將情慾張力拉滿，激發男人的性衝動與肉體渴望！絕不主動提助理！"
        )
    # 明確求約：勾一下後隨口推助理，不要業務味
    return (
        "【情節張力階梯・L4實戰收斂】：對方已經急著想約想幹了！"
        "先大膽開撩讓對方心癢難耐（例如「看你本事能不能讓我腿軟哭著求饒」），最後隨性丟一句「自己去找助理排啦」，不要有業務廣告味！"
    )


def social_dynamics_hint(history: list[dict], *, persona_name: str) -> str:
    """真人社交動力學：不是每句都接、會吐槽拆台（空歷史回空字串）。"""
    rows = list(history or [])[-6:]
    if not rows:
        return ""
    parts: list[str] = []
    crew = CREW_MAP.get(str(persona_name or ""), "")
    if crew:
        parts.append(
            f"你屬於群裡的小圈圈：{crew}。跟同圈的人講話可以更熟、更酸、更不客套。"
        )
    last = rows[-1]
    if str(last.get("role")) == "assistant":
        parts.append(
            "上一則是姊妹說的：請優先【吐槽、質疑、拆台或翻白眼】"
            "（例如「又在臭美」「你說到最後還不是要買」），不要一味捧場附和。"
        )
    else:
        parts.append(
            "如果群友這句話沒有爆點、很普通，你可以選擇【只回一個表情／短評／"
            "甚至這句不接去講自己的事】，真人不會每句都認真接。"
        )
    return (
        "\n【真人社交動力學（拒絕捧哉機器人）】：\n"
        + "".join(parts)
        + "\n禁止每句都完美接話：真人會無視、會冷場、會吐槽。"
    )
