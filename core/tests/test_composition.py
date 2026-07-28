"""Tests for the reusable composition builders (octoforge_core.composition).

Covers the P5 seams: the default tool set assembled by `build_tool_registry`
(with and without a search provider), port substitution through the builders
(fake SearchProvider, in-memory InstructionStore) and a working
`build_conversation_manager` on an in-memory SQLite database.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.events import Cancelled, Failed, Finished
from octoforge_core.agent.prompts import StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import ConversationEvent
from octoforge_core.composition import (
    RunnerOptions,
    ToolLimits,
    ToolServices,
    ToolStores,
    build_agent_loop,
    build_conversation_manager,
    build_dataset_service,
    build_external_executor,
    build_instruction_service,
    build_runner_config,
    build_tool_registry,
)
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionType,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.search.api import SearchResponse, SearchResult
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "user-1"
CHANNEL = "test"
DIALOG_ID = "dialog-1"
REPLY = "composed hello"
QUESTION = "hello there"
SEARCH_QUERY = "fake query"
FAKE_ANSWER = "fake direct answer"
FAKE_RESULT_TITLE = "Fake result"
SAVED_TITLE = "saved scenario"
SAVED_CONTENT = "do the composed thing"
FIRST_VERSION = 1
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
WAIT_TIMEOUT_SECONDS = 2.0

ALL_BASIC_TOOLS = {
    "http_request",
    "task_create",
    "task_list",
    "task_delete",
    "cron_pause",
    "cron_resume",
    "web_search",
    "recall",
    "endpoint_get",
    "instruction_save",
    "instruction_delete",
    "external_call",
    "data_put",
    "data_query",
    "data_forget",
    "memory_store",
    "memory_delete",
    "history_search",
}

CONTEXT = ToolContext(user_id=USER_ID, channel=CHANNEL, dialog_id=DIALOG_ID)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


class InMemoryInstructionStore:
    """InstructionStore implementation keeping records in a dict (no SQL)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str | None], Instruction] = {}
        self.embeddings: dict[str, tuple[float, ...]] = {}

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        now = utc_now()
        record = Instruction(
            id=f"mem-{len(self.records)}",
            type=draft.kind,
            title=draft.title,
            content=draft.content,
            tags=draft.tags,
            version=FIRST_VERSION,
            usage_count=0,
            success_count=0,
            created_at=now,
            updated_at=now,
            system=draft.system,
            owner_id=draft.owner_id,
        )
        self.records[(draft.kind.value, draft.title, draft.owner_id)] = record
        self.embeddings[record.id] = draft.embedding
        return record

    async def get_by_title(
        self,
        title: str,
        kind: InstructionType | None,
        owner_id: str | None = None,
    ) -> Instruction | None:
        return self.records.get((kind.value if kind is not None else "", title, owner_id)) or next(
            (
                record
                for record in self.records.values()
                if record.title == title and record.owner_id == owner_id
            ),
            None,
        )

    async def get(self, instruction_id: str) -> Instruction | None:
        for record in self.records.values():
            if record.id == instruction_id:
                return record
        return None

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]:
        return [
            EmbeddedInstruction(instruction=record, embedding=self.embeddings[record.id])
            for record in self.records.values()
            if user_id is None or record.owner_id is None or record.owner_id == user_id
        ]

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        pass

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool:
        for key, record in list(self.records.items()):
            if record.id == instruction_id and record.owner_id == owner_id:
                del self.records[key]
                return True
        return False

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        return self.records.pop((kind.value, title, None), None) is not None

    async def list_missing_embeddings(self) -> list[Instruction]:
        return [record for record in self.records.values() if not self.embeddings.get(record.id)]

    async def set_embedding(self, instruction_id: str, embedding: tuple[float, ...]) -> bool:
        if all(record.id != instruction_id for record in self.records.values()):
            return False
        self.embeddings[instruction_id] = embedding
        return True

    async def publish(self, instruction_id: str) -> Instruction | None:
        for key, record in list(self.records.items()):
            if record.id != instruction_id:
                continue
            if record.type is InstructionType.MEMORY:
                return None
            del self.records[key]
            published = Instruction(
                id=record.id,
                type=record.type,
                title=record.title,
                content=record.content,
                tags=record.tags,
                version=record.version,
                usage_count=record.usage_count,
                success_count=record.success_count,
                created_at=record.created_at,
                updated_at=utc_now(),
                system=record.system,
                owner_id=None,
            )
            self.records[(record.type.value, record.title, None)] = published
            return published
        return None

    async def list_system(self) -> list[Instruction]:
        return [record for record in self.records.values() if record.system]


