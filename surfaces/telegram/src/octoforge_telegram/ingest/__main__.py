"""The Telegram ingestion node: reads updates, submits them, and nothing else.

Run with ``python -m octoforge_telegram.ingest``.

It exists because of one Bot API fact: a token may be long-polled by exactly
one process. The moment the service runs on more than one pod, ingestion has
to live outside them or they steal each other's updates — so it moves out,
and the pods keep only the rendering half of the surface.

What it carries is deliberately small: the Bot API client, the invite gate
with its own database, and the clients that describe an image or transcribe a
recording at ingestion. No dialogs, no model, no embeddings — those live
behind the API it posts to.

Where a message lands is the balancer's decision: this process posts to one
address with the account id in a header, and affinity puts it on whichever pod
owns that user.
"""

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

import httpx
from octoforge_core.db.engine import create_engine, create_session_factory
from octoforge_server.config import Settings

from octoforge_telegram.client import TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.gateway import ApiGatewayRegistry, basic_auth_header
from octoforge_telegram.invites.store import SqlAlchemyInviteStore, SqlAlchemyMemberDirectory
from octoforge_telegram.poller import TelegramMembership, TelegramPoller, TelegramPollerOptions
from octoforge_telegram.schema import TelegramSurfaceBase

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
NO_TOKEN_MESSAGE = "OF_TELEGRAM_BOT_TOKEN is required to run the Telegram ingestion node"
NO_SERVICE_URL_MESSAGE = (
    "OF_TELEGRAM_SERVICE_URL is required: the ingestion node has no dialogs of its own, "
    "it posts them to the service"
)
UP_MESSAGE = "Telegram ingestion is up; send SIGINT/SIGTERM to stop"
DOWN_MESSAGE = "Telegram ingestion stopped"


async def run_ingest(settings: Settings, telegram: TelegramSettings) -> None:
    """Poll for updates and post them to the service until SIGINT or SIGTERM."""
    if not telegram.telegram_bot_token:
        raise SystemExit(NO_TOKEN_MESSAGE)
    if not telegram.telegram_service_url:
        raise SystemExit(NO_SERVICE_URL_MESSAGE)
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


async def _build(
    stack: AsyncExitStack, settings: Settings, telegram: TelegramSettings
) -> TelegramPoller:
    """Assemble the node: a bot client, the gate, and a way into the service."""
    outbound = await stack.enter_async_context(httpx.AsyncClient())
    service = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=telegram.telegram_service_url,
            headers=basic_auth_header(settings.service_username, settings.service_password_hash),
        )
    )
    engine = create_engine(telegram.telegram_database_url)
    stack.push_async_callback(engine.dispose)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    session_factory = create_session_factory(engine)
    invites = SqlAlchemyInviteStore(
        session_factory, ttl_seconds=telegram.telegram_invite_ttl_seconds
    )
    membership = (
        TelegramMembership(invites, telegram.telegram_admin_ids)
        if telegram.telegram_admin_ids
        else None
    )
    return TelegramPoller(
        client=TelegramBotClient(http_client=outbound, token=telegram.telegram_bot_token),
        registry=ApiGatewayRegistry(service),
        options=TelegramPollerOptions(
            poll_timeout_seconds=telegram.telegram_poll_timeout_seconds,
            membership=membership,
            directory=SqlAlchemyMemberDirectory(session_factory),
            voice_max_seconds=telegram.voice_max_seconds,
        ),
    )


def main() -> None:
    """Console entry: configure logging and run the node."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # httpx logs full request URLs at INFO — and a Bot API URL carries the token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_ingest(Settings(), TelegramSettings()))


if __name__ == "__main__":
    main()
