from __future__ import annotations

import io
import logging
import unittest
from unittest.mock import patch

from app.main import configure_logging


class LoggingTests(unittest.TestCase):
    def test_info_uses_stdout_and_warnings_use_stderr(self) -> None:
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        original_level = root.level
        standard_output = io.StringIO()
        error_output = io.StringIO()
        try:
            with (
                patch("sys.stdout", standard_output),
                patch("sys.stderr", error_output),
            ):
                configure_logging(logging.INFO)
                logger = logging.getLogger("routing-test")
                logger.info("ordinary status")
                logger.warning("warning status")
                logger.error("error status")
                for handler in root.handlers:
                    handler.flush()

            self.assertIn("ordinary status", standard_output.getvalue())
            self.assertNotIn("warning status", standard_output.getvalue())
            self.assertIn("warning status", error_output.getvalue())
            self.assertIn("error status", error_output.getvalue())
            self.assertNotIn("ordinary status", error_output.getvalue())
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers.clear()
            root.handlers.extend(original_handlers)
            root.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
