"""Tests for the rotating log file a deployment keeps beside stdout."""

import logging
from pathlib import Path

import pytest
from octoforge_server.logs import LoggingConfig, configure_logging, flush_file_logs


@pytest.fixture(autouse=True)
def clean_root_logger() -> object:
    """Each test starts from a bare root logger and leaves one behind."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    yield
    flush_file_logs()
    for handler in list(root.handlers):
        handler.close()
    root.handlers.clear()
    root.handlers.extend(saved)


def read_log(path: Path) -> str:
    flush_file_logs()  # the writer is a background thread; wait for it
    return path.read_text(encoding="utf-8")


def test_without_a_directory_nothing_is_written(tmp_path: Path) -> None:
    """The default stays what a container gives you: stdout only."""
    configure_logging(LoggingConfig("app"))

    logging.getLogger("test").info("hello")

    assert list(tmp_path.iterdir()) == []


def test_lines_reach_a_file_named_after_the_process(tmp_path: Path) -> None:
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path)))

    logging.getLogger("test").info("the record survives the container")

    assert read_log(tmp_path / "app.log").count("the record survives the container") == 1


def test_each_process_writes_its_own_file(tmp_path: Path) -> None:
    """One mounted directory, several processes: sharing a file would race
    on rotation and lose the lines an incident needs."""
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path)))
    logging.getLogger("test").info("from the app")
    flush_file_logs()
    logging.getLogger().handlers.clear()

    configure_logging(LoggingConfig("ingest", log_dir=str(tmp_path)))
    logging.getLogger("test").info("from ingestion")

    assert "from the app" in read_log(tmp_path / "app.log")
    assert "from ingestion" in read_log(tmp_path / "ingest.log")


def test_the_budget_is_bounded_by_rotation(tmp_path: Path) -> None:
    """An unbounded log on a nearly full disk is its own outage."""
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path), max_mb=1, backups=2))

    for index in range(20_000):
        logging.getLogger("test").info("filler line %d %s", index, "x" * 100)
    flush_file_logs()

    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == ["app.log", "app.log.1", "app.log.2"]  # never a fourth
    assert sum(path.stat().st_size for path in tmp_path.iterdir()) < 4 * 1024 * 1024


def test_configuring_twice_keeps_one_file_handler(tmp_path: Path) -> None:
    """create_app() may run after an entry point already configured logging."""
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path)))
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path)))

    logging.getLogger("test").info("once")

    assert read_log(tmp_path / "app.log").count("once") == 1


def test_httpx_is_pinned_below_info(tmp_path: Path) -> None:
    """httpx logs full URLs at INFO, and a Bot API URL carries the token."""
    configure_logging(LoggingConfig("app", log_dir=str(tmp_path)))

    assert logging.getLogger("httpx").level == logging.WARNING
