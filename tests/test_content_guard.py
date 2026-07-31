from __future__ import annotations

import unittest

from app.content_guard import ContentGuard


class ContentGuardTests(unittest.TestCase):
    def assert_blocked(
        self,
        guard: ContentGuard,
        value: str,
        *,
        reason: str | None = None,
    ) -> None:
        result = guard.screen(value)
        self.assertTrue(result.blocked, value)
        if reason is not None:
            self.assertEqual(result.reason, reason)

    def test_nfkc_normalization_blocks_full_width_spelling(self) -> None:
        guard = ContentGuard(("secret",))

        self.assert_blocked(guard, "這是ＳＥＣＲＥＴ內容")

    def test_spacing_punctuation_and_zero_width_do_not_split_a_term(self) -> None:
        guard = ContentGuard(("secret",))

        self.assert_blocked(guard, "s e-c\u200br.e_t")

    def test_repeated_characters_do_not_hide_a_term(self) -> None:
        guard = ContentGuard(("secret",))

        self.assert_blocked(guard, "seeecreeet")

    def test_common_leet_substitutions_are_blocked(self) -> None:
        guard = ContentGuard(("secret",))

        self.assert_blocked(guard, "s3cr3t")

    def test_one_edit_and_adjacent_transposition_are_blocked(self) -> None:
        guard = ContentGuard(("secret",))

        for candidate in ("secrat", "secert"):
            with self.subTest(candidate=candidate):
                self.assert_blocked(guard, candidate, reason="approximate")

    def test_unrelated_ordinary_text_is_not_blocked(self) -> None:
        guard = ContentGuard(("secret",), ("限制主題",))

        for candidate in (
            "今天天氣很好，晚點想去散步。",
            "大家晚餐吃什麼？",
            "sunshine and coffee",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(guard.screen(candidate).blocked)

    def test_safe_context_masks_blocked_text_and_preserves_safe_text(self) -> None:
        guard = ContentGuard(("秘密計畫",), ("限制主題",))

        masked = guard.safe_context("有人提到秘密計畫")
        self.assertNotEqual(masked, "有人提到秘密計畫")
        self.assertIn("隱去", masked)
        self.assertEqual(guard.safe_context("今天聊電影"), "今天聊電影")

    def test_policy_digest_is_stable_and_policy_sensitive(self) -> None:
        first = ContentGuard(("秘密計畫",), ("限制主題",))
        same = ContentGuard(("秘密計畫",), ("限制主題",))
        reordered = ContentGuard(("另一項", "秘密計畫"), ("限制主題",))
        moved_kind = ContentGuard(("限制主題",), ("秘密計畫",))

        self.assertEqual(first.policy_digest, same.policy_digest)
        self.assertNotEqual(first.policy_digest, reordered.policy_digest)
        self.assertNotEqual(first.policy_digest, moved_kind.policy_digest)


if __name__ == "__main__":
    unittest.main()
