from __future__ import annotations

import unittest

from tests.test_memory import account_record


class AccountAdultModeTests(unittest.TestCase):
    def test_record_forces_lenient_regardless_of_config(self) -> None:
        # 尺度全开：无论 legacy bool 或 dashboard mode 为何，一律强制 lenient。
        legacy = account_record().with_updates(adult_text_enabled=True)
        self.assertEqual(legacy.adult_text_mode, "lenient")
        self.assertTrue(legacy.adult_text_enabled)
        self.assertEqual(legacy.public_dict()["adult_text_mode"], "lenient")

        restricted = account_record().with_updates(adult_text_mode="restricted")
        self.assertEqual(restricted.adult_text_mode, "lenient")
        self.assertTrue(restricted.adult_text_enabled)

        strict = account_record().with_updates(adult_text_mode="strict")
        self.assertEqual(strict.adult_text_mode, "lenient")
        self.assertTrue(strict.adult_text_enabled)

    def test_record_accepts_any_mode_without_conflict_error(self) -> None:
        # 尺度全开后不再校验 mode 与 legacy bool 的冲突。
        record = account_record().with_updates(
            adult_text_mode="strict",
            adult_text_enabled=True,
        )
        self.assertEqual(record.adult_text_mode, "lenient")
        self.assertTrue(record.adult_text_enabled)


if __name__ == "__main__":
    unittest.main()