class FakeSearchProvider:
    """SearchProvider stub returning one fixed result and answer."""

    async def search(self, query: str, num_results: int) -> SearchResponse:
        return SearchResponse(
            results=(
                SearchResult(
                    title=FAKE_RESULT_TITLE,
                    link="https://fake.example",
                    snippet="fake snippet",
                ),
            ),
            answer=FAKE_ANSWER,
        )


class ScriptedLLM:
    """LLMClient stub answering every stream with the same final reply."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=REPLY)
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content=REPLY))


class PassThroughRouter:
    """MessageRouter stub never starting background processes."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


def default_limits() -> ToolLimits:
    return ToolLimits(
        instructions_top_k=5,
        datasets_query_default_limit=50,
        datasets_query_max_limit=200,
        history_search_default_limit=20,
        history_search_max_limit=100,
    )


def build_registry(
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    search_provider: FakeSearchProvider | None = None,
    instruction_store: InMemoryInstructionStore | None = None,
) -> ToolRegistry:
    """Assemble a tool registry from the builders over in-memory/SQLite parts."""
    store = instruction_store if instruction_store is not None else InMemoryInstructionStore()
    instructions = build_instruction_service(store, LenientEmbedder())
    summary_store = SqlAlchemySummaryStore(session_factory)
    guard = SsrfGuard()
    return build_tool_registry(
        http_client,
        guard,
        stores=ToolStores(
            tasks=InMemoryTaskStore(),
            cron=SqlAlchemyCronStore(session_factory),
            archive=summary_store,
            summaries=summary_store,
        ),
        services=ToolServices(
            instructions=instructions,
            datasets=build_dataset_service(
                SqlAlchemyDatasetStore(session_factory),
                LenientEmbedder(),
            ),
            executor=build_external_executor(
                service=instructions,
                http_client=http_client,
                guard=guard,
            ),
            search_provider=search_provider,
        ),
        limits=default_limits(),
    )


async def test_build_tool_registry_registers_all_basic_tools(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    registry = build_registry(http_client, session_factory, search_provider=FakeSearchProvider())
    assert {spec.name for spec in registry.specs()} == ALL_BASIC_TOOLS


async def test_build_tool_registry_skips_web_search_without_provider(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    registry = build_registry(http_client, session_factory)
    assert {spec.name for spec in registry.specs()} == ALL_BASIC_TOOLS - {"web_search"}


async def test_skills_run_over_substituted_ports(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    instruction_store = InMemoryInstructionStore()
    registry = build_registry(
        http_client,
        session_factory,
        search_provider=FakeSearchProvider(),
        instruction_store=instruction_store,
    )
    instructions = build_instruction_service(instruction_store, LenientEmbedder())
    await instructions.save(USER_ID, InstructionType.SKILL, SAVED_TITLE, SAVED_CONTENT)

    search_output = await registry.get("web_search").execute({"query": SEARCH_QUERY}, CONTEXT)
    assert FAKE_ANSWER in search_output
    search_hits = await registry.get("recall").execute({"query": SAVED_TITLE}, CONTEXT)
    assert SAVED_TITLE in search_hits


async def test_build_conversation_manager_runs_a_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM()
    manager = build_conversation_manager(
        config=build_runner_config(
            build_agent_loop(llm, ToolRegistry(), max_iterations=MAX_ITERATIONS),
            StaticPromptProvider(),
            PassThroughRouter(),
            NoopContextCompactor(),
            options=RunnerOptions(max_processes=MAX_PROCESSES),
        ),
        dialogs=SqlAlchemyDialogRepository(session_factory),
        messages=SqlAlchemyMessageRepository(session_factory),
        tasks=InMemoryTaskStore(),
        exchanges=SqlAlchemyExchangeRepository(session_factory),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    await runner.submit(QUESTION)

    events: list[ConversationEvent] = []
    while True:
        event = await asyncio.wait_for(queue.get(), WAIT_TIMEOUT_SECONDS)
        events.append(event)
        if isinstance(event.payload, (Finished, Failed, Cancelled)):
            break
    assert isinstance(events[-1].payload, Finished)
    assert events[-1].payload.message.content == REPLY
    await runner.stop()
