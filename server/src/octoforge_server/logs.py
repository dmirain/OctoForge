"""Logging setup shared by every entry point of a deployment.

Stdout stays the primary sink — it is what `docker logs` reads and what a
foreground run prints. What it is not is a *record*: a container's log lives
and dies with the container, so every deploy erased the history of whatever
happened before it (2026-08-04: a crash at 20:57 left nothing readable by
21:04, in the middle of diagnosing that very crash).

So a deployment may also name a directory, and each process keeps its own
rotating file there. Its own, deliberately: two processes rotating one file
race on the rename and lose lines. The budget is bounded by construction —
`log_max_mb` x (`log_backups` + 1) per process, 2 GB by default — because an
unbounded log on a nearly full disk is its own outage.

File writes go through a queue drained by a background thread. Rotation
renames files, and doing that on the event loop would stall every dialog in
the process for the duration of the rename.
"""

import atexit
import logging
import logging.handlers
import queue
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
HTTPX_LOGGER = "httpx"
MEGABYTE = 1024 * 1024


class _FileLogWriter:
    """The background drain of the file queue: one per process."""

    def __init__(self) -> None:
        self._listener: logging.handlers.QueueListener | None = None

    def start(self, handler: logging.Handler) -> logging.handlers.QueueHandler:
        """Begin draining a fresh queue into `handler`, and return its inlet."""
        records: queue.Queue[logging.LogRecord] = queue.Queue()
        self._listener = logging.handlers.QueueListener(
            records, handler, respect_handler_level=True
        )
        self._listener.start()
        return logging.handlers.QueueHandler(records)

    def stop(self) -> None:
        """Drain and stop; safe to call when nothing is running."""
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


_writer = _FileLogWriter()


def flush_file_logs() -> None:
    """Drain and stop the background writer; safe to call when none exists.

    Registered for process exit, because without it the last seconds before a
    shutdown — the interesting ones — are still sitting in the queue.
    """
    _writer.stop()


def configure_logging(
    process: str,
    log_dir: str = "",
    max_mb: int = 200,
    backups: int = 9,
    level: int = logging.INFO,
) -> None:
    """Send this process's logs to stdout, and to a rotating file when asked.

    `process` names the file (`app`, `ingest`, `telegram`): one deployment
    runs several of these against one mounted directory, and they must not
    write to the same file.

    Idempotent: a second call adds no second handler, so an entry point that
    configures logging and then builds an app that configures it again keeps
    one of each.

    httpx is pinned to WARNING here rather than at each call site: it logs
    full request URLs at INFO, and a Bot API URL carries the bot token —
    which would then sit in the logs of any process that also runs the bot.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)
    elif root.level > level or root.level == logging.NOTSET:
        # somebody else configured logging first (uvicorn does, and so does
        # pytest) and left the root above this level — every record we are
        # here to keep would be dropped before reaching a handler. Never the
        # other way round: a deliberately more verbose setting is left alone.
        root.setLevel(level)
    logging.getLogger(HTTPX_LOGGER).setLevel(logging.WARNING)
    if not log_dir or _has_file_handler(root):
        return
    root.addHandler(_file_handler(process, log_dir, max_mb, backups))


def _has_file_handler(root: logging.Logger) -> bool:
    return any(isinstance(handler, logging.handlers.QueueHandler) for handler in root.handlers)


def _file_handler(process: str, log_dir: str, max_mb: int, backups: int) -> logging.Handler:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        directory / f"{process}.log",
        maxBytes=max(1, max_mb) * MEGABYTE,
        backupCount=max(0, backups),
        encoding="utf-8",
    )
    rotating.setFormatter(logging.Formatter(LOG_FORMAT))
    atexit.register(flush_file_logs)
    # unbounded queue: dropping a log line to protect memory would lose
    # exactly the lines an incident needs, and the drain keeps up with any
    # rate a single process can produce
    return _writer.start(rotating)
