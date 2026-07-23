"""FastAPI application factory and composition root (shared by standalone surfaces)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Response, status
from fastapi.staticfiles import StaticFiles
from octoforge_core import (
    ConversationManager,
    DialogRepository,
    MessageRepository,
    SqlAlchemyTaskStore,
    bootstrap_schema,
    build_agent_loop,
    build_compactor,
    build_conversation_manager,
    build_cron_outcome_reporter,
    build_cron_scheduler,
    build_dataset_service,
    build_external_executor,
    build_instruction_service,
    build_llm_client,
    build_router,
    build_runner_config,
    build_tool_registry,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import PromptProvider, StaticPromptProvider
from octoforge_core.composition import RunnerOptions, ToolLimits, ToolServices, ToolStores
from octoforge_core.config import EmbeddingBackend, HttpRerankerConfig, RerankerConfig
from octoforge_core.context.compactor import CompactorConfig
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.cron.scheduler import CronSchedulerConfig
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.cron.waker import ManagerCronWaker
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.errors import LLMResponseError
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.registry import CORE_SYSTEM_SKILLS, sync_system_registry
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.llm.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from octoforge_core.llm.errors import LLMError
from octoforge_core.llm.http_reranker import HttpRerankerClient
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder
from octoforge_core.llm.reranker import CrossEncoderReranker, RerankerClient
from octoforge_core.memory.store import SqlAlchemyMemoryStore
from octoforge_core.net.external import ExternalCallAuth
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.serper import SerperSearchProvider
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_web.api.cron import router as cron_router
from octoforge_web.api.dialog import router as dialog_router
from octoforge_web.config import Settings
from octoforge_web.prompts import FilePromptProvider
from octoforge_web.system_skills import WEB_SYSTEM_SKILLS
from octoforge_web.telegram.admin import AdminAccess, AdminManageTool
from octoforge_web.telegram.bridge import RunnerProvider
from octoforge_web.telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_web.telegram.invites.models import InviteBase
from octoforge_web.telegram.invites.store import SqlAlchemyInviteStore
from octoforge_web.telegram.poller import (
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
)

STATIC_DIR = Path(__file__).parent / "static"
APP_TITLE = "OctoForge"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
HEALTH_STATUS = "ok"
READY_STATUS = "ready"
NOT_READY_STATUS = "not-ready"
WEB_CHANNEL = "web"
USER_ID_HEADER = "X-User-Id"
USER_ID_HEADER_VALUE_TEMPLATE = "{user_id}"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    """Assembled services shared by the HTTP app and standalone surfaces."""

    settings: Settings
    conversation_manager: ConversationManager
    channel: str
    cron_store: CronStore
    session_factory: async_sessionmaker[AsyncSession]


@asynccontextmanager
async def runtime(settings: Settings) -> AsyncIterator[Runtime]:
    """Build all services and background tasks; no HTTP listener is involved.

    Shared composition root: the FastAPI lifespan wraps it, standalone
    surfaces (the Telegram-only runner) use it directly. The object graph is
    assembled from the reusable builders of `octoforge_core.composition`;
    only the settings/transport specifics live here.
    """
    engine = create_engine(settings.database_url)
    await _bootstrap_schema(engine)
    session_factory = create_session_factory(engine)
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    task_store = SqlAlchemyTaskStore(session_factory)
    cron_store = SqlAlchemyCronStore(session_factory)
    invites = await _build_invite_store(settings)
    try:
        async with (
            httpx.AsyncClient(base_url=settings.llm_base_url) as llm_http,
            httpx.AsyncClient(base_url=settings.embedding_base_url) as embed_http,
            httpx.AsyncClient() as outbound_http,
        ):
            llm_client = build_llm_client(llm_http, settings.to_llm_config())
            embedder = _build_embedder(settings, embed_http)
            instructions = build_instruction_service(
                SqlAlchemyInstructionStore(session_factory),
                embedder,
                reranker=_build_reranker(settings, outbound_http),
                rerank_candidates=settings.reranker_candidates,
            )
            datasets = build_dataset_service(SqlAlchemyDatasetStore(session_factory), embedder)
            await _sync_system_skills(instructions, settings)
            # The app's own base URL is allowlisted so tool records can
            # target our loopback HTTP API (cron jobs) past the SSRF guard.
            guard = SsrfGuard(allowed_prefixes=(settings.self_base_url,))
            memory = SqlAlchemyMemoryStore(session_factory)
            summary_store = SqlAlchemySummaryStore(session_factory)
            registry = build_tool_registry(
                outbound_http,
                guard,
                stores=ToolStores(
                    tasks=task_store,
                    cron=cron_store,
                    memory=memory,
                    archive=summary_store,
                    summaries=summary_store,
                ),
                services=ToolServices(
                    instructions=instructions,
                    datasets=datasets,
                    executor=build_external_executor(
                        service=instructions,
                        http_client=outbound_http,
                        guard=guard,
                        auth_whitelist=_external_call_whitelist(settings),
                    ),
                    search_provider=_build_search_provider(settings, outbound_http),
                ),
                limits=_tool_limits(settings),
            )
            if invites is not None and settings.telegram_admin_ids:
                registry.register(
                    AdminManageTool(
                        invites[0],
                        cron_store,
                        messages,
                        dialogs,
                        AdminAccess(
                            admin_ids=frozenset(settings.telegram_admin_ids),
                            telegram=TelegramBotClient(
                                http_client=outbound_http, token=settings.telegram_bot_token
                            ),
                        ),
                    )
                )
            prompt_provider: PromptProvider = FilePromptProvider(
                files=settings.to_prompt_files(),
                fallback=StaticPromptProvider(),
            )
            manager = build_conversation_manager(
                config=build_runner_config(
                    build_agent_loop(
                        llm_client,
                        registry,
                        max_iterations=settings.agent_max_iterations,
                        stream_idle_timeout=settings.llm_stream_idle_timeout_seconds or None,
                    ),
                    prompt_provider,
                    build_router(
                        llm_client,
                        prompt_provider,
                        timeout_seconds=settings.router_timeout_seconds,
                    ),
                    build_compactor(
                        store=summary_store,
                        archive=summary_store,
                        llm=llm_client,
                        config=CompactorConfig(
                            hot_max_chars=settings.context_hot_max_chars,
                            compact_target_chars=settings.context_compact_target_chars,
                            model_context_tokens=settings.model_context_tokens,
                            context_buffer_tokens=settings.context_buffer_tokens,
                        ),
                    ),
                    options=RunnerOptions(
                        max_processes=settings.max_processes,
                        task_outcome_listener=build_cron_outcome_reporter(
                            cron_store,
                            retry_limit=settings.cron_retry_limit,
                            backoff_base_seconds=settings.cron_retry_backoff_seconds,
                        ),
                    ),
                ),
                dialogs=dialogs,
                messages=messages,
                tasks=task_store,
            )
            # Sweep before the scheduler and surfaces start: orphaned tasks
            # are failed (cron-tagged ones get a bounded retry) and persisted
            # results that never reached their dialog are redelivered.
            await manager.recover_interrupted()
            scheduler_task = _start_cron_scheduler(cron_store, manager, settings)
            telegram = _start_telegram(
                settings,
                manager.get_or_create_runner,
                dialogs,
                outbound_http,
                invites,
            )
            try:
                yield Runtime(
                    settings=settings,
                    conversation_manager=manager,
                    channel=WEB_CHANNEL,
                    cron_store=cron_store,
                    session_factory=session_factory,
                )
            finally:
                await _stop_background_tasks(scheduler_task, telegram)
                await manager.stop_all()
    finally:
        await engine.dispose()
        if invites is not None:
            await invites[1].dispose()


def _configure_logging() -> None:
    """Ensure application and core logs reach a handler (idempotent)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with all dependencies wired."""
    _configure_logging()
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with runtime(resolved_settings) as rt:
            app.state.settings = rt.settings
            app.state.conversation_manager = rt.conversation_manager
            app.state.channel = rt.channel
            app.state.cron_store = rt.cron_store
            app.state.session_factory = rt.session_factory
            yield

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": HEALTH_STATUS}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, str]:
        """Readiness probe: verify the database answers a trivial query."""
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        try:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.warning("readiness check failed: database unavailable", exc_info=True)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": NOT_READY_STATUS, "database": "down"}
        return {"status": READY_STATUS, "database": "ok"}

    app.include_router(dialog_router)
    app.include_router(cron_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def _bootstrap_schema(engine: AsyncEngine) -> None:
    """Migrate the schema to head; fall back to create_all if Alembic fails.

    A fresh or already-managed database is migrated to head. If migrations
    cannot run at all (misconfiguration, unexpected engine), the app still
    starts on a create_all schema rather than failing outright.
    """
    try:
        await bootstrap_schema(engine)
    except Exception:  # any migration failure must not block startup
        logger.warning("Alembic migration failed; falling back to create_all", exc_info=True)
        await init_db(engine)


async def _sync_system_skills(instructions: InstructionService, settings: Settings) -> None:
    """Sync the declarative system registry into the store when embeddings work.

    The sync needs working embeddings (every upsert recomputes one); when no
    usable backend is configured it is skipped so the app still starts
    (embeddings stay optional until the first skills_search/save call). A
    failing embeddings backend or database must not take the app down: log a
    warning and start without the sync (it retries on the next restart).
    """
    if not settings.embeddings_configured():
        return
    try:
        await sync_system_registry(instructions, CORE_SYSTEM_SKILLS + WEB_SYSTEM_SKILLS)
    except (LLMError, LLMResponseError, SQLAlchemyError):
        logger.warning(
            "System skill registry sync failed; starting without it",
            exc_info=True,
        )


def _build_embedder(settings: Settings, http_client: httpx.AsyncClient) -> EmbeddingClient:
    """Choose the embeddings backend: local sentence-transformers or HTTP."""
    if settings.embedding_backend == EmbeddingBackend.LOCAL:
        return SentenceTransformerEmbedder(
            model_name=settings.embedding_model,
            batch_size=settings.embedding_batch_size,
        )
    return OpenAIEmbeddingClient(
        http_client=http_client,
        config=settings.to_embedding_config(),
    )


def _build_reranker(settings: Settings, http_client: httpx.AsyncClient) -> RerankerClient | None:
    """Build the optional reranker: HTTP backend when an API key is set, else local."""
    if not settings.reranker_model:
        return None
    if settings.reranker_api_key:
        return HttpRerankerClient(
            http_client=http_client,
            config=HttpRerankerConfig(
                model=settings.reranker_model,
                api_key=settings.reranker_api_key,
                api_url=settings.reranker_api_url,
                timeout_seconds=settings.reranker_timeout_seconds,
            ),
        )
    return CrossEncoderReranker(RerankerConfig(model=settings.reranker_model))


def _build_search_provider(
    settings: Settings,
    outbound_http: httpx.AsyncClient,
) -> SearchProvider | None:
    """Build the default web-search provider when a serper token is configured."""
    if not settings.serper_token:
        return None
    return SerperSearchProvider(http_client=outbound_http, api_key=settings.serper_token)


def _tool_limits(settings: Settings) -> ToolLimits:
    """Map the settings' tool limit fields to the core ToolLimits bundle."""
    return ToolLimits(
        instructions_top_k=settings.instructions_top_k,
        datasets_query_default_limit=settings.datasets_query_default_limit,
        datasets_query_max_limit=settings.datasets_query_max_limit,
        memory_search_default_limit=settings.memory_search_default_limit,
        memory_search_max_limit=settings.memory_search_max_limit,
        history_search_default_limit=settings.history_search_default_limit,
        history_search_max_limit=settings.history_search_max_limit,
    )


