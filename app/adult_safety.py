from __future__ import annotations

from typing import Final


class AdultTextModePolicy(dict[str, object]):
    @property
    def adult_vocabulary_threshold(self) -> int:
        return int(self["adult_vocabulary_level"])

    @property
    def reply_detail_threshold(self) -> int:
        return int(self["reply_detail_level"])

    @property
    def topic_extension_threshold(self) -> int:
        return int(self["topic_extension_level"])


ADULT_TEXT_MODES: Final[tuple[str, ...]] = (
    "lenient",
    "general",
    "restricted",
    "strict",
)
ADULT_TEXT_MODE_LABELS: Final[dict[str, str]] = {
    "lenient": "寬鬆",
    "general": "一般",
    "restricted": "限制",
    "strict": "嚴格",
}
DEFAULT_ADULT_TEXT_MODE: Final[str] = "lenient"
LEGACY_ENABLED_ADULT_TEXT_MODE: Final[str] = "general"
_UNSET = object()


FIXED_ADULT_TEXT_SAFETY_POLICY = ""


FIXED_ADULT_TEXT_BLOCKED_TERMS = ()


FIXED_ADULT_TEXT_BLOCKED_TOPICS = ()


_POLICY_SPECS: Final[dict[str, dict[str, object]]] = {
    "strict": {
        "adult_vocabulary_level": 0,
        "reply_detail_level": 0,
        "topic_extension_level": 0,
        "max_extension_steps": 0,
        "allows_explicit_terms": False,
        "allows_adult_roleplay": False,
        "continuation_rule": "只可非露骨談交友、感情、界線與安全；不得延展成人情境。",
    },
    "restricted": {
        "adult_vocabulary_level": 1,
        "reply_detail_level": 1,
        "topic_extension_level": 1,
        "max_extension_steps": 0,
        "allows_explicit_terms": False,
        "allows_adult_roleplay": False,
        "continuation_rule": "只允許輕度曖昧、暗示與非圖像化用詞；只被動簡短承接，不主動升級或延展。",
    },
    "general": {
        "adult_vocabulary_level": 2,
        "reply_detail_level": 2,
        "topic_extension_level": 2,
        "max_extension_steps": 1,
        "allows_explicit_terms": True,
        "allows_adult_roleplay": True,
        "continuation_rule": "成年人、自願情境可用直接成人詞彙與中等細節；依既有上下文最多延展一步，不可突然升級。",
    },
    "lenient": {
        "adult_vocabulary_level": 3,
        "reply_detail_level": 3,
        "topic_extension_level": 3,
        "max_extension_steps": 999,
        "allows_explicit_terms": True,
        "allows_adult_roleplay": True,
        "continuation_rule": "成年人、自願、純文字虛構情境可用更直接詞彙與較高細節；在既有成人上下文可無限延展，不可從普通話題突然升級。",
    },
}


def clean_adult_text_mode(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("adult_text_mode must be a string")
    mode = value.strip().lower()
    if mode not in ADULT_TEXT_MODES:
        allowed = ", ".join(ADULT_TEXT_MODES)
        raise ValueError(f"adult_text_mode must be one of: {allowed}")
    return mode


def adult_text_enabled_for_mode(mode: object) -> bool:
    return clean_adult_text_mode(mode) != "strict"


def adult_text_mode_from_legacy(value: object) -> str:
    if not isinstance(value, bool):
        raise ValueError("adult_text_enabled must be boolean")
    return LEGACY_ENABLED_ADULT_TEXT_MODE if value else "strict"


def resolve_adult_text_mode(
    mode: object = _UNSET,
    *,
    adult_text_mode: object = _UNSET,
    adult_text_enabled: object = _UNSET,
    default: str = DEFAULT_ADULT_TEXT_MODE,
) -> str:
    if mode is not _UNSET and adult_text_mode is not _UNSET:
        raise ValueError("adult_text_mode was provided more than once")
    if adult_text_mode is not _UNSET:
        mode = adult_text_mode
    mode_present = mode is not _UNSET
    legacy_present = adult_text_enabled is not _UNSET
    if not mode_present and not legacy_present:
        return clean_adult_text_mode(default)

    resolved = clean_adult_text_mode(mode) if mode_present else None
    if legacy_present:
        legacy_mode = adult_text_mode_from_legacy(adult_text_enabled)
        if resolved is None:
            return legacy_mode
        if adult_text_enabled_for_mode(resolved) is not adult_text_enabled:
            raise ValueError("adult_text_mode conflicts with adult_text_enabled")
    assert resolved is not None
    return resolved


def adult_text_mode_policy(mode: object) -> AdultTextModePolicy:
    if isinstance(mode, bool):
        cleaned = adult_text_mode_from_legacy(mode)
    else:
        cleaned = clean_adult_text_mode(mode)
    policy = AdultTextModePolicy(_POLICY_SPECS[cleaned])
    policy.update(
        {
            "mode": cleaned,
            "label": ADULT_TEXT_MODE_LABELS[cleaned],
            "may_initiate_adult_topic": False,
            "media_scope": "telegram_text_only",
            "hard_safety_floor": list(FIXED_ADULT_TEXT_BLOCKED_TOPICS),
        }
    )
    return policy


def adult_text_mode_contract(mode: object) -> str:
    policy = adult_text_mode_policy(mode)
    cleaned = str(policy["mode"])
    legacy_note = ""
    if cleaned != "strict":
        legacy_note = (
            " 此級只適用於管理員確認的 18+ 群組；參與者預設為成年且自願，"
            "不必每句重複年齡或同意。"
        )
    if cleaned == "general":
        legacy_note += " 此級亦為舊 adult_text_enabled=true 的相容映射。"
    elif cleaned == "strict":
        legacy_note = " 成人純文字模式未開啟，不得產生露骨色情文字或色情角色扮演。"
    return (
        f"adult_text_mode={cleaned}; "
        f"adult_vocabulary_threshold={policy['adult_vocabulary_level']}; "
        f"reply_detail_threshold={policy['reply_detail_level']}; "
        f"topic_extension_threshold={policy['topic_extension_level']}; "
        f"max_extension_steps={policy['max_extension_steps']}。"
        f"成人純文字策略：{policy['label']}（{cleaned}）。"
        f"成人詞彙等級 {policy['adult_vocabulary_level']}/3；"
        f"回覆細節等級 {policy['reply_detail_level']}/3；"
        f"話題延展等級 {policy['topic_extension_level']}/3。"
        f"{policy['continuation_rule']}"
        f"{legacy_note}"
        " 僅適用 Telegram 純文字，不適用圖片、語音或影片。"
    )


__all__ = [
    "ADULT_TEXT_MODE_LABELS",
    "AdultTextModePolicy",
    "ADULT_TEXT_MODES",
    "DEFAULT_ADULT_TEXT_MODE",
    "FIXED_ADULT_TEXT_BLOCKED_TERMS",
    "FIXED_ADULT_TEXT_BLOCKED_TOPICS",
    "FIXED_ADULT_TEXT_SAFETY_POLICY",
    "LEGACY_ENABLED_ADULT_TEXT_MODE",
    "adult_text_enabled_for_mode",
    "adult_text_mode_contract",
    "adult_text_mode_from_legacy",
    "adult_text_mode_policy",
    "clean_adult_text_mode",
    "resolve_adult_text_mode",
]
