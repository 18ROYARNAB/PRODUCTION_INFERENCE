import logging
import re
import unittest
from unittest.mock import MagicMock, patch

from production_inference.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_configure_logging_adds_timestamp_and_logger_metadata(self) -> None:
        handler = MagicMock()
        root_logger = MagicMock()
        root_logger.handlers = [handler]
        record = logging.LogRecord(
            "test.module",
            logging.INFO,
            __file__,
            1,
            "operation completed",
            (),
            None,
        )

        with patch(
            "production_inference.logging_config.logging.getLogger",
            return_value=root_logger,
        ):
            configure_logging()

        formatter = handler.setFormatter.call_args.args[0]
        rendered = formatter.format(record)
        self.assertRegex(
            rendered,
            re.compile(
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
                r"INFO test\.module: operation completed$"
            ),
        )


if __name__ == "__main__":
    unittest.main()
