"""Application logging configuration."""

import logging
import os


def setup_logging():
    """Configure root logger from environment."""
    raw_level = os.getenv("KLIPPERLCD_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw_level, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Keep transport/library internals out of app debug logs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    if not hasattr(logging, raw_level):
        logger.warning(
            "Invalid KLIPPERLCD_LOG_LEVEL=%r, fallback to INFO",
            raw_level,
        )
    logger.info("Logging initialized with level %s", logging.getLevelName(level))
