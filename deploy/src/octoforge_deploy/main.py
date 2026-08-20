"""FastAPI application factory and composition root (shared by standalone surfaces)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from octoforge_console import surface as console
from octoforge_console.surface import ConsoleSurface
from octoforge_core import (
    ConversationManager,
    DialogRepository,
    ExchangeRepository,
    ManagerStores,
    OwnershipConfig,
    SqlAlchemyTaskStore,
    UnitOfWork,
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
    build_limit_service,
    build_llm_client,
    build_router,
    build_runner_config,
    build_summary_store,
    build_tool_registry,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.admin.store import SqlAlchemyAdminStore
from octoforge_core.agent.prompts import PromptProvider, StaticPromptProvider
from octoforge_core.composition import (
    CollectionsRuntime,
    LexicalBackend,
    ResponseLayer,
    RunnerOptions,
    ToolLimits,
    ToolServices,
    ToolStores,
    build_collections,
    build_response_layer,
)
from octoforge_core.config import EmbeddingBackend, HttpRerankerConfig, RerankerConfig
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
from octoforge_core.dialogs.api import MessageRepository
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.errors import LLMResponseError
from octoforge_core.identity.api import IdentityStore
from octoforge_core.identity.service import AccessService
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.registry import (
    CORE_SYSTEM_SKILLS,
    SystemSkill,
    sync_system_registry,
)
from octoforge_core.llm.embedding_cache import CachingEmbeddingClient
from octoforge_core.llm.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from octoforge_core.llm.errors import LLMError
from octoforge_core.llm.http_reranker import HttpRerankerClient
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder
from octoforge_core.llm.reranker import CrossEncoderReranker, RerankerClient
from octoforge_core.mcp.api import MIRROR_KIND
from octoforge_core.mcp.client import StreamableHttpMcpClient
from octoforge_core.mcp.executor import McpMirrorCallExecutor
from octoforge_core.mcp.skills import McpSkillGenerator
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore
from octoforge_core.mcp.sync import McpSyncLoop, McpToolSync
from octoforge_core.mcp.tools import McpAddTool
from octoforge_core.media.api import MediaResult, MediaUnderstanding
from octoforge_core.media.service import MediaService
from octoforge_core.net.collections.api import CollectionConfig
from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.net.external import CallCredentials, ExternalCallAuth
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.response_memory import ResponseMemoryConfig
from octoforge_core.params.store import SqlAlchemyUserParamStore
from octoforge_core.ports import LLMClient
from octoforge_core.retention_sweep import RetentionSweeper
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.serper import SerperSearchProvider
from octoforge_core.secrets.api import SecretStore
from octoforge_core.secrets.link_store import SqlAlchemySecretFormLinkStore
from octoforge_core.secrets.store import SqlAlchemySecretStore
from octoforge_core.secrets.tools import SecretLinkTool, SecretListTool
from octoforge_core.settings.api import max_active_users
from octoforge_core.settings.store import SqlAlchemySettingsStore
from octoforge_core.speech.api import AudioResolver, TranscriptionClient
from octoforge_core.speech.client import OpenAITranscriptionClient
from octoforge_core.tariffs.api import CORE_FEATURES, LimitGate
from octoforge_core.tariffs.store import SqlAlchemyTariffStore, SqlAlchemyUsageMeter
from octoforge_core.tools.base import Tool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_core.vision.api import ImageResolver, VisionClient
from octoforge_core.vision.client import OpenAIVisionClient
from octoforge_server.app import SECRETS_PAGE, build_app
from octoforge_server.capabilities import log_capabilities
from octoforge_server.channels import WEB_CHANNEL
from octoforge_server.config import Settings
from octoforge_server.logs import configure_logging
from octoforge_server.prompts import FilePromptProvider
from octoforge_server.runtime_state import Runtime
from octoforge_server.secret_links import (
    PersonSecretsLinkBuilder,
    SecretLinkService,
    secrets_link_builder,
)
from octoforge_server.skill_overlay import apply_overlay, load_overlay
from octoforge_server.surfaces import Surface, SurfaceRoutes
from octoforge_server.system_skills import WEB_SYSTEM_SKILLS
from octoforge_telegram import capabilities as telegram_capabilities
from octoforge_telegram import surface as telegram_surface
from octoforge_telegram.admin import AdminAccess, AdminManageTool, AdminStores
from octoforge_telegram.audio import TelegramAudioResolver
from octoforge_telegram.bridge import RunnerProvider
from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.drafts import SqlAlchemyDraftStore
from octoforge_telegram.images import TelegramImageResolver
from octoforge_telegram.invites.store import (
    SqlAlchemyInviteStore,
    SqlAlchemyMemberDirectory,
    SqlAlchemyReferralStore,
)
from octoforge_telegram.notifier import TelegramActivationNotifier
from octoforge_telegram.poller import (
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
)
from octoforge_telegram.schema import TelegramSurfaceBase
from octoforge_telegram.surface import TelegramSurface
from octoforge_webui import surface as webui
from octoforge_webui.surface import WebUiSurface
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

APP_TITLE = "OctoForge"
# names this process's own log file when OF_LOG_DIR is set; one deployment
# runs several processes against one directory
WEB_PROCESS_LOG_NAME = "app"
HEALTH_STATUS = "ok"
READY_STATUS = "ready"
NOT_READY_STATUS = "not-ready"
USER_ID_HEADER = "X-User-Id"
USER_ID_HEADER_VALUE_TEMPLATE = "{user_id}"
ADMIN_NOT_SEEN_MESSAGE = (
    "%d of %d OF_TELEGRAM_ADMIN_IDS have never messaged this bot, so they have no "
    "person to be an admin as and admin_manage stays hidden from them; it appears "
    "after they write once and this process restarts"
)

logger = logging.getLogger(__name__)

# Signature of the next handler in Starlette's middleware chain.
NextCall = Callable[[Request], Awaitable[Response]]


async def _probe_backends(engine: AsyncEngine) -> tuple[frozenset[str], LexicalBackend]:
    """What the database can actually do, asked once the schema exists."""
    search_extensions = await _probe_search_extensions(engine)
    return search_extensions, await _probe_lexical_backend(engine, search_extensions)


def _report_capabilities(
    settings: Settings,
    telegram_settings: TelegramSettings,
    search_extensions: frozenset[str],
    lexical_backend: LexicalBackend,
) -> None:
    """One startup report, assembled from the service and its interfaces.

    The service cannot describe a bot it does not know exists, so each
    surface says its own piece and the deployment gathers them.
    """
    log_capabilities(
        settings,
        logger,
        search_extensions,
        lexical_backend,
        extra=(telegram_capabilities.describe(telegram_settings),),
    )


def _build_manager_stores(
    session_factory: async_sessionmaker[AsyncSession],
) -> ManagerStores:
    """Build the persistence collaborators one conversation manager needs."""
    return ManagerStores(
        dialogs=SqlAlchemyDialogRepository(session_factory),
        messages=SqlAlchemyMessageRepository(session_factory),
        tasks=SqlAlchemyTaskStore(session_factory),
        exchanges=SqlAlchemyExchangeRepository(session_factory),
        claims=SqlAlchemyClaimRepository(session_factory),
        uow=UnitOfWork(session_factory),
    )


@asynccontextmanager
async def runtime(settings: Settings) -> AsyncIterator[Runtime]:
    """Build all services and background tasks; no HTTP listener is involved.

    Shared composition root: the FastAPI lifespan wraps it, standalone
    surfaces (the Telegram-only runner) use it directly. The object graph is
    assembled from the reusable builders of `octoforge_core.composition`;
    only the settings/transport specifics live here.
    """
    # each surface reads its own settings from the same environment; the
    # service's object never carried them
    telegram_settings = TelegramSettings()
    engine = create_engine(settings.database_url)
    await _bootstrap_schema(engine)
    # after the bootstrap: the schema step is what creates the extensions, so
    # probing earlier would report a gap the next line has already filled
    search_extensions, lexical_backend = await _probe_backends(engine)
    _report_capabilities(settings, telegram_settings, search_extensions, lexical_backend)
    session_factory = create_session_factory(engine)
    await _sweep_retention(settings, session_factory)
    manager_stores = _build_manager_stores(session_factory)
    dialogs, exchanges = manager_stores.dialogs, manager_stores.exchanges
    claims, task_store, identity = (
        manager_stores.claims,
        manager_stores.tasks,
        SqlAlchemyIdentityStore(session_factory),
    )
    cron_store, secret_store, user_params, tariff_store, usage_meter, settings_store = (
        SqlAlchemyCronStore(session_factory),
        _build_secret_store(settings, session_factory),
        SqlAlchemyUserParamStore(session_factory),
        SqlAlchemyTariffStore(session_factory),
        SqlAlchemyUsageMeter(session_factory),
        SqlAlchemySettingsStore(session_factory),
    )
    # the cap arrives as a callable: identity must not import settings, the
    # two modules meet only here
    limit_service, access = (
        build_limit_service(tariff_store, usage_meter),
        AccessService(identity, lambda: max_active_users(settings_store)),
    )
    secret_links = SecretLinkService(
        settings.secrets_key, codes=SqlAlchemySecretFormLinkStore(session_factory)
    )
    telegram_stores = await _build_telegram_stores(telegram_settings)
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
            vision_client, deep_vision_client, speech_client, image_resolver = (
                _build_vision_client(settings, vision_http),
                _build_deep_vision_client(settings, vision_http),
                _build_speech_client(settings, speech_http),
                _build_telegram_image_resolver(telegram_settings, outbound_http),
            )
            media_service = _build_media_service(
                telegram_settings, outbound_http, vision_client, speech_client, limit_service
            )
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
                limits=limit_service,
            )
            await _sync_system_skills(instructions, settings)
            # The app's own base URL is allowlisted so tool records can
            # target our loopback HTTP API (cron jobs) past the SSRF guard.
            guard = SsrfGuard(allowed_prefixes=(settings.self_base_url,))
            summary_store = build_summary_store(session_factory, lexical_search=lexical_backend)
            # Postgres-only: elsewhere None, and every response keeps the old
            # truncation — the capability pattern, decided here and nowhere else
            collections_runtime, response_layer = _build_data_tiers(
                settings, engine, session_factory
            )
            spill = response_layer.spill
            mcp = _build_mcp(
                _McpDeps(
                    session_factory=session_factory,
                    outbound_http=outbound_http,
                    guard=guard,
                    instructions=instructions,
                    secret_store=secret_store,
                    llm=llm_client,
                    spill=spill,
                )
            )
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
                    collections=collections_runtime,
                    responses=response_layer,
                    executor=build_external_executor(
                        service=instructions,
                        http_client=outbound_http,
                        guard=guard,
                        credentials=CallCredentials(
                            auth_whitelist=_external_call_whitelist(settings),
                            secrets=secret_store,
                            user_params=user_params,
                        ),
                        delegates={MIRROR_KIND: mcp.call_delegate},
                        spill=spill,
                    ),
                    search_provider=_build_search_provider(settings, outbound_http),
                    mcp_add=mcp.add_tool,
                    secret_list=(
                        SecretListTool(secret_store) if secret_store is not None else None
                    ),
                    secret_link=(
                        SecretLinkTool(PersonSecretsLinkBuilder(settings, secret_links))
                        if secret_store is not None
                        else None
                    ),
                ),
                limits=_tool_limits(settings),
                limit_gate=limit_service,
            )
            admin_tool = await _build_telegram_admin_tool(
                telegram_settings,
                telegram_stores,
                outbound_http,
                _AdminToolServices(
                    cron_store=cron_store,
                    dialogs=dialogs,
                    messages=manager_stores.messages,
                    instructions=instructions,
                    identities=identity,
                ),
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
                        meter=limit_service,
                    ),
                    options=RunnerOptions(
                        max_processes=settings.max_processes,
                        task_outcome_listener=build_cron_outcome_reporter(cron_store),
                        vision=deep_vision_client,
                        image_resolver=image_resolver,
                        limits=limit_service,
                        response_memory=response_layer.memory,
                    ),
                ),
                stores=manager_stores,
                ownership=OwnershipConfig(node_id=settings.node_id),
            )
            # Renderers BEFORE recovery, so a dialog recovered here can be
            # rendered: recovery builds that dialog's actor, and the manager
            # attaches the surface to every actor it builds. Registered after
            # recovery instead, a cron answer that finished while this process
            # was down would be redelivered into a dialog with no transport
            # and wait, marked undelivered, until its user wrote again.
            surfaces = _attach_renderers(
                _installed_surfaces(
                    _build_telegram_surface(
                        telegram_settings,
                        manager.get_or_create_runner,
                        outbound_http,
                        _TelegramExtras(
                            stores=telegram_stores,
                            secrets_link=(
                                secrets_link_builder(settings, secret_links, TELEGRAM_CHANNEL)
                                if secret_store is not None
                                else None
                            ),
                            media=PersonScopedMedia(media_service, identity, TELEGRAM_CHANNEL),
                            access=access,
                            admin_tool=admin_tool,
                            identities=identity,
                        ),
                    )
                ),
                manager,
                registry,
            )
            # Sweep before the scheduler and surfaces start: orphaned tasks
            # are restarted as background processes and persisted results
            # that never reached their dialog are redelivered. Only dialogs
            # no other live instance owns are touched.
            await manager.recover_interrupted()
            # the claim heartbeat starts after recovery: until it has run,
            # this process holds no dialogs worth advertising as live
            manager.start()
            background_tasks = (
                _start_cron_scheduler(cron_store, manager, settings),
                _start_collecting_sweeper(exchanges, dialogs, manager, settings),
                _start_mcp_sync(mcp.sync, settings),
                *(
                    (_start_collections_sweeper(collections_runtime.store, settings),)
                    if collections_runtime is not None
                    else ()
                ),
            )
            await _start_surfaces(surfaces)
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
                    user_params=user_params,
                    tariff_store=tariff_store,
                    usage_meter=usage_meter,
                    limit_gate=limit_service,
                    # this assembly ships no custom tools; an installer with
                    # its own gated tools passes CORE_FEATURES | {its codes}
                    known_features=CORE_FEATURES,
                    dialogs=dialogs,
                    summary_store=summary_store,
                    exchanges=exchanges,
                    claims=claims,
                    identity_store=identity,
                    settings_store=settings_store,
                    access=access,
                    media_service=media_service,
                    channels=_served_channels(surfaces),
                    activation_notifier=(
                        TelegramActivationNotifier(
                            TelegramBotClient(
                                http_client=outbound_http,
                                token=telegram_settings.telegram_bot_token,
                            ),
                            identity,
                        )
                        if telegram_settings.telegram_bot_token
                        else None
                    ),
                    surface_state=_surface_state(telegram_stores),
                )
            finally:
                await _stop_background_tasks(*background_tasks)
                for surface in surfaces:
                    await _close_surface(surface)
                await manager.stop_all()
    finally:
        await engine.dispose()
        if telegram_stores is not None:
            await telegram_stores.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build this deployment: the service, plus the interfaces installed on it."""
    resolved = settings or Settings()
    configure_logging(
        WEB_PROCESS_LOG_NAME,
        log_dir=resolved.log_dir,
        max_mb=resolved.log_max_mb,
        backups=resolved.log_backups,
    )
    return build_app(resolved, runtime, _surface_routes())