def _external_call_whitelist(settings: Settings) -> tuple[ExternalCallAuth, ...]:
    """Env-configured entries plus the app's own API with the per-user header."""
    self_entry = ExternalCallAuth(
        base_url_prefix=settings.self_base_url,
        header_name=USER_ID_HEADER,
        header_value=USER_ID_HEADER_VALUE_TEMPLATE,
    )
    return (*settings.to_external_call_auth_whitelist(), self_entry)


def _start_cron_scheduler(
    store: CronStore,
    manager: ConversationManager,
    settings: Settings,
) -> asyncio.Task[None]:
    """Build the cron scheduler of this instance and start its poll loop."""
    scheduler = build_cron_scheduler(
        store,
        ManagerCronWaker(manager),
        owner=uuid.uuid4().hex,
        config=CronSchedulerConfig(
            poll_interval_seconds=settings.cron_poll_interval_seconds,
            lease_ttl_seconds=settings.cron_lease_ttl_seconds,
            replay_limit=settings.cron_replay_limit,
        ),
    )
    return asyncio.create_task(scheduler.run_forever())


async def _build_invite_store(
    settings: Settings,
) -> tuple[SqlAlchemyInviteStore, AsyncEngine] | None:
    """Build the invite store on its own database when Telegram is enabled.

    The invites schema is bootstrapped with a plain create_all: one small
    isolated table on its own Base/engine, no Alembic chain of its own.
    """
    if not settings.telegram_bot_token:
        return None
    engine = create_engine(settings.telegram_database_url)
    async with engine.begin() as connection:
        await connection.run_sync(InviteBase.metadata.create_all)
    store = SqlAlchemyInviteStore(
        create_session_factory(engine), ttl_seconds=settings.telegram_invite_ttl_seconds
    )
    return store, engine


