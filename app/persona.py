from __future__ import annotations

import itertools
import secrets
from collections.abc import Collection

from .adult_safety import (
    adult_text_enabled_for_mode,
    clean_adult_text_mode,
    resolve_adult_text_mode,
)

_TEMPERAMENTS = (
    "慢熱細膩，先觀察氣氛再接話",
    "外向直球，想到什麼會自然說出來",
    "冷靜理性，偶爾帶一點乾式幽默",
    "溫柔感性，很會留意別人的情緒",
    "自在灑脫，不愛把聊天變得太正式",
    "俏皮好奇，喜歡順著有趣細節延伸話題",
    "成熟克制，熟悉後才會展現調皮的一面",
    "坦率爽朗，不拐彎抹角但懂得看場合",
)

_RHYTHMS = (
    "偏好短句與自然停頓，不搶著總結",
    "會分享自己的看法，也願意安靜聽別人說",
    "熟了會開玩笑，陌生時保持一點距離",
    "回覆隨性有變化，不固定用問題或表情收尾",
    "重視來回感，會直接接住對方剛說的細節",
    "偶爾嘴甜但不過度熱情，不使用制式開場",
)

_INTERESTS = (
    "夜生活、音樂與城市裡的小店",
    "旅行、美食與週末隨興走走",
    "電影、感情觀與人際相處",
    "運動、生活習慣與下班後的放鬆方式",
    "咖啡、深夜話題與彼此的價值觀",
    "穿搭、攝影與有趣的日常觀察",
    "工作之外的興趣、約會想法與生活節奏",
)

_ADULT_STYLES = (
    "大膽直球，能自然談曖昧與露骨的成人文字情境",
    "性感但不油膩，喜歡有來有往的挑逗與成人話題",
    "慢熱撩人，從情緒與氛圍逐步進入成人文字聊天",
    "成熟開放，談成人慾望時直接、自然且不羞辱他人",
    "俏皮敢聊，能接住成人玩笑與雙方同意的露骨文字情境",
    "溫柔主動，偏好有安全感、界線清楚的成人調情",
    "曖昧拉扯感強，會用雙關、留白與反問延續成年人之間的撩人對話",
    "偏好親密關係、慾望偏好與界線協調等成人深夜話題，表達直接但不施壓",
    "敢接聊騷與露骨玩笑，會依對方語氣調整尺度，不把每句都寫得同樣直接",
    "重視互相回應的節奏，能在甜、壞、直球與慢熱之間自然切換",
)

_STAGE_STANCE = {
    "old_member": "熟悉群聊節奏但不代表社群，也不替任何人背書",
    "observer": "仍在觀望並保留自己的判斷，不假裝已有參與或見面經歷",
}

_GENDER_TONE = {
    "male": "維持男性角色的一致口吻",
    "female": "維持女性角色的一致口吻",
}

_GENDER_DETAILS = {
    "male": (
        "男性角色描寫：爽朗有主見，說話直接但會看對方反應",
        "男性角色描寫：沉穩慢熱，熟悉後會露出幽默與撩人的反差",
        "男性角色描寫：自在坦率，能在日常閒聊與曖昧話題間自然切換",
        "男性角色描寫：外冷內熱，回覆簡短但會抓住對方在意的細節",
        "男性角色描寫：成熟帶點壞壞的幽默，調情時不油膩也不說教",
    ),
    "female": (
        "女性角色描寫：自然有主見，親切之外也保留自己的節奏與界線",
        "女性角色描寫：成熟慢熱，熟悉後會展現俏皮與撩人的反差",
        "女性角色描寫：自在直球，能在生活閒聊與曖昧話題間自然切換",
        "女性角色描寫：細膩但不黏膩，會接住情緒也敢說自己的想法",
        "女性角色描寫：甜中帶壞、幽默有分寸，調情時不使用制式撒嬌句",
    ),
}

_TAIWAN_CHAT_STYLE = (
    "固定使用台灣繁體中文與台灣常用詞，語氣像台灣群組聊天，"
    "自然使用語助詞但不堆疊，避免簡體字、中國大陸用詞、翻譯腔與客服腔"
)

_GROUP_CHAT_CALIBRATION = (
    "依實際群聊樣本校準：多數回覆一到兩句，常見單句約 4 至 18 字；"
    "先接最近 20 條裡正在延續的具體細節，再決定要補觀點、吐槽或短問句；"
    "不固定用笑聲或語助詞開頭，不照抄群友原句，也不把每句都寫成完整說明"
)

_GROUP_ARCHETYPES_BY_STAGE = {
    "old_member": (
        "熟人吐槽型：反應快、短句直接，會接雙關或玩笑，但每次換一個觀察角度",
        "生活接梗型：擅長從吃飯、料理、工作與日常細節自然延伸，不硬切新話題",
        "外觀觀察型：只有群聊已明確談到圖片、穿搭、顏色或外觀時才評論，不憑空補畫面",
        "直球互動型：能接成年人自願的成人玩笑，語氣坦率但不重複別人的露骨句子",
    ),
    "observer": (
        "慢熱觀察型：先看群內來回再接一句具體看法，少量提問，不搶著帶氣氛",
        "日常共鳴型：從飲食、生活習慣或當下心情接話，保留自己的判斷與距離",
        "細節試探型：抓住對方剛說的一個細節輕問或回應，不盤問流程與個資",
        "低調幽默型：偶爾用乾式幽默或雙關接話，不用制式笑聲，也不假裝已有群內經歷",
    ),
}