def _surface_routes() -> tuple[SurfaceRoutes, ...]:
    """What the installed interfaces serve.

    Gathered from module constants because mounting happens before any
    surface object exists — see the `Surface` port. This tuple and
    `_installed_surfaces` are the two halves of "which interfaces does this
    deployment have", and they are meant to be read together.
    """
    return (
        SurfaceRoutes(
            name=console.SURFACE_NAME,
            routers=console.ROUTERS,
            static_files=console.STATIC_FILES,
        ),
        SurfaceRoutes(
            name=webui.SURFACE_NAME, routers=webui.ROUTERS, static_files=webui.STATIC_FILES
        ),
        SurfaceRoutes(
            name=telegram_surface.SURFACE_NAME,
            routers=telegram_surface.ROUTERS,
            static_files=telegram_surface.STATIC_FILES,
        ),
        # the service's own page, listed here so mounting has one path
        SurfaceRoutes(name="secrets", static_files=(SECRETS_PAGE,)),
    )


def _served_channels(surfaces: Sequence[Surface]) -> frozenset[str]:
    """Which channels a request may name, as the installed surfaces declared them.

    Assembled rather than written down: a deployment without Telegram must
    not accept `telegram`, or a message would open a dialog nobody reads.
    """
    return frozenset(
        channel for channel in (surface.channel() for surface in surfaces) if channel is not None
    )


