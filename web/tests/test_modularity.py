"""Acceptance modularity scenario: a minimal third-party composition root.

Builds a ConversationManager the way an installer would — from the reusable
core builders (`octoforge_core.composition`), not `main.runtime()` —
overriding the system and router prompts from files, substituting a fake
SearchProvider and an in-memory InstructionStore, then runs a dialog through
it. This is the acceptance check of the modularity roadmap (P1-P3, P5): the
seams are replaced without touching the core and without copying the default
composition root.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from octoforge_core.agent.events import Cancelled, Failed, Finished, ToolCallCompleted
from octoforge_core.agent.prompts import (
    ROUTER_PROMPT_NAME,
    SYSTEM_PROMPT_NAME,
    StaticPromptProvider,
)
from octoforge_core.agent.router import ROUTE_TOOL_NAME
from octoforge_core.agent.runner import ConversationEvent, ConversationManager
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
    build_router,
    build_runner_config,
    build_tool_registry,
)
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionService,
    InstructionType,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.search.api import SearchResponse, SearchResult
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolSpec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.prompts import FilePromptProvider

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "installer-user"
CHANNEL = "installer-channel"
FIRST_QUESTION = "first question"
SECOND_QUESTION = "second question"
FINAL_REPLY = "all done"
SAVED_TITLE = "saved scenario"
SAVED_CONTENT = "do the installer thing"
SEARCH_QUERY = "fake query"
FAKE_ANSWER = "fake direct answer"
FAKE_RESULT_TITLE = "Fake result"
CUSTOM_SYSTEM_PROMPT = "CUSTOM SYSTEM PROMPT FROM FILE"
CUSTOM_ROUTER_PROMPT = "CUSTOM ROUTER PROMPT FROM FILE (limit {limit}):\n{processes}"
MAX_ITERATIONS = 5
MAX_PROCESSES = 5
DEFAULT_K = 5
QUERY_LIMIT = 50
ROUTER_TIMEOUT_SECONDS = 5.0
WAIT_TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
FIRST_CALL = 1
SECOND_CALL = 2
ROUTER_CALLS_ONE = 1
FIRST_VERSION = 1


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

    async def publish(self, instruction_id: str) -> Instruction | None:
        for key, record in list(self.records.items()):
            if record.id != instruction_id:
                continue
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


class RootLLM:
    """LLMClient stub scripting the dialog and recording every request.

    complete() serves the router (always inject); stream() drives the dialog:
    the first call waits on a gate (so the second user message meets an active
    process and exercises the router), then asks for web_search, then for
    recall, then finishes with the final reply.
    """

    def __init__(self) -> None:
        self.stream_requests: list[list[ChatMessage]] = []
        self.complete_requests: list[list[ChatMessage]] = []
        self.first_stream_gate = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self.complete_requests.append(list(messages))
        return Completion(
            message=ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=(
                    ToolCall(
                        id="route-1",
                        name=ROUTE_TOOL_NAME,
                        arguments={"ops": [{"action": "inject", "target_id": None}]},
                    ),
                ),
            )
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_requests.append(list(messages))
        call_number = len(self.stream_requests)
        if call_number == FIRST_CALL:
            await self.first_stream_gate.wait()
            yield StreamFinished(message=_tool_call("call-search", "web_search", SEARCH_QUERY))
        elif call_number == SECOND_CALL:
            yield StreamFinished(message=_tool_call("call-instructions", "recall", SAVED_TITLE))
        else:
            yield LlmTextDelta(text=FINAL_REPLY)
            yield StreamFinished(
                message=ChatMessage(role=MessageRole.ASSISTANT, content=FINAL_REPLY)
            )


def _tool_call(call_id: str, name: str, query: str) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=call_id, name=name, arguments={"query": query}),),
    )


@dataclass(slots=True)
class ThirdPartyRoot:
    """The installer's composition: manager plus the substituted components."""

    manager: ConversationManager
    llm: RootLLM
    instructions: InstructionService


