from __future__ import annotations

import unittest

from app.adult_safety import (
    FIXED_ADULT_TEXT_BLOCKED_TERMS,
    FIXED_ADULT_TEXT_BLOCKED_TOPICS,
    FIXED_ADULT_TEXT_SAFETY_POLICY,
    adult_text_mode_contract,
)
from app.content_guard import ContentGuard


class AdultSafetyMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ContentGuard(
            FIXED_ADULT_TEXT_BLOCKED_TERMS,
            FIXED_ADULT_TEXT_BLOCKED_TOPICS,
        )

    def test_consensual_adult_text_is_not_lexically_blocked(self) -> None:
        allowed = (
            "我們兩個都28歲，也都同意聊彼此的床上偏好。",
            "兩位30歲成年人自願用純文字聊親吻、撫摸和慾望。",
            "成年伴侶正在聊親密時喜歡主動還是被動。",
            "雙方都成年也願意，用直接一點的成人語氣接話。",
            "成年人之間想聊做愛時最在意的節奏和界線。",
            "成人群裡雙方都同意純文字調情。",
        )

        for value in allowed:
            with self.subTest(value=value):
                self.assertFalse(self.guard.screen(value).blocked)

    def test_high_confidence_harm_terms_are_lexically_blocked(self) -> None:
        blocked = (
            "這是未成年性愛內容",
            "內容涉及兒童色情",
            "用迷姦當成情境",
            "描述強姦",
            "分享偷拍裸照",
            "製作色情深偽",
            "提供 hidden camera sex 教學",
            "散布 revenge porn",
        )

        for value in blocked:
            with self.subTest(value=value):
                self.assertTrue(self.guard.screen(value).blocked)

    def test_enabled_contract_allows_adult_text_without_boilerplate(self) -> None:
        contract = adult_text_mode_contract(True)

        self.assertIn("18+", contract)
        self.assertIn("不必每句重複年齡或同意", contract)
        self.assertIn("預設為成年且自願", contract)
        self.assertIn("明確出現未成年", contract)

    def test_disabled_contract_keeps_explicit_text_off(self) -> None:
        contract = adult_text_mode_contract(False)

        self.assertIn("模式未開啟", contract)
        self.assertIn("不得產生露骨色情文字", contract)

    def test_fixed_policy_keeps_the_hard_safety_floor(self) -> None:
        policy = " ".join(FIXED_ADULT_TEXT_SAFETY_POLICY.split())
        for phrase in (
            "minor",
            "age-ambiguous",
            "coercion",
            "incapacity",
            "sexual deepfakes",
            "doxxing",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, policy)


if __name__ == "__main__":
    unittest.main()
