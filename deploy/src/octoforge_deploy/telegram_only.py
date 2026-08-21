"""A deployment that runs the Telegram bot and no HTTP listener.

Run with ``python -m octoforge_deploy.telegram_only``. The process only makes
outbound connections (Bot API long poll, LLM/embeddings endpoints); nothing is
bound or announced on the host. The cron scheduler runs in-process, as in the
HTTP deployment.

It lives with the deployments rather than with the Telegram surface because
that is what it is: a choice about *which interfaces run*, assembled from the
same graph the HTTP one uses. A surface that assembled deployments would be
deciding what runs beside it.
"""

import asyncio
import logging
import signal

from octoforge_server.config import Settings
from octoforge_server.logs import LoggingConfig, configure_logging
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.main import runtime

logger = logging.getLogger(__name__)

# names this process's own log file when OF_LOG_DIR is set
PROCESS_LOG_NAME = "telegram"
NO_TOKEN_MESSAGE = "OF_TELEGRAM_BOT_TOKEN is required to run the standalone Telegram surface"
UP_MESSAGE = "Telegram surface is up without the HTTP API; send SIGINT/SIGTERM to stop"
DOWN_MESSAGE = "Telegram surface stopped"


async def run_standalone(settings: Settings) -> None:
    """Run the Telegram adapter (and cron scheduler) until SIGINT or SIGTERM."""
    if not TelegramSettings().telegram_bot_token:
        raise SystemExit(NO_TOKEN_MESSAGE)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with runtime(settings):
        logger.info(UP_MESSAGE)
        await stop.wait()
    logger.info(DOWN_MESSAGE)


def main() -> None:
    """Console entry: configure logging and run the standalone surface."""
    settings = Settings()
    configure_logging(
        LoggingConfig(
            PROCESS_LOG_NAME,
            log_dir=settings.log_dir,
            max_mb=settings.log_max_mb,
            backups=settings.log_backups,
        )
    )
    asyncio.run(run_standalone(settings))


if __name__ == "__main__":
    main()
