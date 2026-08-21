"""Standalone Telegram update ingestion process posting into the service."""

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from octoforge_server.config import Settings
from octoforge_server.logs import LoggingConfig, configure_logging

from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.ingest.setup import build as _build
from octoforge_telegram.ingest.setup import report_media as _report_media
from octoforge_telegram.ingest.setup import service_headers

__all__ = ["_build", "_report_media", "run_ingest", "service_headers"]

logger = logging.getLogger(__name__)

PROCESS_LOG_NAME = "ingest"
NO_TOKEN_MESSAGE = "OF_TELEGRAM_BOT_TOKEN is required to run the Telegram ingestion node"
NO_SERVICE_URL_MESSAGE = "OF_TELEGRAM_SERVICE_URL is required"
NO_CREDENTIAL_MESSAGE = "OF_SERVICE_USERNAME and OF_SERVICE_PASSWORD are required"
UP_MESSAGE = "Telegram ingestion is up; send SIGINT/SIGTERM to stop"
DOWN_MESSAGE = "Telegram ingestion stopped"


async def run_ingest(settings: Settings, telegram: TelegramSettings) -> None:
    if not telegram.telegram_bot_token:
        raise SystemExit(NO_TOKEN_MESSAGE)
    if not telegram.telegram_service_url:
        raise SystemExit(NO_SERVICE_URL_MESSAGE)
    if not (settings.service_username and settings.service_password):
        raise SystemExit(NO_CREDENTIAL_MESSAGE)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with AsyncExitStack() as stack:
        poller = await _build(stack, settings, telegram)
        task = asyncio.create_task(poller.run_forever())
        logger.info(UP_MESSAGE)
        await stop.wait()
        task.cancel()
    logger.info(DOWN_MESSAGE)


def main() -> None:
    settings = Settings()
    configure_logging(
        LoggingConfig(
            PROCESS_LOG_NAME,
            log_dir=settings.log_dir,
            max_mb=settings.log_max_mb,
            backups=settings.log_backups,
        )
    )
    asyncio.run(run_ingest(settings, TelegramSettings()))


if __name__ == "__main__":
    main()