def _surface_state(stores: _TelegramStores | None) -> dict[str, Any]:
    """What installed surfaces need their own dependencies to reach."""
    return {
        "telegram_members": stores.directory if stores is not None else None,
        "telegram_invites": stores.invites if stores is not None else None,
        "telegram_referrals": stores.referrals if stores is not None else None,
    }


async def _sweep_retention(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Apply the configured retention policy once at startup.

    A no-op unless an operator set a limit, and never fatal: failing to prune
    old rows is not a reason to refuse to serve. Runs at startup rather than on
    a timer because the tables it touches grow slowly and a restart is frequent
    enough — a background schedule would be more machinery for the same effect.
    """
    policy = settings.retention_policy()
    if not policy.enabled():
        return
    try:
        outcome = await RetentionSweeper(session_factory, policy).sweep()
    except SQLAlchemyError:
        logger.warning("retention sweep failed; starting without it", exc_info=True)
        return
    logger.info("retention (%s): removed %s", policy.describe(), outcome.describe())


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
    """Choose the embeddings backend: local sentence-transformers or HTTP.

    Wrapped in a small cache because `recall` searches instructions and datasets
    with the same query string and each service embeds it independently — two
    identical computations on nearly every user message. The decorator keeps
    that fix out of both services' ports.
    """
    backend: EmbeddingClient
    if settings.embedding_backend == EmbeddingBackend.LOCAL:
        backend = SentenceTransformerEmbedder(
            model_name=settings.embedding_model,
            batch_size=settings.embedding_batch_size,
        )
    else:
        backend = OpenAIEmbeddingClient(
            http_client=http_client,
            config=settings.to_embedding_config(),
        )
    return CachingEmbeddingClient(backend)


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


class PersonScopedMedia:
    """`MediaUnderstanding` that resolves the account before asking the core.

    The one place where in-process ingestion turns a surface handle into a
    person. It lives here rather than in `core/media/` because nothing inside
    core resolves handles — plans, statuses and usage are all filed under
    people, and `MediaService` must never be handed a `tg:<id>` (asked about
    one it would find no binding and answer yes to everything, which is the
    bug that let a free plan use paid vision for months).

    Its counterpart in the split arrangement is the HTTP endpoint, where the
    same resolution happens in `get_user_id`.
    """

    def __init__(self, media: MediaService, identities: IdentityStore, channel: str) -> None:
        self._media = media
        self._identities = identities
        self._channel = channel

    async def describe(self, user_id: str, refs: Sequence[str]) -> tuple[MediaResult, ...]:
        return await self._media.describe(await self._person(user_id), refs)

    async def transcribe(
        self,
        user_id: str,
        ref: str,
        seconds: int | None,
        min_seconds: int,
        max_seconds: float,
    ) -> MediaResult:
        return await self._media.transcribe(
            await self._person(user_id), ref, seconds, min_seconds, max_seconds
        )

    async def _person(self, user_id: str) -> str:
        return await self._identities.resolve_or_create(
            self._channel, user_id.removeprefix(USER_ID_PREFIX)
        )


def _build_telegram_image_resolver(
    settings: TelegramSettings, http_client: httpx.AsyncClient
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


def _build_media_service(
    telegram_settings: TelegramSettings,
    http_client: httpx.AsyncClient,
    vision: VisionClient | None,
    speech: TranscriptionClient | None,
    limits: LimitGate,
) -> MediaService:
    """Every model call about a user's media, with its plan check and ledger.

    The resolvers are Telegram's because that is the only transport whose
    references exist today; a second surface would add its own here.
    """
    return MediaService(
        vision=vision,
        images=_build_telegram_image_resolver(telegram_settings, http_client),
        speech=speech,
        audio=_build_telegram_audio_resolver(telegram_settings, http_client),
        limits=limits,
    )


def _build_telegram_audio_resolver(
    settings: TelegramSettings, http_client: httpx.AsyncClient
) -> AudioResolver | None:
    """The recording half of the same story; None when the bot is not configured."""
    if not settings.telegram_bot_token:
        return None
    return TelegramAudioResolver(
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


def _collections_config(settings: Settings) -> CollectionConfig:
    """Map the settings' collections knobs to the core config bundle."""
    return CollectionConfig(
        ttl_seconds=settings.collections_ttl_seconds,
        max_per_user=settings.collections_max_per_user,
        max_bytes_per_user=settings.collections_max_mb_per_user * 1024 * 1024,
        query_max_limit=settings.collections_query_max_limit,
        inline_max_chars=settings.collections_inline_max_chars,
        collect_max_pages=settings.collections_collect_max_pages,
    )


def _build_data_tiers(
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[CollectionsRuntime | None, ResponseLayer]:
    """The two homes of a fetched response: the database tier (Postgres only,
    None elsewhere) and the always-present task-memory layer over it."""
    collections_runtime = build_collections(engine, session_factory, _collections_config(settings))
    response_layer = build_response_layer(
        collections_runtime,
        _collections_config(settings),
        _response_memory_config(settings),
    )
    return collections_runtime, response_layer


def _response_memory_config(settings: Settings) -> ResponseMemoryConfig:
    """Map the settings' response-memory knobs to the core config bundle."""
    megabyte = 1024 * 1024
    return ResponseMemoryConfig(
        max_response_chars=settings.response_memory_max_mb * megabyte,
        budget_chars=settings.response_memory_budget_mb * megabyte,
        get_default_chars=settings.response_get_default_chars,
        get_max_chars=settings.response_get_max_chars,
    )


def _start_collections_sweeper(
    store: SqlAlchemyCollectionStore, settings: Settings
) -> asyncio.Task[None]:
    """Drop expired collections on a timer.

    Started only in assemblies where `build_collections` answered — the
    sweep is the TTL's other half, and a TTL nobody enforces is a quota
    that only ever grows.
    """

    async def run() -> None:
        while True:
            await asyncio.sleep(settings.collections_sweep_interval_seconds)
            try:
                dropped = await store.delete_expired()
                if dropped:
                    logger.info("collections sweep: %d expired dropped", dropped)
            except Exception:
                logger.exception("collections sweep failed; retrying next interval")

    return asyncio.create_task(run())


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
class _McpRuntime:
    """The MCP module assembled: the sync loop's engine and the two hooks."""

    sync: McpToolSync
    call_delegate: McpMirrorCallExecutor
    add_tool: McpAddTool


@dataclass(frozen=True, slots=True)
class _McpDeps:
    """What the MCP module borrows from the rest of the runtime."""

    session_factory: async_sessionmaker[AsyncSession]
    outbound_http: httpx.AsyncClient
    guard: SsrfGuard
    instructions: InstructionService
    secret_store: SecretStore | None
    # the group-skill generator's model: the crawler that turns a server's
    # tool list into one usage skill (or logs the shape it cannot classify)
    llm: LLMClient
    # oversized structured results become collections; None = truncation
    spill: ResponseSpill | None = None


def _build_mcp(deps: _McpDeps) -> _McpRuntime:
    """Assemble the MCP module: shared servers, mirror sync, call delegate."""
    store = SqlAlchemyMcpServerStore(deps.session_factory)
    client = StreamableHttpMcpClient(deps.outbound_http, deps.guard)
    sync = McpToolSync(
        store,
        client,
        deps.instructions,
        secrets=deps.secret_store,
        skills=McpSkillGenerator(deps.llm, deps.instructions),
    )
    return _McpRuntime(
        sync=sync,
        call_delegate=McpMirrorCallExecutor(store, client, deps.secret_store, spill=deps.spill),
        add_tool=McpAddTool(store, sync, deps.guard, secrets=deps.secret_store),
    )


def _start_mcp_sync(sync: McpToolSync, settings: Settings) -> asyncio.Task[None]:
    """Start the periodic MCP mirror refresh.

    The protocol offers no version of a server's tool list, so freshness is
    this sweep: cheap by construction (the diff is in memory, only changed
    content re-embeds), and the first interval's delay is fine — mcp_add
    already ran the first sync inline.
    """
    loop = McpSyncLoop(sync, interval_seconds=settings.mcp_sync_interval_seconds)
    return asyncio.create_task(loop.run_forever())


@dataclass(frozen=True, slots=True)
class _TelegramStores:
    """The Telegram surface's own database: invites, referrals, member profiles."""

    invites: SqlAlchemyInviteStore
    referrals: SqlAlchemyReferralStore
    directory: SqlAlchemyMemberDirectory
    drafts: SqlAlchemyDraftStore
    engine: AsyncEngine


async def _build_telegram_stores(settings: TelegramSettings) -> _TelegramStores | None:
    """Build the invite store and member directory when Telegram is enabled.

    The schema is bootstrapped with a plain create_all: a couple of small
    isolated tables on their own Base/engine, no Alembic chain of their own.
    """
    if not settings.telegram_bot_token:
        return None
    engine = create_engine(settings.telegram_database_url)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    session_factory = create_session_factory(engine)
    return _TelegramStores(
        invites=SqlAlchemyInviteStore(
            session_factory, ttl_seconds=settings.telegram_invite_ttl_seconds
        ),
        referrals=SqlAlchemyReferralStore(session_factory),
        directory=SqlAlchemyMemberDirectory(session_factory),
        drafts=SqlAlchemyDraftStore(session_factory),
        engine=engine,
    )


@dataclass(frozen=True, slots=True)
class _TelegramExtras:
    """Optional collaborators of the Telegram surface."""

    stores: _TelegramStores | None = None
    secrets_link: Callable[[str], str] | None = None
    # the agent-facing admin command; None when this deployment has no admins
    admin_tool: Tool | None = None
    # turns a Telegram account into the person it belongs to
    identities: IdentityStore | None = None
    # Turns incoming pictures and recordings into text, through the core.
    # None: media understanding is off, Telegram keeps today's
    # placeholder/text-only path.
    media: MediaUnderstanding | None = None
    # the waiting/active/banned gate; None = statuses are not enforced here
    access: AccessService | None = None


@dataclass(frozen=True, slots=True)
class _AdminToolServices:
    """Core services the Telegram admin command reads and edits through."""

    cron_store: CronStore
    dialogs: DialogRepository
    messages: MessageRepository
    instructions: InstructionService
    identities: IdentityStore


async def _admin_user_ids(identities: IdentityStore, admin_ids: Sequence[int]) -> frozenset[str]:
    """Turn the configured Telegram accounts into the people they belong to.

    Done once, here, because the visibility hook that needs the answer is
    synchronous and sits on the path that builds every LLM request — it must
    not reach a database. The mapping never changes once made, so caching it
    costs nothing in correctness.

    An admin the identity store has never seen is one who has not messaged
    the bot yet, and is reported rather than passed over in silence: they get
    no admin tool until they do and this process restarts.
    """
    resolved: set[str] = set()
    unknown: list[int] = []
    for external in admin_ids:
        user_id = await identities.resolve(TELEGRAM_CHANNEL, str(external))
        if user_id is None:
            unknown.append(external)
        else:
            resolved.add(user_id)
    if unknown:
        logger.warning(ADMIN_NOT_SEEN_MESSAGE, len(unknown), len(admin_ids))
    return frozenset(resolved)


async def _build_telegram_admin_tool(
    settings: TelegramSettings,
    stores: _TelegramStores | None,
    http_client: httpx.AsyncClient,
    services: _AdminToolServices,
) -> Tool | None:
    """Build `admin_manage`, or None when this deployment has no admins.

    Core services go in from the outside: the surface reaches into them, they
    never reach into it.
    """
    if stores is None or not settings.telegram_admin_ids:
        return None
    return AdminManageTool(
        AdminStores(
            invites=stores.invites,
            cron_store=services.cron_store,
            messages=services.messages,
            instructions=services.instructions,
            identities=services.identities,
            directory=stores.directory,
        ),
        AdminAccess(
            admin_user_ids=await _admin_user_ids(services.identities, settings.telegram_admin_ids),
            telegram=TelegramBotClient(http_client=http_client, token=settings.telegram_bot_token),
            bot_username=settings.resolved_bot_username(),
        ),
    )


def _build_telegram_surface(
    settings: TelegramSettings,
    runner_provider: RunnerProvider,
    http_client: httpx.AsyncClient,
    extras: _TelegramExtras | None = None,
) -> TelegramSurface | None:
    """Assemble the Telegram surface when a bot token is configured; None otherwise.

    None is what "not installed" looks like from here: nothing downstream
    special-cases Telegram, it simply is not among the surfaces.

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
        drafts=resolved.stores.drafts if resolved.stores is not None else None,
        identities=resolved.identities,
    )
    membership = None
    if resolved.stores is not None and settings.telegram_admin_ids:
        membership = TelegramMembership(
            resolved.stores.invites,
            settings.telegram_admin_ids,
            referrals=resolved.stores.referrals,
            open_registration=settings.telegram_open_registration,
        )
    poller = TelegramPoller(
        client=client,
        registry=registry,
        options=TelegramPollerOptions(
            poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
            membership=membership,
            secrets_link=resolved.secrets_link,
            directory=resolved.stores.directory if resolved.stores is not None else None,
            identities=resolved.identities,
            media=resolved.media,
            access=resolved.access,
            referrals=resolved.stores.referrals if resolved.stores is not None else None,
            bot_username=settings.resolved_bot_username(),
            voice_max_seconds=settings.voice_max_seconds,
        ),
    )
    return TelegramSurface(
        registry=registry,
        poller=poller,
        admin_tool=resolved.admin_tool,
        polls=settings.telegram_poll_in_process,
    )


async def _stop_background_tasks(*tasks: asyncio.Task[None]) -> None:
    """Stop the service's own background loops. Surfaces close themselves."""
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _installed_surfaces(telegram: TelegramSurface | None) -> tuple[Surface, ...]:
    """Which interfaces this deployment runs.

    The one place that knows the answer. Everything below is written against
    the `Surface` port, so removing one from this tuple is the whole of
    removing it — no branch elsewhere asks whether Telegram is there.
    """
    surfaces: list[Surface] = [ConsoleSurface(), WebUiSurface()]
    if telegram is not None:
        surfaces.append(telegram)
    return tuple(surfaces)


def _attach_renderers(
    surfaces: Sequence[Surface], manager: ConversationManager, registry: ToolRegistry
) -> Sequence[Surface]:
    """Give the manager each surface's renderer and the agent its tools.

    Separate from starting them because the two belong at different points of
    startup: a renderer has to be in place before recovery (an actor built
    there needs somewhere to deliver), while reading updates has to wait until
    after it (see `runtime`). Returns what it was given, so the caller keeps
    one name for the surfaces it will start later.
    """
    for surface in surfaces:
        renderer = surface.dialog_surface()
        if renderer is not None:
            manager.use_surface(renderer)
        for tool in surface.tools():
            registry.register(tool)
    return surfaces


async def _start_surfaces(surfaces: Sequence[Surface]) -> None:
    """Start each surface's background work.

    A surface that fails to start is reported and skipped rather than taking
    the application down with it: a broken bot must not cost the console and
    the API.
    """
    for surface in surfaces:
        try:
            await surface.start()
        except Exception:
            logger.exception("surface failed to start: %s", surface.name)


async def _close_surface(surface: Surface) -> None:
    """Stop one surface; a failure must not block the rest of the shutdown."""
    try:
        await surface.aclose()
    except Exception:
        logger.exception("surface failed to close: %s", surface.name)


app = create_app()