def _start_telegram(
    settings: Settings,
    runner_provider: RunnerProvider,
    dialogs: DialogRepository,
    http_client: httpx.AsyncClient,
    invites: tuple[SqlAlchemyInviteStore, AsyncEngine] | None = None,
) -> tuple[TelegramBridgeRegistry, asyncio.Task[None]] | None:
    """Start the Telegram long-poll adapter when a bot token is configured.

    The membership gate activates only with admin ids configured: without
    admins there is nobody to issue invites, and gating would lock every
    existing user out (legacy open behavior is kept instead).
    """
    if not settings.telegram_bot_token:
        return None
    client = TelegramBotClient(http_client=http_client, token=settings.telegram_bot_token)
    registry = TelegramBridgeRegistry(
        runner_provider=runner_provider,
        client=client,
        edit_throttle_seconds=settings.telegram_edit_throttle_seconds,
        rich_messages_enabled=settings.telegram_rich_messages,
    )
    membership = None
    if invites is not None and settings.telegram_admin_ids:
        membership = TelegramMembership(invites[0], settings.telegram_admin_ids)
    poller = TelegramPoller(
        client=client,
        registry=registry,
        poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
        membership=membership,
    )
    return registry, asyncio.create_task(_run_telegram(poller, registry, dialogs))


async def _run_telegram(
    poller: TelegramPoller,
    registry: TelegramBridgeRegistry,
    dialogs: DialogRepository,
) -> None:
    """Warm bridges for known Telegram dialogs, then poll for updates."""
    user_ids = await dialogs.list_user_ids_by_channel(TELEGRAM_CHANNEL)
    await registry.warm(user_ids)
    await poller.run_forever()


async def _stop_background_tasks(
    scheduler_task: asyncio.Task[None],
    telegram: tuple[TelegramBridgeRegistry, asyncio.Task[None]] | None,
) -> None:
    """Stop the cron scheduler and the Telegram adapter, if it was started."""
    scheduler_task.cancel()
    with suppress(asyncio.CancelledError):
        await scheduler_task
    if telegram is not None:
        registry, poller_task = telegram
        poller_task.cancel()
        with suppress(asyncio.CancelledError):
            await poller_task
        await registry.aclose()


app = create_app()
