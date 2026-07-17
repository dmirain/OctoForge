"""FastAPI application factory and composition root."""

import asyncio
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
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.seed import seed_if_empty
from octoforge_core.llm.embeddings import OpenAIEmbeddingClient
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.memory.api import MemoryStore
from octoforge_core.memory.store import SqlAlchemyMemoryStore
from octoforge_core.net.external import ExternalCallExecutor
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
from octoforge_core.tasks.runner import TaskRunner

from octoforge_web.api.dialog import router as dialog_router
from octoforge_web.config import Settings

STATIC_DIR = Path(__file__).parent / "static"
APP_TITLE = "OctoForge"
HEALTH_STATUS = "ok"
WEB_CHANNEL = "web"


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
                # Seeding needs the embeddings endpoint; without a configured key
                # it is skipped so the app still starts (embeddings stay optional
                # until the first instructions_search/save call).
                if resolved_settings.embedding_api_key:
                    await seed_if_empty(instructions)
                guard = SsrfGuard()
                external_executor = ExternalCallExecutor(
                    service=instructions,
                    http_client=outbound_http,
                    guard=guard,
                    auth_whitelist=resolved_settings.to_external_call_auth_whitelist(),
                )
                registry = SkillRegistry()
                registry.register(
                    HttpRequestSkill(http_client=outbound_http, guard=guard),
                    SkillOrigin.BASIC,
                )
                registry.register(TaskSpawnSkill(store=task_store), SkillOrigin.BASIC)
                registry.register(TaskListSkill(store=task_store), SkillOrigin.BASIC)
                registry.register(
                    InstructionsSearchSkill(
                        service=instructions,
                        default_k=resolved_settings.instructions_top_k,
                        datasets=datasets,
                    ),
                    SkillOrigin.BASIC,
                )
                registry.register(InstructionSaveSkill(service=instructions), SkillOrigin.BASIC)
                registry.register(
                    ExternalCallSkill(executor=external_executor),
                    SkillOrigin.BASIC,
                )
                registry.register(DataPutSkill(service=datasets), SkillOrigin.BASIC)
                registry.register(
                    DataQuerySkill(
                        service=datasets,
                        default_limit=resolved_settings.datasets_query_default_limit,
                        max_limit=resolved_settings.datasets_query_max_limit,
                    ),
                    SkillOrigin.BASIC,
                )
                registry.register(DataForgetSkill(service=datasets), SkillOrigin.BASIC)
                memory = SqlAlchemyMemoryStore(session_factory)
                _register_memory_skills(registry, memory, resolved_settings)
                loop = AgentLoop(
                    llm_client=llm_client,
                    registry=registry,
                    max_iterations=resolved_settings.agent_max_iterations,
                )
                manager = ConversationManager(
                    loop=loop,
                    system_prompt=DEFAULT_SYSTEM_PROMPT,
                    dialogs=dialogs,
                    messages=messages,
                    tasks=task_store,
                )
                task_runner = TaskRunner(
                    store=task_store,
                    llm_client=llm_client,
                    registry=registry,
                    on_task_done=manager.notify_task_done,
                )
                runner_task = asyncio.create_task(task_runner.run_forever())
                app.state.settings = resolved_settings
                app.state.conversation_manager = manager
                app.state.channel = WEB_CHANNEL
                yield
                runner_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runner_task
        finally:
            await engine.dispose()

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": HEALTH_STATUS}

    app.include_router(dialog_router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


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
