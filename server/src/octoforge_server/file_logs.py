"""Background queue and rotating file handler for process logs."""

import atexit
import logging
import logging.handlers
import queue
from dataclasses import dataclass
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
MEGABYTE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    process: str
    log_dir: str = ""
    max_mb: int = 200
    backups: int = 9
    level: int = logging.INFO


class FileLogWriter:
    def __init__(self) -> None:
        self._listener: logging.handlers.QueueListener | None = None

    def start(self, handler: logging.Handler) -> logging.handlers.QueueHandler:
        records: queue.Queue[logging.LogRecord] = queue.Queue()
        self._listener = logging.handlers.QueueListener(
            records,
            handler,
            respect_handler_level=True,
        )
        self._listener.start()
        return logging.handlers.QueueHandler(records)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


WRITER = FileLogWriter()


def flush_file_logs() -> None:
    WRITER.stop()


def has_file_handler(root: logging.Logger) -> bool:
    return any(isinstance(handler, logging.handlers.QueueHandler) for handler in root.handlers)


def file_handler(config: LoggingConfig) -> logging.Handler:
    directory = Path(config.log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        directory / f"{config.process}.log",
        maxBytes=max(1, config.max_mb) * MEGABYTE,
        backupCount=max(0, config.backups),
        encoding="utf-8",
    )
    rotating.setFormatter(logging.Formatter(LOG_FORMAT))
    atexit.register(flush_file_logs)
    return WRITER.start(rotating)
