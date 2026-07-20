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
    AgentLoop,
    ConversationManager,
    DialogRepository,
    MessageRepository,
    SkillOrigin,
    SkillRegistry,
    SqlAlchemyTaskStore,
    bootstrap_schema,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import PromptProvider, StaticPromptProvider
from octoforge_core.agent.router import LLMRouter
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.config import EmbeddingBackend
from octoforge_core.cron.api import CronStore, Scheduler
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.cron.waker import ManagerCronWaker
from octoforge_core.datasets.api import DatasetService
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.errors import LLMResponseError
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.seed import migrate_cron_tools_to_native, seed_if_empty
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.llm.embeddings import EmbeddingClient, OpenAIEmbeddingClient
from octoforge_core.llm.local_embeddings import SentenceTransformerEmbedder
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.reranker import CrossEncoderReranker, RerankerClient, RerankerConfig
from octoforge_core.memory.api import MemoryStore
from octoforge_core.memory.store import SqlAlchemyMemoryStore
from octoforge_core.net.external import ExternalCallAuth, ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.search.serper import SerperSearchProvider
from octoforge_core.skills.basic.cron_jobs import (
    CronCreateSkill,
    CronDeleteSkill,
    CronListSkill,
    CronPauseSkill,
    CronResumeSkill,
)
from octoforge_core.skills.basic.data_forget import DataForgetSkill
from octoforge_core.skills.basic.data_put import DataPutSkill
from octoforge_core.skills.basic.data_query import DataQuerySkill
from octoforge_core.skills.basic.external_call import ExternalCallSkill
from octoforge_core.skills.basic.http_request import HttpRequestSkill
from octoforge_core.skills.basic.instruction_save import InstructionSaveSkill
from octoforge_core.skills.basic.instructions_search import InstructionsSearchSkill
from octoforge_core.skills.basic.memory_delete import MemoryDeleteSkill
from octoforge_core.skills.basic.memory_search import MemorySearchSkill
from octoforge_core.skills.basic.memory_store import MemoryStoreSkill
from octoforge_core.skills.basic.task_list import TaskListSkill
from octoforge_core.skills.basic.task_spawn import TaskSpawnSkill
from octoforge_core.skills.basic.web_search import WebSearchSkill
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_web.api.cron import router as cron_router
from octoforge_web.api.dialog import router as dialog_router
from octoforge_web.config import Settings
from octoforge_web.prompts import FilePromptProvider
from octoforge_web.telegram.bridge import RunnerProvider
from octoforge_web.telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_web.telegram.poller import TelegramBridgeRegistry, TelegramPoller

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
    surfaces (the Telegram-only runner) use it directly.
    """
    engine = create_engine(settings.database_url)
    await _bootstrap_schema(engine)
    session_factory = create_session_factory(engine)
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    task_store = SqlAlchemyTaskStore(session_factory)
    cron_store = SqlAlchemyCronStore(session_factory)
    try:
        async with (
            httpx.AsyncClient(base_url=settings.llm_base_url) as llm_http,
            httpx.AsyncClient(base_url=settings.embedding_base_url) as embed_http,
            httpx.AsyncClient() as outbound_http,
        ):
            llm_client = OpenAICompatibleClient(
                http_client=llm_http,
                config=settings.to_llm_config(),
            )
            embedder = _build_embedder(settings, embed_http)
            reranker = _build_reranker(settings)
            instructions = LocalInstructionService(
                SqlAlchemyInstructionStore(session_factory),
                embedder,
                reranker=reranker,
                rerank_candidates=settings.reranker_candidates,
            )
            datasets = LocalDatasetService(SqlAlchemyDatasetStore(session_factory), embedder)
            await _seed_instructions(instructions, settings)
            # The app's own base URL is allowlisted so tool records can
            # target our loopback HTTP API (cron jobs) past the SSRF guard.
            guard = SsrfGuard(allowed_prefixes=(settings.self_base_url,))
            external_executor = ExternalCallExecutor(
                service=instructions,
                http_client=outbound_http,
                guard=guard,
                auth_whitelist=_external_call_whitelist(settings),
            )
            registry = SkillRegistry()
            _register_core_skills(registry, outbound_http, guard, task_store, cron_store)
            if settings.serper_token:
                registry.register(
                    WebSearchSkill(
                        provider=SerperSearchProvider(
                            http_client=outbound_http,
                            api_key=settings.serper_token,
                        )
                    ),
                    SkillOrigin.BASIC,
                )
            _register_instruction_skills(
                registry, instructions, datasets, external_executor, settings
            )
            _register_dataset_skills(registry, datasets, settings)
            memory = SqlAlchemyMemoryStore(session_factory)
            _register_memory_skills(registry, memory, settings)
            loop = AgentLoop(
                llm_client=llm_client,
                registry=registry,
                max_iterations=settings.agent_max_iterations,
            )
            prompt_provider: PromptProvider = FilePromptProvider(
                files=settings.to_prompt_files(),
                fallback=StaticPromptProvider(),
            )
            manager = ConversationManager(
                config=RunnerConfig(
                    loop=loop,
                    prompts=prompt_provider,
                    router=LLMRouter(
                        llm_client,
                        timeout_seconds=settings.router_timeout_seconds,
                        prompts=prompt_provider,
                    ),
                    max_processes=settings.max_processes,
                ),
                dialogs=dialogs,
                messages=messages,
                tasks=task_store,
            )
            scheduler_task = _start_cron_scheduler(cron_store, manager, settings)
            telegram = _start_telegram(
                settings, manager.get_or_create_runner, dialogs, outbound_http
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
    finally:
        await engine.dispose()


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


async def _seed_instructions(instructions: InstructionService, settings: Settings) -> None:
    """Seed the baseline records and migrate cron tools when embeddings work.

    Seeding needs working embeddings; when no usable backend is configured it
    is skipped so the app still starts (embeddings stay optional until the
    first instructions_search/save call). A failing embeddings backend or
    database must not take the app down: log a warning and start without the
    baseline records (seeding retries on the next restart).
    """
    if not settings.embeddings_configured():
        return
    try:
        await seed_if_empty(instructions)
        await migrate_cron_tools_to_native(instructions)
    except (httpx.HTTPError, LLMResponseError, SQLAlchemyError):
        logger.warning(
            "Instruction seeding failed; starting without baseline records",
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


def _build_reranker(settings: Settings) -> RerankerClient | None:
    """Build the optional cross-encoder reranker (empty model = disabled)."""
    if not settings.reranker_model:
        return None
    return CrossEncoderReranker(RerankerConfig(model=settings.reranker_model))


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
    scheduler: Scheduler = CronScheduler(
        store=store,
        waker=ManagerCronWaker(manager),
        owner=uuid.uuid4().hex,
        config=CronSchedulerConfig(
            poll_interval_seconds=settings.cron_poll_interval_seconds,
            lease_ttl_seconds=settings.cron_lease_ttl_seconds,
            replay_limit=settings.cron_replay_limit,
        ),
    )
    return asyncio.create_task(scheduler.run_forever())


def _start_telegram(
    settings: Settings,
    runner_provider: RunnerProvider,
    dialogs: DialogRepository,
    http_client: httpx.AsyncClient,
) -> tuple[TelegramBridgeRegistry, asyncio.Task[None]] | None:
    """Start the Telegram long-poll adapter when a bot token is configured."""
    if not settings.telegram_bot_token:
        return None
    client = TelegramBotClient(http_client=http_client, token=settings.telegram_bot_token)
    registry = TelegramBridgeRegistry(
        runner_provider=runner_provider,
        client=client,
        edit_throttle_seconds=settings.telegram_edit_throttle_seconds,
    )
    poller = TelegramPoller(
        client=client,
        registry=registry,
        poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
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


def _register_core_skills(
    registry: SkillRegistry,
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    task_store: SqlAlchemyTaskStore,
    cron_store: CronStore,
) -> None:
    """Register the HTTP, background-task and cron skills."""
    registry.register(
        HttpRequestSkill(http_client=outbound_http, guard=guard),
        SkillOrigin.BASIC,
    )
    registry.register(TaskSpawnSkill(), SkillOrigin.BASIC)
    registry.register(TaskListSkill(store=task_store), SkillOrigin.BASIC)
    registry.register(CronCreateSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronListSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronDeleteSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronPauseSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronResumeSkill(store=cron_store), SkillOrigin.BASIC)


def _register_instruction_skills(
    registry: SkillRegistry,
    instructions: InstructionService,
    datasets: DatasetService,
    executor: ExternalCallExecutor,
    settings: Settings,
) -> None:
    """Register the instructions discovery/save and external-call skills."""
    registry.register(
        InstructionsSearchSkill(
            service=instructions,
            default_k=settings.instructions_top_k,
            datasets=datasets,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(InstructionSaveSkill(service=instructions), SkillOrigin.BASIC)
    registry.register(ExternalCallSkill(executor=executor), SkillOrigin.BASIC)


def _register_dataset_skills(
    registry: SkillRegistry,
    datasets: DatasetService,
    settings: Settings,
) -> None:
    """Register the dataset record skills."""
    registry.register(DataPutSkill(service=datasets), SkillOrigin.BASIC)
    registry.register(
        DataQuerySkill(
            service=datasets,
            default_limit=settings.datasets_query_default_limit,
            max_limit=settings.datasets_query_max_limit,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(DataForgetSkill(service=datasets), SkillOrigin.BASIC)


def _register_memory_skills(
    registry: SkillRegistry,
    store: MemoryStore,
    settings: Settings,
) -> None:
    """Register the memory skills over the shared memory store."""
    registry.register(MemoryStoreSkill(store=store), SkillOrigin.BASIC)
    registry.register(
        MemorySearchSkill(
            store=store,
            default_limit=settings.memory_search_default_limit,
            max_limit=settings.memory_search_max_limit,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(MemoryDeleteSkill(store=store), SkillOrigin.BASIC)


app = create_app()
