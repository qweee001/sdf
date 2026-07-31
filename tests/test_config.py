from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import load_settings


class MaxAccountsSettingsTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "TG_API_ID": "12345",
            "TG_API_HASH": "test-api-hash",
            "ACCOUNT_ENCRYPTION_KEY": "test-encryption-key",
            "DASHBOARD_PASSWORD": "test-password-123",
        }
        values.update(overrides)
        return values

    def test_max_accounts_defaults_to_twenty(self) -> None:
        with patch.dict(os.environ, self._environment(), clear=True):
            self.assertEqual(load_settings().max_accounts, 20)

    def test_max_accounts_can_be_overridden_by_railway_variable(self) -> None:
        with patch.dict(
            os.environ,
            self._environment(MAX_ACCOUNTS="7"),
            clear=True,
        ):
            self.assertEqual(load_settings().max_accounts, 7)

    def test_max_accounts_cannot_exceed_console_limit(self) -> None:
        with patch.dict(
            os.environ,
            self._environment(MAX_ACCOUNTS="21"),
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "at most 20"):
                load_settings()


if __name__ == "__main__":
    unittest.main()
