from __future__ import annotations

import unittest

from tests.test_memory import account_record


class AccountAdultModeTests(unittest.TestCase):
    def test_record_canonicalizes_mode_and_legacy_bool(self) -> None:
        legacy = account_record().with_updates(adult_text_enabled=True)
        self.assertEqual(legacy.adult_text_mode, "general")
        self.assertTrue(legacy.adult_text_enabled)
        self.assertEqual(legacy.public_dict()["adult_text_mode"], "general")
        self.assertTrue(legacy.public_dict()["adult_text_enabled"])

        restricted = account_record().with_updates(adult_text_mode="restricted")
        self.assertEqual(restricted.adult_text_mode, "restricted")
        self.assertTrue(restricted.adult_text_enabled)

        strict = restricted.with_updates(adult_text_mode="strict")
        self.assertEqual(strict.adult_text_mode, "strict")
        self.assertFalse(strict.adult_text_enabled)

    def test_record_rejects_conflicting_mode_and_legacy_bool(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts"):
            account_record().with_updates(
                adult_text_mode="strict",
                adult_text_enabled=True,
            )


if __name__ == "__main__":
    unittest.main()
