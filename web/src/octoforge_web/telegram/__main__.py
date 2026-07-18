"""Standalone Telegram surface: the bot without the HTTP API (no listening port).

Run with ``python -m octoforge_web.telegram``. The process only makes outbound
connections (Bot API long poll, LLM/embeddings endpoints); nothing is bound or
announced on the host. The cron scheduler runs in-process, as in the web app.
"""

import asyncio
import logging
import signal

from octoforge_web.config import Settings
from octoforge_web.main import runtime

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
NO_TOKEN_MESSAGE = "OF_TELEGRAM_BOT_TOKEN is required to run the standalone Telegram surface"
UP_MESSAGE = "Telegram surface is up without the HTTP API; send SIGINT/SIGTERM to stop"
DOWN_MESSAGE = "Telegram surface stopped"


async def run_standalone(settings: Settings) -> None:
    """Run the Telegram adapter (and cron scheduler) until SIGINT or SIGTERM."""
    if not settings.telegram_bot_token:
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
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # httpx logs full request URLs at INFO — and Bot API URLs carry the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_standalone(Settings()))


if __name__ == "__main__":
    main()
