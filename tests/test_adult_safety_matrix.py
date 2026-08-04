from __future__ import annotations

import unittest
from pathlib import Path

from app.adult_safety import (
    ADULT_TEXT_MODE_LABELS,
    ADULT_TEXT_MODES,
    FIXED_ADULT_TEXT_BLOCKED_TERMS,
    FIXED_ADULT_TEXT_BLOCKED_TOPICS,
    FIXED_ADULT_TEXT_SAFETY_POLICY,
    adult_text_mode_contract,
    adult_text_mode_policy,
    clean_adult_text_mode,
    resolve_adult_text_mode,
)
from app.content_guard import ContentGuard


class AdultSafetyMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ContentGuard(
            FIXED_ADULT_TEXT_BLOCKED_TERMS,
            FIXED_ADULT_TEXT_BLOCKED_TOPICS,
        )

    def test_four_stable_modes_have_chinese_labels(self) -> None:
        self.assertEqual(
            ADULT_TEXT_MODES,
            ("lenient", "general", "restricted", "strict"),
        )
        self.assertEqual(
            ADULT_TEXT_MODE_LABELS,
            {
                "lenient": "寬鬆",
                "general": "一般",
                "restricted": "限制",
                "strict": "嚴格",
            },
        )

    def test_mode_cleaning_legacy_mapping_and_conflicts(self) -> None:
        for mode in ADULT_TEXT_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(clean_adult_text_mode(mode), mode)
        with self.assertRaises(ValueError):
            clean_adult_text_mode("enabled")
        self.assertEqual(
            resolve_adult_text_mode(adult_text_enabled=True),
            "general",
        )
        self.assertEqual(
            resolve_adult_text_mode(adult_text_enabled=False),
            "strict",
        )
        self.assertEqual(
            resolve_adult_text_mode("lenient", adult_text_enabled=True),
            "lenient",
        )
        with self.assertRaises(ValueError):
            resolve_adult_text_mode("restricted", adult_text_enabled=False)
        with self.assertRaises(ValueError):
            resolve_adult_text_mode("strict", adult_text_enabled=True)

    def test_each_mode_has_distinct_structured_thresholds(self) -> None:
        policies = {
            mode: adult_text_mode_policy(mode)
            for mode in ADULT_TEXT_MODES
        }
        self.assertEqual(
            [policies[mode]["adult_vocabulary_level"] for mode in reversed(ADULT_TEXT_MODES)],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [policies[mode]["reply_detail_level"] for mode in reversed(ADULT_TEXT_MODES)],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [policies[mode]["topic_extension_level"] for mode in reversed(ADULT_TEXT_MODES)],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [policies[mode].topic_extension_threshold for mode in reversed(ADULT_TEXT_MODES)],
            [0, 1, 2, 3],
        )
        self.assertEqual(policies["strict"]["max_extension_steps"], 0)
        self.assertEqual(policies["restricted"]["max_extension_steps"], 0)
        self.assertEqual(policies["general"]["max_extension_steps"], 1)
        self.assertEqual(policies["lenient"]["max_extension_steps"], 2)
        for policy in policies.values():
            self.assertFalse(policy["may_initiate_adult_topic"])
            self.assertEqual(policy["media_scope"], "telegram_text_only")
            self.assertTrue(policy["hard_safety_floor"])

    def test_each_mode_contract_names_its_thresholds_and_behavior(self) -> None:
        contracts = {
            mode: adult_text_mode_contract(mode)
            for mode in ADULT_TEXT_MODES
        }
        self.assertEqual(len(set(contracts.values())), 4)
        self.assertIn("成人詞彙等級 0/3", contracts["strict"])
        self.assertIn("不得延展成人情境", contracts["strict"])
        self.assertIn("成人詞彙等級 1/3", contracts["restricted"])
        self.assertIn("只被動簡短承接", contracts["restricted"])
        self.assertIn("成人詞彙等級 2/3", contracts["general"])
        self.assertIn("最多延展一步", contracts["general"])
        self.assertIn("成人詞彙等級 3/3", contracts["lenient"])
        self.assertIn("最多自然延展兩步", contracts["lenient"])
        for mode in ("restricted", "general", "lenient"):
            with self.subTest(mode=mode):
                self.assertIn("管理員確認的 18+ 群組", contracts[mode])

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

    def test_readme_documents_four_modes_and_legacy_migration(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        for value, label in (
            ("lenient", "寬鬆"),
            ("general", "一般"),
            ("restricted", "限制"),
            ("strict", "嚴格"),
        ):
            self.assertIn(f"`{value}`（{label}）", readme)
        self.assertIn("`adult_text_enabled=true` 映射為 `general`", readme)
        self.assertIn("`adult_text_enabled=false` 映射為 `strict`", readme)
        self.assertIn("SQLite schema version 9", readme)


if __name__ == "__main__":
    unittest.main()
