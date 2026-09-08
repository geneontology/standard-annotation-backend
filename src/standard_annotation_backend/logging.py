"""Logging configuration for SAB processes."""

import logging


def configure_logging() -> None:
    """Configure concise process-wide logging using the standard library."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
