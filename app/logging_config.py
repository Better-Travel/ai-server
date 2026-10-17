"""Logging setup for the app (console, level from `LOG_LEVEL`)."""

import logging

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once; quiets chatty HTTP client libraries."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, force=True)
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
