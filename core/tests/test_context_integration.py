"""Integration: ConversationRunner process branches go through the compactor."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.events import ProcessCompleted
from octoforge_core.agent.loop import AgentLoop, AgentLoopConfig
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import (
    ConversationEvent,
    ConversationManager,
    DialogSubmission,
    ManagerStores,
    OwnershipConfig,
    RunnerConfig,
)
from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.compactor import (
    CompactorConfig,
    CompactorServices,
    LlmContextCompactor,
)
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import MessageAppend
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.tasks.store import SqlAlchemyTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolSpec
from octoforge_core.tools.registry import ToolRegistry

USER_ID = "user-1"
CHANNEL = "web"
PROMPT = "test system prompt"
TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
HOT_MAX_CHARS = 30
COMPACT_TARGET_CHARS = 35
COMPACTED_SEQ_TO = 2
OLD_MESSAGES = ["old message one", "old message two"]
RECENT_MESSAGES = ["old message three", "old message four", "old message five"]
FIRST_SUMMARY_REPLY = "TOPICS: alpha\nSUMMARY:\ncompressed one"
SECOND_SUMMARY_REPLY = "TOPICS: beta\nSUMMARY:\ncompressed two"


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # A file database, not :memory:: the in-memory StaticPool shares a single
    # connection, and a background compaction session closing mid-finalize
    # would roll back the pump's uncommitted persist.
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


class PassthroughRouter:
    """MessageRouter stub: every message starts a new process."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


class DialogLLM:
    """LLMClient stub: stream replays dialog replies, complete summary replies."""

    def __init__(self, stream_replies: list[str], complete_replies: list[str]) -> None:
        self._stream_replies = list(stream_replies)
        self._complete_replies = list(complete_replies)
        self.stream_requests: list[list[ChatMessage]] = []
        self.complete_requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self.complete_requests.append(list(messages))
        return Completion(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=self._complete_replies.pop(0))
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_requests.append(list(messages))
        content = self._stream_replies.pop(0)
        yield LlmTextDelta(text=content)
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content=content))


def make_manager(
    llm: DialogLLM,
    session_factory: async_sessionmaker[AsyncSession],
    compactor: LlmContextCompactor,
) -> ConversationManager:
    loop = AgentLoop(llm, ToolRegistry(), AgentLoopConfig(MAX_ITERATIONS))
    config = RunnerConfig(
        loop=loop,
        prompts=StaticPromptProvider({SYSTEM_PROMPT_NAME: PROMPT}),
        router=PassthroughRouter(),
        max_processes=MAX_PROCESSES,
        compactor=compactor,
    )
    return ConversationManager(
        config=config,
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(session_factory),
            messages=SqlAlchemyMessageRepository(session_factory),
            tasks=SqlAlchemyTaskStore(session_factory),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
            uow=UnitOfWork(session_factory),
        ),
        ownership=OwnershipConfig(node_id="test-node"),
    )


async def prefill(session_factory: async_sessionmaker[AsyncSession], texts: list[str]) -> Dialog:
    """Persist a dialog with user messages, as a previous run would have left it."""
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)
    repository = SqlAlchemyMessageRepository(session_factory)
    for text in texts:
        await repository.append(
            MessageAppend(dialog.id, ChatMessage(role=MessageRole.USER, content=text))
        )
    return dialog


async def add_summary(
    store: SqlAlchemySummaryStore,
    dialog_id: str,
    seq_range: tuple[int, int],
) -> None:
    seq_from, seq_to = seq_range
    await store.create(
        DialogueSummary(
            id=uuid.uuid4().hex,
            dialog_id=dialog_id,
            seq_from=seq_from,
            seq_to=seq_to,
            topics=("travel",),
            content="trip plans",
            created_at=utc_now(),
        )
    )


async def wait_completed(queue: asyncio.Queue[ConversationEvent]) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=TIMEOUT_SECONDS)
        if isinstance(event.payload, ProcessCompleted):
            return


async def wait_for_condition(predicate: Callable[[], bool]) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)


async def wait_for_summaries(
    store: SqlAlchemySummaryStore, dialog_id: str
) -> list[DialogueSummary]:
    summaries: list[DialogueSummary] = []

    async def _wait() -> None:
        nonlocal summaries
        while not summaries:
            summaries = await store.list_for_dialog(dialog_id)
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)
    return summaries


def branch_contents(branch: list[ChatMessage]) -> list[str]:
    return [message.content for message in branch]


async def submit_and_wait(
    submit: Callable[[DialogSubmission], Awaitable[None]],
    queue: asyncio.Queue[ConversationEvent],
    prompt: str,
) -> None:
    await submit(DialogSubmission(prompt))
    await wait_completed(queue)


def assert_compacted_branch(contents: list[str]) -> None:
    assert any("compressed one" in content for content in contents)  # the topics block
    assert "old message three" in contents  # the hot tail stays verbatim
    assert "fresh question one" in contents
    assert "answer one" in contents
    assert any("fresh question two" in content for content in contents)  # date-enveloped tail
    assert "old message one" not in contents
    assert "old message two" not in contents


async def test_branch_after_restart_has_topics_block_and_fresh_tail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await prefill(session_factory, [*OLD_MESSAGES, "recent one", "recent two"])
    store = SqlAlchemySummaryStore(session_factory)
    await add_summary(store, dialog.id, (1, 2))
    llm = DialogLLM(stream_replies=["answer"], complete_replies=[])
    compactor = LlmContextCompactor(CompactorServices(store, store, llm), CompactorConfig())
    manager = make_manager(llm, session_factory, compactor)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit(DialogSubmission("fresh question"))
    await wait_completed(queue)

    contents = branch_contents(llm.stream_requests[0])
    assert any("trip plans" in content and "travel" in content for content in contents)
    assert "recent one" in contents
    assert "recent two" in contents
    assert any("fresh question" in content for content in contents)  # date-enveloped tail
    assert "old message one" not in contents  # compacted: never verbatim in the branch
    assert "old message two" not in contents
    assert llm.complete_requests == []  # below the limit: no compaction ran


async def test_long_dialog_compacts_and_the_next_branch_uses_the_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await prefill(session_factory, [*OLD_MESSAGES, *RECENT_MESSAGES])
    store = SqlAlchemySummaryStore(session_factory)
    llm = DialogLLM(
        stream_replies=["answer one", "answer two"],
        complete_replies=[FIRST_SUMMARY_REPLY, SECOND_SUMMARY_REPLY],
    )
    compactor = LlmContextCompactor(
        CompactorServices(store, store, llm),
        CompactorConfig(
            hot_max_chars=HOT_MAX_CHARS,
            compact_target_chars=COMPACT_TARGET_CHARS,
        ),
    )
    manager = make_manager(llm, session_factory, compactor)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await submit_and_wait(runner.submit, queue, "fresh question one")
    await wait_for_condition(lambda: llm.complete_requests != [])
    summaries = await wait_for_summaries(store, dialog.id)
    assert [(s.seq_from, s.seq_to) for s in summaries] == [(1, COMPACTED_SEQ_TO)]
    assert summaries[0].topics == ("alpha",)
    assert summaries[0].content == "compressed one"

    await submit_and_wait(runner.submit, queue, "fresh question two")

    contents = branch_contents(llm.stream_requests[-1])
    assert_compacted_branch(contents)
