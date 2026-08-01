"""Tests for the standalone Telegram surface (no HTTP listener involved)."""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

import pytest
from octoforge_core.config import EmbeddingBackend
from octoforge_server.channels import WEB_CHANNEL
from octoforge_server.config import Settings
from octoforge_telegram.surface import _report_failure

from octoforge_deploy.main import runtime
from octoforge_deploy.telegram_only import NO_TOKEN_MESSAGE, run_standalone


def settings_for(tmp_path: Path, token: str = "") -> Settings:
    """Settings wired to a throwaway database; no external calls are made."""
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        telegram_bot_token=token,
        embedding_backend=EmbeddingBackend.OPENAI,
        embedding_api_key="",
    )


async def test_run_standalone_requires_token(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=NO_TOKEN_MESSAGE):
        await run_standalone(settings_for(tmp_path))


async def test_runtime_builds_without_fastapi(tmp_path: Path) -> None:
    """The shared composition root works with no FastAPI app and no bot token."""
    async with runtime(settings_for(tmp_path)) as rt:
        assert rt.conversation_manager is not None
        assert rt.channel == WEB_CHANNEL
        assert rt.cron_store is not None


# --- the telegram task's done-callback (supervisor-lite) --------------------


async def test_a_dying_surface_is_reported_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception escaping the Telegram surface task is logged loudly, not dropped."""

    async def explode() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(explode())
    with suppress(RuntimeError):
        await task  # let it finish (and record its exception) before inspecting it

    with caplog.at_level(logging.ERROR):
        _report_failure(task)

    assert any("telegram surface stopped" in record.message for record in caplog.records)


async def test_a_cancelled_surface_is_not_a_failure() -> None:
    """Normal shutdown (task.cancel() from `_stop_background_tasks`) is not a failure."""

    async def spin_forever() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(spin_forever())
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    _report_failure(task)  # must not raise (task.exception() would, on cancel)
