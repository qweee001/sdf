from __future__ import annotations

import itertools
import secrets
from collections.abc import Collection


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
)

_STAGE_STANCE = {
    "old_member": "熟悉群聊節奏但不代表社群，也不替任何人背書",
    "observer": "仍在觀望並保留自己的判斷，不假裝已有參與或見面經歷",
}

_GENDER_TONE = {
    "male": "維持男性角色的一致口吻",
    "female": "維持女性角色的一致口吻",
}

_TAIWAN_CHAT_STYLE = (
    "固定使用台灣繁體中文與台灣常用詞，語氣像台灣群組聊天，"
    "自然使用語助詞但不堆疊，避免簡體字、中國大陸用詞、翻譯腔與客服腔"
)

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
    adult_text_enabled: bool,
    *,
    exclude: str | Collection[str] = "",
) -> str:
    """Generate a stored, account-specific chat style without model calls."""
    if gender not in _GENDER_TONE:
        raise ValueError("gender must be male or female")
    if stage not in _STAGE_STANCE:
        raise ValueError("stage must be old_member or observer")

    adult_options: tuple[str, ...]
    if adult_text_enabled:
        adult_options = tuple(
            f"成人純文字取向：{item}；僅限成年、自願、尊重與隱私，"
            "拒絕即停止，不追問、不施壓"
            for item in _ADULT_STYLES
        )
    else:
        adult_options = (
            "聊天以日常、交友與感情話題為主，不主動帶入露骨成人內容",
        )

    candidates = [
        "；".join(
            (
                f"性格：{temperament}",
                f"聊天節奏：{rhythm}",
                f"興趣方向：{interest}",
                _STAGE_STANCE[stage],
                _GENDER_TONE[gender],
                _TAIWAN_CHAT_STYLE,
                adult_style,
                "保持一般群友視角，不捏造真人經歷",
            )
        )
        + "。"
        for temperament, rhythm, interest, adult_style in itertools.product(
            _TEMPERAMENTS,
            _RHYTHMS,
            _INTERESTS,
            adult_options,
        )
    ]
    excluded = _excluded_styles(exclude)
    available = [candidate for candidate in candidates if candidate not in excluded]
    if not available:
        available = candidates
    return secrets.SystemRandom().choice(available)


def generate_account_profile(
    *,
    exclude_style: str | Collection[str] = "",
    role_candidates: Collection[tuple[str, str]] = (),
) -> dict[str, object]:
    """Generate a complete, safe-to-preview account settings preset."""
    chooser = secrets.SystemRandom()
    roles = tuple(role_candidates) or tuple(
        itertools.product(_GENDER_TONE, _STAGE_STANCE)
    )
    if any(
        gender not in _GENDER_TONE or stage not in _STAGE_STANCE
        for gender, stage in roles
    ):
        raise ValueError("role_candidates contains an unsupported role")
    gender, stage = chooser.choice(roles)
    adult_text_enabled = chooser.choice((True, True, False))
    style = generate_persona(
        gender,
        stage,
        adult_text_enabled,
        exclude=exclude_style,
    )
    task_name, task_info = chooser.choice(
        _ADULT_TASKS if adult_text_enabled else _TASKS
    )
    stage_label = "老成員" if stage == "old_member" else "觀望"
    profile: dict[str, object] = {
        "label": f"{chooser.choice(_PROFILE_LABELS[gender])}｜{stage_label}",
        "gender": gender,
        "stage": stage,
        "style": style,
        "task_name": task_name,
        "task_info": task_info,
        "ai_model": chooser.choice(_TEXT_MODELS),
        "adult_text_enabled": adult_text_enabled,
    }
    profile.update(dict(chooser.choice(_BEHAVIOR_PRESETS)))
    return profile
