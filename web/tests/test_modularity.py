"""Acceptance modularity scenario: a minimal third-party composition root.

Builds a ConversationManager the way an installer would — with its own
wiring, not `main.runtime()` — overriding the system and router prompts
from files, substituting a fake SearchProvider and an in-memory
InstructionStore, then runs a dialog through it. This is the acceptance
check of the modularity roadmap (P1-P3): the seams are replaced without
touching the core.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from octoforge_core.agent.events import Cancelled, Failed, Finished, ToolCallCompleted
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import (
    ROUTER_PROMPT_NAME,
    SYSTEM_PROMPT_NAME,
    StaticPromptProvider,
)
from octoforge_core.agent.router import ROUTE_TOOL_NAME, LLMRouter
from octoforge_core.agent.runner import ConversationEvent, ConversationManager, RunnerConfig
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.instructions.api import EmbeddedInstruction, Instruction, InstructionType
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.search.api import SearchResponse, SearchResult
from octoforge_core.skills.base import SkillOrigin, SkillSpec
from octoforge_core.skills.basic.instructions_search import InstructionsSearchSkill
from octoforge_core.skills.basic.web_search import WebSearchSkill
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.time import utc_now
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
ROUTER_TIMEOUT_SECONDS = 5.0
WAIT_TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
FIRST_CALL = 1
SECOND_CALL = 2
FIRST_VERSION = 1


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


class InMemoryInstructionStore:
    """InstructionStore implementation keeping records in a dict (no SQL)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], Instruction] = {}
        self.embeddings: dict[str, tuple[float, ...]] = {}

    async def upsert(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...],
        embedding: tuple[float, ...],
    ) -> Instruction:
        now = utc_now()
        record = Instruction(
            id=f"mem-{len(self.records)}",
            type=kind,
            title=title,
            content=content,
            tags=tags,
            version=FIRST_VERSION,
            usage_count=0,
            success_count=0,
            created_at=now,
            updated_at=now,
        )
        self.records[(kind.value, title)] = record
        self.embeddings[record.id] = embedding
        return record

    async def get_by_title(self, title: str, kind: InstructionType | None) -> Instruction | None:
        return self.records.get((kind.value if kind is not None else "", title)) or next(
            (record for record in self.records.values() if record.title == title),
            None,
        )

    async def list_with_embeddings(self) -> list[EmbeddedInstruction]:
        return [
            EmbeddedInstruction(instruction=record, embedding=self.embeddings[record.id])
            for record in self.records.values()
        ]

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        pass

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        return self.records.pop((kind.value, title), None) is not None


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
    instructions_search, then finishes with the final reply.
    """

    def __init__(self) -> None:
        self.stream_requests: list[list[ChatMessage]] = []
        self.complete_requests: list[list[ChatMessage]] = []
        self.first_stream_gate = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        self.complete_requests.append(list(messages))
        return ChatMessage(
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

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_requests.append(list(messages))
        call_number = len(self.stream_requests)
        if call_number == FIRST_CALL:
            await self.first_stream_gate.wait()
            yield StreamFinished(message=_tool_call("call-search", "web_search", SEARCH_QUERY))
        elif call_number == SECOND_CALL:
            yield StreamFinished(
                message=_tool_call("call-instructions", "instructions_search", SAVED_TITLE)
            )
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
    instructions: LocalInstructionService


def build_third_party_root(
    session_factory: async_sessionmaker[AsyncSession],
    prompt_dir: Path,
) -> ThirdPartyRoot:
    """Assemble a ConversationManager from installer-owned parts (no main.py)."""
    system_file = prompt_dir / "system.txt"
    system_file.write_text(CUSTOM_SYSTEM_PROMPT, encoding="utf-8")
    router_file = prompt_dir / "router.txt"
    router_file.write_text(CUSTOM_ROUTER_PROMPT, encoding="utf-8")
    prompts = FilePromptProvider(
        files={SYSTEM_PROMPT_NAME: system_file, ROUTER_PROMPT_NAME: router_file},
        fallback=StaticPromptProvider(),
    )
    instructions = LocalInstructionService(InMemoryInstructionStore(), LenientEmbedder())
    registry = SkillRegistry()
    registry.register(WebSearchSkill(provider=FakeSearchProvider()), SkillOrigin.BASIC)
    registry.register(
        InstructionsSearchSkill(service=instructions, default_k=DEFAULT_K),
        SkillOrigin.BASIC,
    )
    llm = RootLLM()
    manager = ConversationManager(
        config=RunnerConfig(
            loop=AgentLoop(llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS),
            prompts=prompts,
            router=LLMRouter(llm, timeout_seconds=ROUTER_TIMEOUT_SECONDS, prompts=prompts),
            max_processes=MAX_PROCESSES,
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
    tmp_path: Path,
) -> None:
    root = build_third_party_root(session_factory, tmp_path)
    await root.instructions.save(InstructionType.SKILL, SAVED_TITLE, SAVED_CONTENT)
    runner = await root.manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit(FIRST_QUESTION)
    await wait_until(lambda: len(root.llm.stream_requests) == 1)
    await runner.submit(SECOND_QUESTION)
    await wait_until(lambda: len(root.llm.complete_requests) == 1)
    root.llm.first_stream_gate.set()
    events = await collect_until_terminal(queue)

    # the conversation system prompt came from the installer's file
    first_system = root.llm.stream_requests[0][0]
    assert first_system.role is MessageRole.SYSTEM
    assert first_system.content.startswith(CUSTOM_SYSTEM_PROMPT)
    # the router prompt came from the installer's file too
    router_system = root.llm.complete_requests[0][0]
    assert router_system.role is MessageRole.SYSTEM
    assert "CUSTOM ROUTER PROMPT FROM FILE" in router_system.content
    assert FIRST_QUESTION in router_system.content
    # the skills ran over the substituted provider and instruction store
    outputs = tool_outputs(events)
    assert any(FAKE_ANSWER in output for output in outputs)
    assert any(SAVED_TITLE in output for output in outputs)
    # the dialog ran to a final answer
    assert isinstance(events[-1].payload, Finished)
    assert events[-1].payload.message.content == FINAL_REPLY
