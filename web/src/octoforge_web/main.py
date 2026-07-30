"""FastAPI application factory and composition root (shared by standalone surfaces)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from octoforge_core import (
    ConversationManager,
    DialogRepository,
    ExchangeRepository,
    SqlAlchemyTaskStore,
    bootstrap_schema,
    build_agent_loop,
    build_collecting_sweeper,
    build_compactor,
    build_conversation_manager,
    build_cron_outcome_reporter,
    build_cron_scheduler,
    build_dataset_service,
    build_dataset_store,
    build_external_executor,
    build_instruction_service,
    build_instruction_store,
    build_llm_client,
    build_router,
    build_runner_config,
    build_summary_store,
    build_tool_registry,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.admin.api import AdminReadModel
from octoforge_core.admin.store import SqlAlchemyAdminStore
from octoforge_core.agent.prompts import PromptProvider, StaticPromptProvider
from octoforge_core.composition import (
    LexicalBackend,
    RunnerOptions,
    ToolLimits,
    ToolServices,
    ToolStores,
)
from octoforge_core.config import EmbeddingBackend, HttpRerankerConfig, RerankerConfig
from octoforge_core.context.api import SummaryStore
from octoforge_core.context.compactor import CompactorConfig
from octoforge_core.cron.api import CronStore
from octoforge_core.cron.scheduler import CronSchedulerConfig
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.search_extensions import (
    PG_TEXTSEARCH,
    VECTOR,
    installed_search_extensions,
)
from octoforge_core.db.sqlite_fts import has_sqlite_fts
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.errors import LLMResponseError
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.registry import (
    CORE_SYSTEM_SKILLS,
    SystemSkill,
    sync_system_registry,
)
from octoforge_core.llm.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from octoforge_core.llm.errors import LLMError
from octoforge_core.llm.http_reranker import HttpRerankerClient
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder
from octoforge_core.llm.reranker import CrossEncoderReranker, RerankerClient
from octoforge_core.net.external import CallCredentials, ExternalCallAuth
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.serper import SerperSearchProvider
from octoforge_core.secrets.api import SecretStore
from octoforge_core.secrets.store import SqlAlchemySecretStore
from octoforge_core.speech.api import TranscriptionClient
from octoforge_core.speech.client import OpenAITranscriptionClient
from octoforge_core.tasks.store import TaskStore
from octoforge_core.vision.api import ImageResolver, VisionClient
from octoforge_core.vision.client import OpenAIVisionClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_web.api.admin import router as admin_router
from octoforge_web.api.cron import router as cron_router
from octoforge_web.api.dialog import router as dialog_router
from octoforge_web.api.secrets import router as secrets_router
from octoforge_web.auth import CROSS_SITE_MESSAGE, AuthGate, is_cross_site_mutation, is_open_path
from octoforge_web.capabilities import log_capabilities
from octoforge_web.config import Settings
from octoforge_web.prompts import FilePromptProvider
from octoforge_web.secret_links import SecretLinkService
from octoforge_web.skill_overlay import apply_overlay, load_overlay
from octoforge_web.system_skills import WEB_SYSTEM_SKILLS
from octoforge_web.telegram.admin import AdminAccess, AdminManageTool, AdminStores
from octoforge_web.telegram.bridge import RunnerProvider
from octoforge_web.telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_web.telegram.images import TelegramImageResolver
from octoforge_web.telegram.invites.api import InviteStore, MemberDirectory
from octoforge_web.telegram.invites.models import InviteBase
from octoforge_web.telegram.invites.store import SqlAlchemyInviteStore, SqlAlchemyMemberDirectory
from octoforge_web.telegram.poller import (
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
)

STATIC_DIR = Path(__file__).parent / "static"
APP_TITLE = "OctoForge"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
HEALTH_STATUS = "ok"
READY_STATUS = "ready"
NOT_READY_STATUS = "not-ready"
WEB_CHANNEL = "web"
HTTPX_LOGGER = "httpx"
USER_ID_HEADER = "X-User-Id"
USER_ID_HEADER_VALUE_TEMPLATE = "{user_id}"

logger = logging.getLogger(__name__)

# Signature of the next handler in Starlette's middleware chain.
NextCall = Callable[[Request], Awaitable[Response]]


@dataclass(slots=True)
class Runtime:
    """Assembled services shared by the HTTP app and standalone surfaces."""

    settings: Settings
    conversation_manager: ConversationManager
    channel: str
    cron_store: CronStore
    session_factory: async_sessionmaker[AsyncSession]
    task_store: TaskStore
    instructions: InstructionService
    admin_read_model: AdminReadModel
    secret_store: SecretStore | None
    secret_links: SecretLinkService
    dialogs: DialogRepository
    summary_store: SummaryStore
    exchanges: ExchangeRepository
    # Telegram who-is-who (None: bot not configured) — read by the operator
    # console to decorate user ids with names and invite attribution
    telegram_members: MemberDirectory | None = None
    telegram_invites: InviteStore | None = None


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
    # after the bootstrap: the schema step is what creates the extensions, so
    # probing earlier would report a gap the next line has already filled
    search_extensions = await _probe_search_extensions(engine)
    lexical_backend = await _probe_lexical_backend(engine, search_extensions)
    log_capabilities(settings, logger, search_extensions, lexical_backend)
    session_factory = create_session_factory(engine)
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    task_store = SqlAlchemyTaskStore(session_factory)
    cron_store = SqlAlchemyCronStore(session_factory)
    secret_store = _build_secret_store(settings, session_factory)
    secret_links = SecretLinkService()
    telegram_stores = await _build_telegram_stores(settings)
    try:
        async with (
            httpx.AsyncClient(base_url=settings.llm_base_url) as llm_http,
            httpx.AsyncClient(base_url=settings.resolved_embedding_base_url()) as embed_http,
            httpx.AsyncClient(base_url=settings.resolved_vision_base_url()) as vision_http,
            httpx.AsyncClient(base_url=settings.stt_base_url) as speech_http,
            httpx.AsyncClient() as outbound_http,
        ):
            llm_client = build_llm_client(llm_http, settings.to_llm_config())
            embedder = _build_embedder(settings, embed_http)
            vision_client = _build_vision_client(settings, vision_http)
            deep_vision_client = _build_deep_vision_client(settings, vision_http)
            speech_client = _build_speech_client(settings, speech_http)
            image_resolver = _build_telegram_image_resolver(settings, outbound_http)
            instructions = build_instruction_service(
                build_instruction_store(session_factory, vector_search=VECTOR in search_extensions),
                embedder,
                reranker=_build_reranker(settings, outbound_http),
                rerank_candidates=settings.reranker_candidates,
                embedding_model=settings.embedding_model,
            )
            datasets = build_dataset_service(
                build_dataset_store(session_factory, lexical_search=lexical_backend),
                embedder,
            )
            await _sync_system_skills(instructions, settings)
            # The app's own base URL is allowlisted so tool records can
            # target our loopback HTTP API (cron jobs) past the SSRF guard.
            guard = SsrfGuard(allowed_prefixes=(settings.self_base_url,))
            summary_store = build_summary_store(session_factory, lexical_search=lexical_backend)
            registry = build_tool_registry(
                outbound_http,
                guard,
                stores=ToolStores(
                    tasks=task_store,
                    cron=cron_store,
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
                        credentials=CallCredentials(
                            auth_whitelist=_external_call_whitelist(settings),
                            secrets=secret_store,
                        ),
                    ),
                    search_provider=_build_search_provider(settings, outbound_http),
                ),
                limits=_tool_limits(settings),
            )
            if telegram_stores is not None and settings.telegram_admin_ids:
                registry.register(
                    AdminManageTool(
                        AdminStores(
                            invites=telegram_stores.invites,
                            cron_store=cron_store,
                            messages=messages,
                            dialogs=dialogs,
                            instructions=instructions,
                            directory=telegram_stores.directory,
                        ),
                        AdminAccess(
                            admin_ids=frozenset(settings.telegram_admin_ids),
                            telegram=TelegramBotClient(
                                http_client=outbound_http, token=settings.telegram_bot_token
                            ),
                            bot_username=settings.resolved_telegram_bot_username(),
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
                        tool_timeout=settings.agent_tool_timeout_seconds,
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
                        task_outcome_listener=build_cron_outcome_reporter(cron_store),
                        vision=deep_vision_client,
                        image_resolver=image_resolver,
                    ),
                ),
                dialogs=dialogs,
                messages=messages,
                tasks=task_store,
                exchanges=exchanges,
            )
            # Sweep before the scheduler and surfaces start: orphaned tasks
            # are restarted as background processes and persisted results
            # that never reached their dialog are redelivered.
            await manager.recover_interrupted()
            scheduler_task = _start_cron_scheduler(cron_store, manager, settings)
            sweeper_task = _start_collecting_sweeper(exchanges, dialogs, manager, settings)
            telegram = _start_telegram(
                settings,
                manager.get_or_create_runner,
                dialogs,
                outbound_http,
                _TelegramExtras(
                    stores=telegram_stores,
                    secrets_link=(
                        _secrets_link_builder(settings, secret_links)
                        if secret_store is not None
                        else None
                    ),
                    vision=vision_client,
                    speech=speech_client,
                ),
            )
            try:
                yield Runtime(
                    settings=settings,
                    conversation_manager=manager,
                    channel=WEB_CHANNEL,
                    cron_store=cron_store,
                    session_factory=session_factory,
                    task_store=task_store,
                    instructions=instructions,
                    admin_read_model=SqlAlchemyAdminStore(session_factory),
                    secret_store=secret_store,
                    secret_links=secret_links,
                    dialogs=dialogs,
                    summary_store=summary_store,
                    exchanges=exchanges,
                    telegram_members=(
                        telegram_stores.directory if telegram_stores is not None else None
                    ),
                    telegram_invites=(
                        telegram_stores.invites if telegram_stores is not None else None
                    ),
                )
            finally:
                await _stop_background_tasks(scheduler_task, sweeper_task, telegram)
                await manager.stop_all()
    finally:
        await engine.dispose()
        if telegram_stores is not None:
            await telegram_stores.engine.dispose()


def _configure_logging() -> None:
    """Ensure application and core logs reach a handler (idempotent).

    httpx is pinned to WARNING for the same reason the standalone Telegram entry
    point does it: it logs full request URLs at INFO, and a Bot API URL carries
    the bot token — which would then sit in the container logs of a process that
    also runs the bot.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger(HTTPX_LOGGER).setLevel(logging.WARNING)


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
            app.state.task_store = rt.task_store
            app.state.instructions = rt.instructions
            app.state.admin_read_model = rt.admin_read_model
            app.state.secret_store = rt.secret_store
            app.state.secret_links = rt.secret_links
            app.state.dialogs = rt.dialogs
            app.state.summary_store = rt.summary_store
            app.state.exchanges = rt.exchanges
            app.state.telegram_members = rt.telegram_members
            app.state.telegram_invites = rt.telegram_invites
            yield

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)

    gate = AuthGate(
        username=resolved_settings.admin_username,
        password_hash=resolved_settings.admin_password_hash,
    )
    # the admin router's own dependency resolves the same object, so a request
    # verified by the middleware does not hash again
    app.state.auth_gate = gate

    @app.middleware("http")
    async def authenticate(request: Request, call_next: NextCall) -> Response:
        """Require the operator credential for everything but the health probes.

        A middleware rather than per-router dependencies because it has to cover
        what routers do not: the static console, `/docs` and `/openapi.json`.
        Exceptions raised here bypass the app's handlers, so the responses are
        built by hand.

        Two checks, in order. The first refuses state-changing requests a
        browser sends from another site: with Basic auth the credential rides
        along automatically, so a form on an attacker's page would otherwise
        act as the operator. The second is the credential itself.
        """
        if is_cross_site_mutation(request):
            logger.warning("cross-site %s to %s refused", request.method, request.url.path)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": CROSS_SITE_MESSAGE},
            )
        if not is_open_path(request.url.path):
            try:
                await gate.authenticate(request)
            except HTTPException as denied:
                return JSONResponse(
                    status_code=denied.status_code,
                    content={"detail": denied.detail},
                    headers=denied.headers,
                )
        return await call_next(request)

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
    app.include_router(admin_router)
    app.include_router(secrets_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def _probe_search_extensions(engine: AsyncEngine) -> frozenset[str]:
    """Ask the database which optional search extensions it actually has.

    Read-only and never fatal: a database that cannot answer (or is not
    Postgres) reports nothing, and search stays on the portable brute-force
    path. This decides which store class the composition root builds, so it has
    to run after the schema bootstrap that creates the extensions.
    """
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(installed_search_extensions)
    except SQLAlchemyError:
        logger.warning("could not probe search extensions; assuming none", exc_info=True)
        return frozenset()


async def _probe_lexical_backend(
    engine: AsyncEngine,
    search_extensions: frozenset[str],
) -> LexicalBackend:
    """Decide which engine answers the lexical half of a search, if any.

    Postgres wins where pg_textsearch exists; otherwise a SQLite database whose
    FTS5 mirrors were built by the migration answers instead. Never fatal: a
    database that cannot do either reports NONE and recall stays on embeddings.
    """
    if PG_TEXTSEARCH in search_extensions:
        return LexicalBackend.POSTGRES
    try:
        async with engine.connect() as connection:
            if await connection.run_sync(has_sqlite_fts):
                return LexicalBackend.SQLITE
    except SQLAlchemyError:
        logger.warning("could not probe SQLite full-text search; assuming none", exc_info=True)
    return LexicalBackend.NONE


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
        await sync_system_registry(instructions, _system_registry(settings))
        # finish deferred embeddings (a backend that was down, rows written by
        # embedder-less migrations) and repair a changed embedding model, whose
        # old vectors are not comparable with anything the new one produces
        resynced = await instructions.resync_embeddings()
        if resynced:
            logger.info("re-embedded %d record(s) whose vector was missing or stale", resynced)
    except (LLMError, LLMResponseError, SQLAlchemyError):
        logger.warning(
            "System skill registry sync failed; starting without it",
            exc_info=True,
        )


def _system_registry(settings: Settings) -> tuple[SystemSkill, ...]:
    """Built-in registry with the installation's file overlay applied.

    The overlay (`OF_SYSTEM_SKILLS_SOURCE`) is how a deployment tunes
    scenarios — localized trigger phrases, house rules, extra records —
    without a rebuild and without editing core. No source configured: the
    built-in registry as is.
    """
    registry = CORE_SYSTEM_SKILLS + WEB_SYSTEM_SKILLS
    path = settings.to_skills_overlay_path()
    if path is None:
        return registry
    patches = load_overlay(path)
    if not patches:
        return registry
    logger.info("system skills overlay applied: %d patch(es) from %s", len(patches), path)
    return apply_overlay(registry, patches)


def _build_secret_store(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> SecretStore | None:
    """Build the encrypted secret store when OF_SECRETS_KEY is configured.

    A malformed key raises at startup: silently starting without secrets
    would surface much later as a confusing per-call failure.
    """
    if not settings.secrets_key:
        return None
    return SqlAlchemySecretStore(session_factory, settings.secrets_key)


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


def _build_vision_client(settings: Settings, http_client: httpx.AsyncClient) -> VisionClient | None:
    """Build the optional vision client; None (feature off) when no model is configured.

    The transport (`TelegramPoller`) only ever sees the `VisionClient` port —
    this is the one place a concrete implementation is chosen, so swapping
    providers (another vendor, a local model, an OCR engine) is a change
    here and in config, never in the poller.
    """
    if not settings.vision_configured():
        return None
    return OpenAIVisionClient(http_client=http_client, config=settings.to_vision_config())


def _build_deep_vision_client(
    settings: Settings, http_client: httpx.AsyncClient
) -> VisionClient | None:
    """Build the optional strong vision tier client (the `image_look` tool).

    Reuses the same vision `httpx.AsyncClient` as the cheap ingestion tier
    (`_build_vision_client`) — same base URL, only the model differs. None
    (the tool stays hidden) when `OF_VISION_DEEP_MODEL` is empty.
    """
    if not settings.deep_vision_configured():
        return None
    return OpenAIVisionClient(http_client=http_client, config=settings.to_deep_vision_config())


def _build_speech_client(
    settings: Settings, http_client: httpx.AsyncClient
) -> TranscriptionClient | None:
    """Build the optional transcription client; None (feature off) when unconfigured.

    Deliberately no fallback to the main LLM's endpoint: `/audio/transcriptions`
    is a different endpoint kind, and a chat-only gateway 404s on it — a silent
    fallback would turn "voice is off" into "every voice message errors".
    """
    if not settings.speech_configured():
        return None
    return OpenAITranscriptionClient(http_client=http_client, config=settings.to_speech_config())


def _build_telegram_image_resolver(
    settings: Settings, http_client: httpx.AsyncClient
) -> ImageResolver | None:
    """Build the Telegram-backed `ImageResolver`; None when the bot is not configured.

    A dedicated `TelegramBotClient` rather than reusing the one `_start_telegram`
    builds for the poller: `TelegramBotClient` is a thin, stateless wrapper over
    the shared `outbound_http` client (base URLs plus the token), so a second
    instance costs nothing extra, and the runner config is assembled here —
    before `_start_telegram` runs — so the standalone Telegram entry point
    (which shares this same `runtime()`, no HTTP surface) gets a working
    resolver too, independent of when or whether the poller itself starts.
    """
    if not settings.telegram_bot_token:
        return None
    return TelegramImageResolver(
        TelegramBotClient(http_client=http_client, token=settings.telegram_bot_token)
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
        history_search_default_limit=settings.history_search_default_limit,
        history_search_max_limit=settings.history_search_max_limit,
        http_request_allowed_origins=tuple(settings.http_request_allowlist),
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
        # ConversationManager satisfies the CronWaker port structurally
        # (identical wake() signature) — no adapter needed
        manager,
        owner=uuid.uuid4().hex,
        config=CronSchedulerConfig(
            poll_interval_seconds=settings.cron_poll_interval_seconds,
            lease_ttl_seconds=settings.cron_lease_ttl_seconds,
            replay_limit=settings.cron_replay_limit,
        ),
    )
    return asyncio.create_task(scheduler.run_forever())


def _start_collecting_sweeper(
    exchanges: ExchangeRepository,
    dialogs: DialogRepository,
    manager: ConversationManager,
    settings: Settings,
) -> asyncio.Task[None]:
    """Start the loop that reacts to forwarded material once it falls quiet."""
    sweeper = build_collecting_sweeper(
        exchanges,
        dialogs,
        # ConversationManager satisfies the CollectionPromoter port structurally
        manager,
        quiet_seconds=settings.material_quiet_seconds,
        interval_seconds=settings.material_sweep_interval_seconds,
    )
    return asyncio.create_task(sweeper.run_forever())


@dataclass(frozen=True, slots=True)
class _TelegramStores:
    """The Telegram surface's own database: invites plus member profiles."""

    invites: SqlAlchemyInviteStore
    directory: SqlAlchemyMemberDirectory
    engine: AsyncEngine


async def _build_telegram_stores(settings: Settings) -> _TelegramStores | None:
    """Build the invite store and member directory when Telegram is enabled.

    The schema is bootstrapped with a plain create_all: a couple of small
    isolated tables on their own Base/engine, no Alembic chain of their own.
    """
    if not settings.telegram_bot_token:
        return None
    engine = create_engine(settings.telegram_database_url)
    async with engine.begin() as connection:
        await connection.run_sync(InviteBase.metadata.create_all)
    session_factory = create_session_factory(engine)
    return _TelegramStores(
        invites=SqlAlchemyInviteStore(
            session_factory, ttl_seconds=settings.telegram_invite_ttl_seconds
        ),
        directory=SqlAlchemyMemberDirectory(session_factory),
        engine=engine,
    )


@dataclass(frozen=True, slots=True)
class _TelegramExtras:
    """Optional collaborators of the Telegram surface."""

    stores: _TelegramStores | None = None
    secrets_link: Callable[[str], str] | None = None
    # None: vision is off, Telegram keeps today's placeholder/text-only path
    vision: VisionClient | None = None
    # None: speech-to-text is off, a recording keeps the "text only" notice
    speech: TranscriptionClient | None = None


def _secrets_link_builder(
    settings: Settings, secret_links: SecretLinkService
) -> Callable[[str], str]:
    """Build the /secrets URL factory: a fresh one-time token per request.

    The token rides in the URL *fragment*, not the query string: a fragment is
    never sent to the server, so it cannot land in an access log (Caddy logs
    the request URI), in a proxy log or in a Referer header. The page reads it
    from `location.hash` and posts it in a request body.
    """

    def build(user_id: str) -> str:
        token = secret_links.issue(user_id)
        return f"{settings.resolved_public_base_url()}/secrets.html#token={token}"

    return build


def _start_telegram(
    settings: Settings,
    runner_provider: RunnerProvider,
    dialogs: DialogRepository,
    http_client: httpx.AsyncClient,
    extras: _TelegramExtras | None = None,
) -> tuple[TelegramBridgeRegistry, asyncio.Task[None]] | None:
    """Start the Telegram long-poll adapter when a bot token is configured.

    The membership gate activates only with admin ids configured: without
    admins there is nobody to issue invites, and gating would lock every
    existing user out (legacy open behavior is kept instead).
    """
    if not settings.telegram_bot_token:
        return None
    resolved = extras if extras is not None else _TelegramExtras()
    client = TelegramBotClient(http_client=http_client, token=settings.telegram_bot_token)
    registry = TelegramBridgeRegistry(
        runner_provider=runner_provider,
        client=client,
        edit_throttle_seconds=settings.telegram_edit_throttle_seconds,
        rich_messages_enabled=settings.telegram_rich_messages,
    )
    membership = None
    if resolved.stores is not None and settings.telegram_admin_ids:
        membership = TelegramMembership(resolved.stores.invites, settings.telegram_admin_ids)
    poller = TelegramPoller(
        client=client,
        registry=registry,
        options=TelegramPollerOptions(
            poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
            membership=membership,
            secrets_link=resolved.secrets_link,
            directory=resolved.stores.directory if resolved.stores is not None else None,
            vision=resolved.vision,
            speech=resolved.speech,
            voice_max_seconds=settings.voice_max_seconds,
        ),
    )
    task = asyncio.create_task(_run_telegram(poller, registry, dialogs))
    task.add_done_callback(_report_telegram_task_failure)
    return registry, task


async def _run_telegram(
    poller: TelegramPoller,
    registry: TelegramBridgeRegistry,
    dialogs: DialogRepository,
) -> None:
    """Warm bridges for known Telegram dialogs, then poll for updates."""
    user_ids = await dialogs.list_user_ids_by_channel(TELEGRAM_CHANNEL)
    await registry.warm(user_ids)
    await poller.run_forever()


def _report_telegram_task_failure(task: asyncio.Task[None]) -> None:
    """Supervisor-lite: loudly report the Telegram surface task dying.

    `TelegramPoller.run_forever` already retries after any exception it can
    see, so reaching here at all means something failed before that loop's
    own catch-all could take over (warming bridges, listing dialogs) or an
    exception type it cannot catch escaped. Either way the Telegram surface
    is now dark for every user until a process restart, which is worth an
    error-level log, not a silently dropped task result. Cancellation
    (normal shutdown via `_stop_background_tasks`) is not a failure.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Telegram surface task terminated unexpectedly", exc_info=exc)


async def _stop_background_tasks(
    scheduler_task: asyncio.Task[None],
    sweeper_task: asyncio.Task[None],
    telegram: tuple[TelegramBridgeRegistry, asyncio.Task[None]] | None,
) -> None:
    """Stop the background loops and the Telegram adapter, if it was started."""
    for task in (scheduler_task, sweeper_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if telegram is not None:
        registry, poller_task = telegram
        poller_task.cancel()
        with suppress(asyncio.CancelledError):
            await poller_task
        await registry.aclose()


app = create_app()
