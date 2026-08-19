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
from collections.abc import Callable
from contextlib import AsyncExitStack

import httpx
from octoforge_core.db.engine import create_engine, create_session_factory
from octoforge_server.config import Settings
from octoforge_server.logs import configure_logging
from octoforge_server.secret_links import SecretLinkService, secrets_link_builder

from octoforge_telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.gateway import ApiGatewayRegistry, ApiProfileMirror, basic_auth_header
from octoforge_telegram.invites.store import (
    SqlAlchemyInviteStore,
    SqlAlchemyMemberDirectory,
    SqlAlchemyReferralStore,
)
from octoforge_telegram.media_client import ApiMediaUnderstanding
from octoforge_telegram.poller import TelegramMembership, TelegramPoller, TelegramPollerOptions
from octoforge_telegram.schema import TelegramSurfaceBase

logger = logging.getLogger(__name__)

# names this process's own log file when OF_LOG_DIR is set
PROCESS_LOG_NAME = "ingest"
NO_TOKEN_MESSAGE = "OF_TELEGRAM_BOT_TOKEN is required to run the Telegram ingestion node"
NO_SERVICE_URL_MESSAGE = (
    "OF_TELEGRAM_SERVICE_URL is required: the ingestion node has no dialogs of its own, "
    "it posts them to the service"
)
NO_CREDENTIAL_MESSAGE = (
    "OF_SERVICE_USERNAME and OF_SERVICE_PASSWORD are required: the service refuses "
    "an unauthenticated relay, and it would refuse it silently — every message would "
    "be dropped with nothing but a 401 in this log. Note OF_SERVICE_PASSWORD, not the "
    "_HASH the pod verifies against."
)
# A rollout restarts the service and this node at once, so the startup probe
# has to outlast the service's boot; long enough for that, short enough that a
# genuinely absent service still shows up in the log while somebody is watching.
MEDIA_PROBE_ATTEMPTS = 6
MEDIA_PROBE_DELAY_SECONDS = 5.0
UP_MESSAGE = "Telegram ingestion is up; send SIGINT/SIGTERM to stop"
DOWN_MESSAGE = "Telegram ingestion stopped"


async def run_ingest(settings: Settings, telegram: TelegramSettings) -> None:
    """Poll for updates and post them to the service until SIGINT or SIGTERM."""
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


def service_headers(settings: Settings) -> dict[str, str]:
    """The credential this node presents to the service.

    Its own function so a test can hand the result to the real gate: the two
    halves of one credential live in different processes, and nothing else
    checks that they still fit. They did not — this node presented the *hash*,
    which the gate hashes again and refuses, and the only evidence was a 401
    the user never saw.
    """
    return basic_auth_header(settings.service_username, settings.service_password)


def _secrets_link(settings: Settings) -> Callable[[str], str] | None:
    """The /secrets URL factory, when this installation stores secrets at all.

    Ingestion can hand out this link without reaching the service: the token
    names the Telegram ACCOUNT and is signed with the installation's secrets
    key, so whichever pod the form lands on validates it from that key alone
    and resolves the person itself. This node could not do that resolution —
    it has no identity store, only its own invite database — which is exactly
    why the token names an account and not a person.

    Without the key nothing can store a secret anyway, and the bot says so
    rather than sending a link to a form that answers 503.
    """
    if not settings.secrets_key:
        return None
    return secrets_link_builder(settings, SecretLinkService(settings.secrets_key), TELEGRAM_CHANNEL)


async def _report_media(media: ApiMediaUnderstanding) -> None:
    """State what media understanding this node will actually get, at startup.

    Retried, because a rollout starts this node while the service is still
    booting: the first deploy of this probe logged "the service did not
    answer" against a service that answered fine two seconds later, and a
    one-shot report that is wrong for the rest of the process's life is worse
    than no report at all. Unreachable after all of them is worth a warning:
    every picture and recording falls back to text until the service answers.
    """
    for attempt in range(1, MEDIA_PROBE_ATTEMPTS + 1):
        capabilities = await media.capabilities()
        if capabilities is not None:
            break
        if attempt < MEDIA_PROBE_ATTEMPTS:
            await asyncio.sleep(MEDIA_PROBE_DELAY_SECONDS)
    else:
        capabilities = None
    if capabilities is None:
        logger.warning(
            "media understanding: the service did not answer in %.0fs — pictures "
            "and recordings will fall back to text until it does",
            MEDIA_PROBE_ATTEMPTS * MEDIA_PROBE_DELAY_SECONDS,
        )
        return
    logger.info(
        "media understanding (on the service): images %s, voice %s",
        "on" if capabilities.get("describes_images") else "off — images arrive as text",
        "on" if capabilities.get("transcribes_audio") else "off — recordings get the text notice",
    )


async def _build(
    stack: AsyncExitStack, settings: Settings, telegram: TelegramSettings
) -> TelegramPoller:
    """Assemble the node: a bot client, the gate, and a way into the service.

    Describing an image and transcribing a recording still happen at
    ingestion, but this process no longer does either itself: it holds no
    core database and cannot resolve an account into a person, so a model
    call made here could be neither checked against a plan nor ledgered
    (a free plan used paid vision for months this way). It asks the service,
    which owns both.

    The startup probe below is the descendant of two log lines that were
    written after the split's first deployment shipped without vision and
    speech clients: the poller degraded exactly as designed for an
    unconfigured feature — silently, to text-only — and images and voice
    "stopped working" for a day. This node still states what it is about to
    rely on; it just asks the service instead of reading its own settings.
    """
    outbound = await stack.enter_async_context(httpx.AsyncClient())
    service = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=telegram.telegram_service_url,
            headers=service_headers(settings),
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
    # referrals live in this database too, so /invite works here exactly as it
    # does in-process; the status gate does not, and runs on the service
    referrals = SqlAlchemyReferralStore(session_factory)
    membership = (
        TelegramMembership(
            invites,
            telegram.telegram_admin_ids,
            referrals=referrals,
            open_registration=telegram.telegram_open_registration,
        )
        if telegram.telegram_admin_ids
        else None
    )
    media = ApiMediaUnderstanding(service)
    await _report_media(media)
    return TelegramPoller(
        client=TelegramBotClient(http_client=outbound, token=telegram.telegram_bot_token),
        registry=ApiGatewayRegistry(service),
        options=TelegramPollerOptions(
            poll_timeout_seconds=telegram.telegram_poll_timeout_seconds,
            membership=membership,
            directory=SqlAlchemyMemberDirectory(session_factory),
            # this node cannot name a person itself, so the mirror posts the
            # profile to the service; without it everyone admitted through a
            # split deployment stayed a bare id in the console
            identities=ApiProfileMirror(service),
            media=media,
            voice_max_seconds=telegram.voice_max_seconds,
            secrets_link=_secrets_link(settings),
            referrals=referrals,
            bot_username=telegram.resolved_bot_username(),
        ),
    )


def main() -> None:
    """Console entry: configure logging and run the node."""
    settings = Settings()
    configure_logging(
        PROCESS_LOG_NAME,
        log_dir=settings.log_dir,
        max_mb=settings.log_max_mb,
        backups=settings.log_backups,
    )
    asyncio.run(run_ingest(settings, TelegramSettings()))


if __name__ == "__main__":
    main()