def build_third_party_root(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    prompt_dir: Path,
) -> ThirdPartyRoot:
    """Assemble a ConversationManager from the core builders (no main.py)."""
    system_file = prompt_dir / "system.txt"
    system_file.write_text(CUSTOM_SYSTEM_PROMPT, encoding="utf-8")
    router_file = prompt_dir / "router.txt"
    router_file.write_text(CUSTOM_ROUTER_PROMPT, encoding="utf-8")
    prompts = FilePromptProvider(
        files={SYSTEM_PROMPT_NAME: system_file, ROUTER_PROMPT_NAME: router_file},
        fallback=StaticPromptProvider(),
    )
    instructions = build_instruction_service(InMemoryInstructionStore(), LenientEmbedder())
    summary_store = SqlAlchemySummaryStore(session_factory)
    guard = SsrfGuard()
    registry = build_tool_registry(
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
            search_provider=FakeSearchProvider(),
        ),
        limits=ToolLimits(
            instructions_top_k=DEFAULT_K,
            datasets_query_default_limit=QUERY_LIMIT,
            datasets_query_max_limit=QUERY_LIMIT,
            history_search_default_limit=QUERY_LIMIT,
            history_search_max_limit=QUERY_LIMIT,
        ),
    )
    llm = RootLLM()
    manager = build_conversation_manager(
        config=build_runner_config(
            build_agent_loop(llm, registry, max_iterations=MAX_ITERATIONS),
            prompts,
            build_router(llm, prompts, timeout_seconds=ROUTER_TIMEOUT_SECONDS),
            NoopContextCompactor(),
            options=RunnerOptions(max_processes=MAX_PROCESSES),
        ),
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=InMemoryTaskStore(),
    )
    return ThirdPartyRoot(manager=manager, llm=llm, instructions=instructions)


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


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Poll the predicate until it holds or the wait budget runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_TIMEOUT_SECONDS
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition was not met in time")
        await asyncio.sleep(POLL_SECONDS)


async def collect_until_terminal(
    queue: asyncio.Queue[ConversationEvent],
) -> list[ConversationEvent]:
    """Drain the subscription queue up to and including the terminal event."""
    events: list[ConversationEvent] = []
    while True:
        event = await asyncio.wait_for(queue.get(), WAIT_TIMEOUT_SECONDS)
        events.append(event)
        if isinstance(event.payload, (Finished, Failed, Cancelled)):
            return events


def tool_outputs(events: list[ConversationEvent]) -> list[str]:
    return [
        event.payload.output for event in events if isinstance(event.payload, ToolCallCompleted)
    ]


async def test_third_party_root_overrides_prompts_search_and_instruction_store(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    root = build_third_party_root(session_factory, http_client, tmp_path)
    await root.instructions.save(USER_ID, InstructionType.SKILL, SAVED_TITLE, SAVED_CONTENT)
    runner = await root.manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit(FIRST_QUESTION)
    await wait_until(lambda: len(root.llm.stream_requests) == 1)
    await runner.submit(SECOND_QUESTION)
    # no LLM router call for the first message (no active processes); the
    # second message routes over the first question's process
    await wait_until(lambda: len(root.llm.complete_requests) == ROUTER_CALLS_ONE)
    root.llm.first_stream_gate.set()
    events = await collect_until_terminal(queue)

    # the conversation system prompt came from the installer's file
    first_system = root.llm.stream_requests[0][0]
    assert first_system.role is MessageRole.SYSTEM
    assert first_system.content.startswith(CUSTOM_SYSTEM_PROMPT)
    # the router prompt came from the installer's file too; its only call is
    # for the second message and lists the first question's process
    router_system, router_user = root.llm.complete_requests[0]
    assert "CUSTOM ROUTER PROMPT FROM FILE" in router_system.content
    assert router_user == ChatMessage(role=MessageRole.USER, content=SECOND_QUESTION)
    assert router_system.role is MessageRole.SYSTEM
    assert FIRST_QUESTION in router_system.content
    # the tools ran over the substituted provider and instruction store
    outputs = tool_outputs(events)
    assert any(FAKE_ANSWER in output for output in outputs)
    assert any(SAVED_TITLE in output for output in outputs)
    # the dialog ran to a final answer
    assert isinstance(events[-1].payload, Finished)
    assert events[-1].payload.message.content == FINAL_REPLY
