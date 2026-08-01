from __future__ import annotations


FIXED_ADULT_TEXT_SAFETY_POLICY = """
The adult-text option applies only to text sent into the Telegram group. When
it is enabled for an administrator-confirmed 18+ allowed group, participants
are treated as adults and ordinary participation in adult banter or fictional
roleplay is treated as voluntary without repeatedly restating age or consent.
This conversational default never establishes consent to real-world contact,
private-material sharing, or a directed act, and any explicit contrary evidence
overrides it immediately. Never produce sexual content involving a minor, an
age-ambiguous person, coercion, incapacity, exploitation, trafficking, sexual
violence, blackmail, harassment, or an ignored/revoked refusal. Never expose
personal data, facilitate stalking or doxxing, or create/solicit non-consensual
intimate material. Never sexualize an identifiable real person, create sexual
deepfakes, or facilitate illegal activity. Consent must be specific and may be
withdrawn at any time for real-world contact, private-material sharing, or a
directed act. Outside an administrator-confirmed 18+ group, if age or consent
is unclear, keep the reply non-sexual.
""".strip()


# These high-confidence phrases provide a deterministic fail-closed layer at
# generation and send time. Broader/contextual cases are handled by the exact-
# verdict semantic audit in AccountWorker._output_policy_allows.
FIXED_ADULT_TEXT_BLOCKED_TERMS = (
    "未成年性愛",
    "兒童色情",
    "幼童裸照",
    "誘騙未成年",
    "迷姦",
    "強姦",
    "性侵情境",
    "人口販運",
    "性剝削",
    "偷拍性愛",
    "偷拍裸照",
    "報復色情",
    "裸照勒索",
    "色情深偽",
    "開盒住址",
    "child porn",
    "sexual minor",
    "underage sex",
    "rape fantasy",
    "hidden camera sex",
    "revenge porn",
    "sexual deepfake",
)


FIXED_ADULT_TEXT_BLOCKED_TOPICS = (
    "未成年人或年齡不明者的性內容",
    "非自願、脅迫、失去行為能力或撤回同意後的性內容",
    "性剝削、人口販運、勒索或非法活動",
    "未經同意的私密內容、偷拍、報復色情或性騷擾",
    "可識別真實人物的色情描寫、色情影像或性深偽",
    "跟蹤、開盒、住址電話等個人資料暴露",
)


def adult_text_mode_contract(enabled: bool) -> str:
    if enabled:
        return """
成人純文字模式已由管理員為此帳號明確開啟：
- 這代表管理員已確認此帳號的允許群組為 18+；群內成人聊天、玩笑或虛構情境中的參與者預設為成年且自願，不必每句重複年齡或同意。
- 明確出現未成年、拒絕、停止、不舒服、撤回同意、強迫、失去行為能力或其他相反證據時，上述預設立即失效並停止相關內容。
- 這項開關只適用於 Telegram 文字回覆，不授權生成成人圖片、語音或影片，也不能把虛構內容冒充為帳號的真實經歷。
- 群內成人聊天的預設自願不等於同意現實接觸、私下交易或分享私密素材；這些情況仍需明確同意。
""".strip()
    return """
成人純文字模式未開啟：可以自然討論成年人交友、感情、界線與安全，但不得產生露骨色情文字或成人情境角色扮演。
""".strip()


__all__ = [
    "FIXED_ADULT_TEXT_BLOCKED_TERMS",
    "FIXED_ADULT_TEXT_BLOCKED_TOPICS",
    "FIXED_ADULT_TEXT_SAFETY_POLICY",
    "adult_text_mode_contract",
]
