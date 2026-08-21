"""Configure stdout and optional process-specific rotating file logging."""

import logging

from octoforge_server.file_logs import (
    LOG_FORMAT,
    LoggingConfig,
    file_handler,
    flush_file_logs,
    has_file_handler,
)

HTTPX_LOGGER = "httpx"

__all__ = ["LoggingConfig", "configure_logging", "flush_file_logs"]


def configure_logging(config: LoggingConfig) -> None:
    """Configure logging idempotently and keep httpx request URLs below INFO."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=config.level, format=LOG_FORMAT)
    elif root.level > config.level or root.level == logging.NOTSET:
        root.setLevel(config.level)
    logging.getLogger(HTTPX_LOGGER).setLevel(logging.WARNING)
    if config.log_dir and not has_file_handler(root):
        root.addHandler(file_handler(config))
