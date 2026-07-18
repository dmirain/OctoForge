"""Tests for the standalone Telegram surface (no HTTP listener involved)."""

from pathlib import Path

import pytest
from octoforge_core.config import EmbeddingBackend

from octoforge_web.config import Settings
from octoforge_web.main import WEB_CHANNEL, runtime
from octoforge_web.telegram.__main__ import NO_TOKEN_MESSAGE, run_standalone


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