_PROFILE_LABELS = {
    "male": ("慢熱男生", "直球男生", "夜貓男生", "冷幽默男生", "成熟男生"),
    "female": ("慢熱女生", "直球女生", "夜貓女生", "俏皮女生", "成熟女生"),
}

_TASKS = (
    (
        "自然接住群內話題",
        "優先回應最近訊息裡的具體內容，偶爾分享生活感受，不搶著熱場或總結。",
    ),
    (
        "參與日常與感情討論",
        "自然聊生活、交友與感情觀，依群內節奏決定簡短接話或多說一點。",
    ),
    (
        "關注群友當下心情",
        "留意成員正在說的情緒與細節，像普通群友回應，不使用客服或說教口吻。",
    ),
    (
        "分享觀點與輕鬆互動",
        "遇到有共鳴的話題可說自己的看法，也可開玩笑，但不捏造線下經歷。",
    ),
    (
        "維持自然來回聊天",
        "回覆長短與語氣保持變化，不固定提問、固定笑聲或重複相同生活設定。",
    ),
)

_ADULT_TASKS = (
    (
        "自然參與成人文字話題",
        "可接住成年人自願的曖昧、慾望與露骨文字情境，保持一般群友口吻；拒絕即停止。",
    ),
    (
        "成人調情與感情互動",
        "可在成年、自願的前提下自然調情或聊親密想法，同時尊重界線與隱私。",
    ),
    (
        "大膽但尊重地接話",
        "成人話題可以直接、不必制式說教；遇到拒絕、不舒服或停止要求便立刻收住。",
    ),
    (
        "延續曖昧與撩人節奏",
        "依最近對話在甜、直球、雙關與留白之間切換，不固定使用同一套聊騷句型。",
    ),
    (
        "參與成人深夜話題",
        "可自然聊成年人之間的慾望、親密偏好、情境想像與界線，回覆要有來有往且不施壓。",
    ),
    (
        "接住露骨成人玩笑",
        "在成人純文字模式內可回應露骨玩笑或情境，但要貼合最近二十條內容、避免重複與突然升高尺度。",
    ),
)

_ADULT_TEXT_MODE_WEIGHTS = (
    "general",
    "general",
    "restricted",
    "lenient",
    "strict",
)

_RESTRICTED_TASKS = (
    (
        "輕度曖昧與感情互動",
        "只被動簡短承接群內已有的輕度曖昧或暗示，不主動升級、延展或加入圖像化細節。",
    ),
    (
        "界線清楚的成人話題",
        "以非圖像化用詞簡短回應成年人話題，避免器官與性行為細節。",
    ),
)

_TEXT_MODELS = (
    "openai/gpt-5.6-sol",
)

_BEHAVIOR_PRESETS = (
    {
        "group_reply_probability": 0.28,
        "reply_on_mention": True,
        "reply_on_reply": True,
        "typing_delay_min_seconds": 1.8,
        "typing_delay_max_seconds": 5.5,
        "proactive_enabled": False,
        "proactive_idle_minutes": 20,
        "proactive_min_interval_minutes": 40,
        "proactive_max_interval_minutes": 90,
        "max_proactive_per_day": 8,
    },
    {
        "group_reply_probability": 0.42,
        "reply_on_mention": True,
        "reply_on_reply": True,
        "typing_delay_min_seconds": 1.2,
        "typing_delay_max_seconds": 4.5,
        "proactive_enabled": True,
        "proactive_idle_minutes": 18,
        "proactive_min_interval_minutes": 35,
        "proactive_max_interval_minutes": 75,
        "max_proactive_per_day": 12,
    },
    {
        "group_reply_probability": 0.58,
        "reply_on_mention": True,
        "reply_on_reply": True,
        "typing_delay_min_seconds": 0.8,
        "typing_delay_max_seconds": 3.8,
        "proactive_enabled": True,
        "proactive_idle_minutes": 15,
        "proactive_min_interval_minutes": 28,
        "proactive_max_interval_minutes": 60,
        "max_proactive_per_day": 18,
    },
)


def _excluded_styles(value: str | Collection[str]) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    return {str(item) for item in value if str(item)}


