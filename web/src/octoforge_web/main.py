"""FastAPI application factory and composition root."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from octoforge_core import (
    AgentLoop,
    ConversationManager,
    DialogRepository,
    MessageRepository,
    SkillOrigin,
    SkillRegistry,
    SqlAlchemyTaskStore,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.agent.prompts import DEFAULT_SYSTEM_PROMPT
from octoforge_core.agent.router import LLMRouter
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.cron.api import CronStore
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.cron.waker import ManagerCronWaker
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.seed import seed_cron_tools_if_absent, seed_if_empty
from octoforge_core.llm.embeddings import OpenAIEmbeddingClient
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.memory.api import MemoryStore
from octoforge_core.memory.store import SqlAlchemyMemoryStore
from octoforge_core.net.external import ExternalCallAuth, ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
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

from octoforge_web.api.cron import router as cron_router
from octoforge_web.api.dialog import router as dialog_router
from octoforge_web.config import Settings

STATIC_DIR = Path(__file__).parent / "static"
APP_TITLE = "OctoForge"
HEALTH_STATUS = "ok"
WEB_CHANNEL = "web"
USER_ID_HEADER = "X-User-Id"
USER_ID_HEADER_VALUE_TEMPLATE = "{user_id}"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application with all dependencies wired."""
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved_settings.database_url)
        await init_db(engine)
        session_factory = create_session_factory(engine)
        dialogs = DialogRepository(session_factory)
        messages = MessageRepository(session_factory)
        task_store = SqlAlchemyTaskStore(session_factory)
        cron_store = SqlAlchemyCronStore(session_factory)
        try:
            async with (
                httpx.AsyncClient(base_url=resolved_settings.llm_base_url) as llm_http,
                httpx.AsyncClient(base_url=resolved_settings.embedding_base_url) as embed_http,
                httpx.AsyncClient() as outbound_http,
            ):
                llm_client = OpenAICompatibleClient(
                    http_client=llm_http,
                    config=resolved_settings.to_llm_config(),
                )
                embedder = OpenAIEmbeddingClient(
                    http_client=embed_http,
                    config=resolved_settings.to_embedding_config(),
                )
                instructions = LocalInstructionService(session_factory, embedder)
                datasets = LocalDatasetService(session_factory, embedder)
                await _seed_instructions(instructions, resolved_settings)
                # The app's own base URL is allowlisted so tool records can
                # target our loopback HTTP API (cron jobs) past the SSRF guard.
                guard = SsrfGuard(allowed_prefixes=(resolved_settings.self_base_url,))
                external_executor = ExternalCallExecutor(
                    service=instructions,
                    http_client=outbound_http,
                    guard=guard,
                    auth_whitelist=_external_call_whitelist(resolved_settings),
                )
                registry = SkillRegistry()
                _register_core_skills(registry, outbound_http, guard, task_store)
                _register_instruction_skills(
                    registry, instructions, datasets, external_executor, resolved_settings
                )
                _register_dataset_skills(registry, datasets, resolved_settings)
                memory = SqlAlchemyMemoryStore(session_factory)
                _register_memory_skills(registry, memory, resolved_settings)
                loop = AgentLoop(
                    llm_client=llm_client,
                    registry=registry,
                    max_iterations=resolved_settings.agent_max_iterations,
                )
                manager = ConversationManager(
                    config=RunnerConfig(
                        loop=loop,
                        system_prompt=DEFAULT_SYSTEM_PROMPT,
                        router=LLMRouter(
                            llm_client,
                            timeout_seconds=resolved_settings.router_timeout_seconds,
                        ),
                        max_processes=resolved_settings.max_processes,
                    ),
                    dialogs=dialogs,
                    messages=messages,
                    tasks=task_store,
                )
                app.state.settings = resolved_settings
                app.state.conversation_manager = manager
                app.state.channel = WEB_CHANNEL
                app.state.cron_store = cron_store
                scheduler_task = _start_cron_scheduler(cron_store, manager, resolved_settings)
                try:
                    yield
                finally:
                    scheduler_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await scheduler_task
        finally:
            await engine.dispose()

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": HEALTH_STATUS}

    app.include_router(dialog_router)
    app.include_router(cron_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def _seed_instructions(instructions: InstructionService, settings: Settings) -> None:
    """Seed the baseline and cron tool records when embeddings are configured.

    Seeding needs the embeddings endpoint; without a configured key it is
    skipped so the app still starts (embeddings stay optional until the first
    instructions_search/save call).
    """
    if not settings.embedding_api_key:
        return
    await seed_if_empty(instructions)
    await seed_cron_tools_if_absent(instructions, settings.self_base_url)


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
    scheduler = CronScheduler(
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


def _register_core_skills(
    registry: SkillRegistry,
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    task_store: SqlAlchemyTaskStore,
) -> None:
    """Register the HTTP and background-task skills."""
    registry.register(
        HttpRequestSkill(http_client=outbound_http, guard=guard),
        SkillOrigin.BASIC,
    )
    registry.register(TaskSpawnSkill(), SkillOrigin.BASIC)
    registry.register(TaskListSkill(store=task_store), SkillOrigin.BASIC)


def _register_instruction_skills(
    registry: SkillRegistry,
    instructions: InstructionService,
    datasets: LocalDatasetService,
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
    datasets: LocalDatasetService,
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
