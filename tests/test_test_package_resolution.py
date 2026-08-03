from __future__ import annotations

import unittest
from pathlib import Path

import tests


class LocalTestsPackageResolutionTests(unittest.TestCase):
    def test_tests_import_resolves_to_repository_package(self) -> None:
        self.assertIsNotNone(tests.__file__)
        self.assertEqual(
            Path(tests.__file__).resolve().parent,
            Path(__file__).resolve().parent,
        )


if __name__ == "__main__":
    unittest.main()