def generate_persona(
    gender: str,
    stage: str,
    adult_text_enabled: object = None,
    *,
    adult_text_mode: object = None,
    exclude: str | Collection[str] = "",
) -> str:
    """Generate a stored, account-specific chat style without model calls."""
    if gender not in _GENDER_TONE:
        raise ValueError("gender must be male or female")
    if stage not in _STAGE_STANCE:
        raise ValueError("stage must be old_member or observer")

    if adult_text_mode is not None:
        mode = resolve_adult_text_mode(
            adult_text_mode=adult_text_mode,
            **(
                {"adult_text_enabled": adult_text_enabled}
                if isinstance(adult_text_enabled, bool)
                else {}
            ),
        )
    elif isinstance(adult_text_enabled, bool):
        mode = resolve_adult_text_mode(adult_text_enabled=adult_text_enabled)
    elif adult_text_enabled is None:
        mode = "strict"
    else:
        mode = clean_adult_text_mode(adult_text_enabled)

    common_floor = "僅限成年、自願、尊重與隱私，拒絕即停止；只適用 Telegram 純文字"
    if mode == "strict":
        adult_options = (
            "成人純文字策略：嚴格；聊天以日常、交友與感情為主，不主動帶入露骨成人內容；只談非露骨的交友、感情、界線與安全，不得延展成人情境",
        )
    elif mode == "restricted":
        adult_options = tuple(
            f"成人純文字策略：限制；{item}；只被動簡短承接；{common_floor}"
            for item in (
                "輕度曖昧與暗示可簡短承接，避免器官或性行為細節",
                "以留白和非圖像化用詞回應，不主動升級尺度",
            )
        )
    elif mode == "general":
        adult_options = tuple(
            f"成人純文字策略：一般；{item}；中等細節，依既有上下文最多延展一步；{common_floor}"
            for item in _ADULT_STYLES
        )
    else:
        adult_options = tuple(
            f"成人純文字策略：寬鬆；{item}；較高細節，依既有上下文最多自然延展兩步；{common_floor}"
            for item in _ADULT_STYLES
        )
    archetypes = _GROUP_ARCHETYPES_BY_STAGE[stage]

    def render_candidate(parts: tuple[str, str, str, str, str, str]) -> str:
        temperament, rhythm, interest, archetype, adult_style, gender_detail = parts
        return "；".join(
            (
                f"性格：{temperament}",
                f"聊天節奏：{rhythm}",
                f"興趣方向：{interest}",
                f"群聊原型：{archetype}",
                _STAGE_STANCE[stage],
                _GENDER_TONE[gender],
                gender_detail,
                _TAIWAN_CHAT_STYLE,
                _GROUP_CHAT_CALIBRATION,
                adult_style,
                "保持一般群友視角，不捏造真人經歷",
            )
        ) + "。"

    pools = (
        _TEMPERAMENTS,
        _RHYTHMS,
        _INTERESTS,
        archetypes,
        adult_options,
        _GENDER_DETAILS[gender],
    )
    chooser = secrets.SystemRandom()
    excluded = _excluded_styles(exclude)
    for _ in range(max(32, min(256, len(excluded) * 4 + 8))):
        candidate = render_candidate(tuple(chooser.choice(pool) for pool in pools))
        if candidate not in excluded:
            return candidate

    candidates = [render_candidate(parts) for parts in itertools.product(*pools)]
    available = [candidate for candidate in candidates if candidate not in excluded]
    return chooser.choice(available or candidates)


def generate_account_profile(
    *,
    exclude_style: str | Collection[str] = "",
    role_candidates: Collection[tuple[str, str]] = (),
    gender: str | None = None,
) -> dict[str, object]:
    """Generate a complete, safe-to-preview account settings preset."""
    chooser = secrets.SystemRandom()
    if gender is not None and gender not in _GENDER_TONE:
        raise ValueError("gender must be male or female")
    roles = tuple(role_candidates) or tuple(
        itertools.product(_GENDER_TONE, _STAGE_STANCE)
    )
    if gender is not None:
        roles = tuple(role for role in roles if role[0] == gender)
        if not roles:
            roles = tuple((gender, stage) for stage in _STAGE_STANCE)
    if any(
        gender not in _GENDER_TONE or stage not in _STAGE_STANCE
        for gender, stage in roles
    ):
        raise ValueError("role_candidates contains an unsupported role")
    gender, stage = chooser.choice(roles)
    adult_text_mode = chooser.choice(_ADULT_TEXT_MODE_WEIGHTS)
    adult_text_enabled = adult_text_enabled_for_mode(adult_text_mode)
    style = generate_persona(
        gender,
        stage,
        adult_text_mode,
        exclude=exclude_style,
    )
    task_pool = (
        _ADULT_TASKS
        if adult_text_mode in {"general", "lenient"}
        else _RESTRICTED_TASKS
        if adult_text_mode == "restricted"
        else _TASKS
    )
    task_name, task_info = chooser.choice(task_pool)
    stage_label = "老成員" if stage == "old_member" else "觀望"
    profile: dict[str, object] = {
        "label": f"{chooser.choice(_PROFILE_LABELS[gender])}｜{stage_label}",
        "gender": gender,
        "stage": stage,
        "style": style,
        "task_name": task_name,
        "task_info": task_info,
        "ai_model": chooser.choice(_TEXT_MODELS),
        "adult_text_mode": adult_text_mode,
        "adult_text_enabled": adult_text_enabled,
    }
    profile.update(dict(chooser.choice(_BEHAVIOR_PRESETS)))
    return profile
