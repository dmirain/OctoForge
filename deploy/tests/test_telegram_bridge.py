"""Tests for the Telegram bridge rendering dialog events into a chat."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationManager,
    MessageKind,
    MessageRole,
    MessageSource,
    ToolRegistry,
    ToolSpec,
)
from octoforge_core.agent.events import Cancelled, Finished, ProcessStarted, TextDelta
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import (
    ConversationRunner,
    ManagerStores,
    OwnershipConfig,
    RunnerConfig,
)
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ToolCall
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.ports import LLMClient
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext
from octoforge_telegram import bridge as bridge_module
from octoforge_telegram.bridge import (
    CANCELLED_LINE,
    REPLY_TARGET_MAP_SIZE,
    TOOL_LINE_TEMPLATE,
    RunnerProvider,
    TelegramBridge,
    TelegramBridgeOptions,
)
from octoforge_telegram.client import (
    MAX_MESSAGE_LENGTH,
    MAX_RICH_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    USER_ID_PREFIX,
    TelegramApiError,
)
from octoforge_telegram.drafts import SqlAlchemyDraftStore
from octoforge_telegram.poller import TelegramBridgeRegistry
from octoforge_telegram.schema import TelegramSurfaceBase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHAT_ID = 12345
TELEGRAM_USER_ID = f"{USER_ID_PREFIX}{CHAT_ID}"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
NO_THROTTLE = 0.0
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
EXCHANGE_ID = "ex-1"
WAIT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.01
REPLY = "final answer"
PARTIAL = "partial"
ECHO_TOOL = "echo_tool"
ECHO_OUTPUT = "echo output"
CALL_ID = "call-1"
LONG_REPLY_TAIL = "x" * 100
CRON_TITLE = "morning report"
CRON_RESULT = "the report is ready"
EXPECTED_MESSAGE_COUNT = 2
QUESTION_MESSAGE_ID = 777
# the plain-text send limit answers used to be cut at
PLAIN_TEXT_LIMIT = MAX_MESSAGE_LENGTH


class FakeTelegramClient:
    """TelegramClient stub recording the outbound calls."""

    def __init__(self) -> None:
        # answers go out as Rich Messages: (chat_id, markdown)
        self.sent: list[tuple[int, str]] = []
        # reply targets, one per send (None = plain message)
        self.replies: list[int | None] = []
        self.edited: list[tuple[int, int, str]] = []
        self.actions: list[tuple[int, str]] = []
        # errors popped (in order) and raised by the next edit_message_rich calls
        self.edit_failures: list[Exception] = []
        self._next_message_id = 0

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[Any]:
        raise NotImplementedError

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        # plain notices only (the poller's greetings and refusals)
        self._next_message_id += 1
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        raise NotImplementedError

    async def send_rich_message(
        self, chat_id: int, markdown: str, reply_to_message_id: int | None = None
    ) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, markdown))
        self.replies.append(reply_to_message_id)
        return self._next_message_id

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        if self.edit_failures:
            raise self.edit_failures.pop(0)
        self.edited.append((chat_id, message_id, markdown))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))

    def current_text(self) -> str:
        """The markdown of the single message rendered so far (last edit or first send)."""
        if self.edited:
            return self.edited[-1][2]
        assert self.sent, "no message rendered yet"
        return self.sent[-1][1]


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as whole-delta streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)

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
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class ChunkedLLM:
    """LLMClient stub streaming one reply chunk by chunk."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

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
        for chunk in self._chunks:
            yield LlmTextDelta(text=chunk)
        yield StreamFinished(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="".join(self._chunks))
        )


class StallingLLM:
    """LLMClient stub emitting partial text and stalling until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

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
        yield LlmTextDelta(text=PARTIAL)
        await self.release.wait()
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content="full"))


class FailingLLM:
    """LLMClient stub raising mid-stream."""

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
        yield LlmTextDelta(text=PARTIAL)
        raise RuntimeError("boom")


class EchoTool:
    """Tool stub returning a fixed output."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=ECHO_TOOL, description="echo", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return ECHO_OUTPUT


class PassthroughRouter:
    """MessageRouter stub always starting a new process."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


class FakeRunner:
    """ConversationRunner stub recording `submit()` calls (reply-routing tests only)."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, str | None, str | None]] = []
        self.kinds: list[MessageKind] = []

    def subscribe(self) -> asyncio.Queue[Any]:
        return asyncio.Queue()

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        pass

    async def submit(
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_exchange_id: str | None = None,
        source: MessageSource | None = None,
    ) -> None:
        self.submitted.append((content, client_message_id, reply_to_exchange_id))
        self.kinds.append((source or MessageSource()).kind)

    async def cancel(self) -> None:
        raise AssertionError("cancel should not be called in these tests")


def make_fake_provider(runner: FakeRunner) -> RunnerProvider:
    """A RunnerProvider always returning the given fake runner."""

    async def provider(user_id: str, channel: str) -> ConversationRunner:
        return cast(ConversationRunner, runner)

    return provider


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def tool_call_reply() -> ChatMessage:
    call = ToolCall(id=CALL_ID, name=ECHO_TOOL, arguments={})
    return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=(call,))


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def make_manager(
    llm_client: LLMClient,
    session_factory: async_sessionmaker[AsyncSession],
    registry: ToolRegistry | None = None,
    tasks: InMemoryTaskStore | None = None,
) -> ConversationManager:
    loop = AgentLoop(
        llm_client=llm_client,
        registry=registry or ToolRegistry(),
        max_iterations=MAX_ITERATIONS,
    )
    return ConversationManager(
        config=RunnerConfig(
            loop=loop,
            prompts=StaticPromptProvider({SYSTEM_PROMPT_NAME: SYSTEM_PROMPT}),
            router=PassthroughRouter(),
            max_processes=MAX_PROCESSES,
            compactor=NoopContextCompactor(),
        ),
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(session_factory),
            messages=SqlAlchemyMessageRepository(session_factory),
            tasks=tasks if tasks is not None else InMemoryTaskStore(),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
        ),
        ownership=OwnershipConfig(node_id="test-node"),
    )


def make_bridge(
    client: FakeTelegramClient,
    manager: ConversationManager,
    drafts: SqlAlchemyDraftStore | None = None,
) -> TelegramBridge:
    return TelegramBridge(
        user_id=TELEGRAM_USER_ID,
        chat_id=CHAT_ID,
        runner_provider=manager.get_or_create_runner,
        client=client,
        options=TelegramBridgeOptions(edit_throttle_seconds=NO_THROTTLE, drafts=drafts),
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_warmed_bridge_gets_the_result_that_finished_while_it_was_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A result redelivered before the bridge subscribes must still reach the chat.

    Recovery asks for the redelivery while building the dialog's actor, and
    the bridge is attached to that actor a moment later — so the request
    arrives before anything is listening. The outbox has to hold it until the
    bridge subscribes, otherwise a cron answer that landed while the bot was
    down is silently stamped delivered and never shown.
    """
    client = FakeTelegramClient()
    tasks = InMemoryTaskStore()
    manager = await make_manager(ScriptedLLM([]), session_factory, tasks=tasks)
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(
        TELEGRAM_USER_ID, TELEGRAM_CHANNEL
    )
    task = Task(
        dialog_id=dialog.id,
        user_id=dialog.user_id,
        channel=dialog.channel,
        title=CRON_TITLE,
        kind=TaskKind.RUN,
        input={"prompt": CRON_TITLE},
        status=TaskStatus.DONE,
        result=CRON_RESULT,
        started_at=utc_now(),
    )
    await tasks.add(task)

    await manager.recover_interrupted()  # no bridge is attached yet
    await asyncio.sleep(POLL_SECONDS * 5)

    assert client.sent == []
    assert task.delivered_at is None

    bridge = make_bridge(client, manager)
    await bridge.start()  # what the manager's attach does once the actor exists

    await wait_until(lambda: client.sent != [])
    assert client.sent == [(CHAT_ID, CRON_RESULT)]
    await wait_until(lambda: task.delivered_at is not None)
    await bridge.aclose()
    await manager.stop_all()


async def test_first_contact_through_the_poller_renders_the_answer_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The in-process arrangement, which no test drove end to end before.

    The bridge resolves its runner; resolving it attaches this surface to the
    dialog; attaching starts the bridge for that dialog — this same object,
    re-entrantly, from inside its own `_ensure_runner()`. The nested call ran
    the whole of `start()` because `_forwarder` was still None, and the outer
    call then subscribed a second time. Two forwarders appended deltas to one
    draft and the user read every answer twice, interleaved:
    "hhelellolo" instead of "hello".

    Only the in-process path reaches it. With ingestion in its own process a
    bridge is born from the attach, after the runner build is memoized, so
    the nested resolve never attaches again.

    Startup warming was the caller that reached this, and it is gone. The
    guard is kept — and driven here through the same two calls warming made —
    because `start()` is public and `attach` calls it: anything that starts a
    bridge before its dialog has an actor brings the bug straight back.
    """
    identities = SqlAlchemyIdentityStore(session_factory)
    person = await identities.resolve_or_create(TELEGRAM_CHANNEL, str(CHAT_ID))
    client = FakeTelegramClient()
    manager = await make_manager(ChunkedLLM(["hel", "lo"]), session_factory)
    registry = TelegramBridgeRegistry(
        runner_provider=manager.get_or_create_runner,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
        identities=identities,
    )
    manager.use_surface(registry)

    # starting a bridge whose dialog has no actor yet is the entry that
    # reaches it — `handle_text` resolves the runner BEFORE calling start(),
    # so there the nested start finishes first and the outer one finds a live
    # forwarder
    await registry.get_or_create(person, CHAT_ID).start()
    bridge = await registry.gateway_for(TELEGRAM_USER_ID, CHAT_ID)
    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == "hello" if client.sent else False)

    assert client.current_text() == "hello"
    await registry.aclose()
    await manager.stop_all()


async def test_single_delta_sends_one_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.sent == [(CHAT_ID, REPLY)]
    assert client.edited == []
    await bridge.aclose()
    await manager.stop_all()


async def test_answer_replies_to_its_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The draft is created as a reply to the Telegram message it answers."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi", client_message_id="777")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.replies == [777]
    await bridge.aclose()
    await manager.stop_all()


async def test_empty_final_renders_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deliberately silent answer must not produce an empty Telegram message."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply("")]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    runner = await manager.get_or_create_runner(TELEGRAM_USER_ID, TELEGRAM_CHANNEL)
    await wait_until(lambda: not runner._processes)

    assert client.sent == []
    assert client.edited == []
    await bridge.aclose()
    await manager.stop_all()


async def test_process_started_does_not_retarget_a_live_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """White-box: once the draft message exists, a later ProcessStarted must
    not change what it replies to (a reply is fixed at creation time)."""
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge._render(
        ProcessStarted(process_id="p1", title="one", source_client_message_id="777"), "x1"
    )
    await bridge._render(TextDelta(text="draft text"), "x1")
    assert client.replies == [QUESTION_MESSAGE_ID]
    await bridge._render(
        ProcessStarted(process_id="p2", title="two", source_client_message_id="888"), "x1"
    )

    assert bridge._drafts["x1"].reply_to == QUESTION_MESSAGE_ID  # unchanged
    await manager.stop_all()


async def test_keyless_submit_sends_a_plain_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ScriptedLLM([reply()]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == REPLY if client.sent else False)

    assert client.replies == [None]
    await bridge.aclose()
    await manager.stop_all()


async def test_deltas_stream_into_one_edited_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(ChunkedLLM(["hel", "lo"]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == "hello" if client.sent else False)

    assert client.sent == [(CHAT_ID, "hel")]
    assert client.edited == [(CHAT_ID, 1, "hello")]
    await bridge.aclose()
    await manager.stop_all()


async def test_long_reply_is_split_into_telegram_sized_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The budget is the Rich Message one — eight times the plain-text limit.
    An answer past even that is sealed and continued in a fresh message."""
    client = FakeTelegramClient()
    long_reply = "x" * MAX_RICH_MESSAGE_LENGTH + "\n" + LONG_REPLY_TAIL
    manager = await make_manager(ScriptedLLM([reply(long_reply)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi", client_message_id="555")
    await wait_until(lambda: len(client.sent) == EXPECTED_MESSAGE_COUNT)

    head, tail = client.sent
    assert head == (CHAT_ID, "x" * MAX_RICH_MESSAGE_LENGTH)
    assert tail == (CHAT_ID, LONG_REPLY_TAIL)
    # only the head of a split answer replies; continuations are plain
    assert client.replies == [555, None]
    await bridge.aclose()


async def test_an_answer_under_the_rich_limit_is_never_split(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bug this delivery path exists for: an answer over the 4096-character
    plain-text limit used to be cut into separate messages, and a table cut
    that way rendered as raw pipes and letters."""
    client = FakeTelegramClient()
    rows = "".join(f"|{i}|https://example.com/{i}|\n" for i in range(200))
    table = "|#|Ссылка|\n|---|---|\n" + rows
    manager = await make_manager(ScriptedLLM([reply(table)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text().endswith("|") if client.sent else False)

    assert len(table) > PLAIN_TEXT_LIMIT  # would have been split before
    assert len(client.sent) == 1
    assert client.current_text() == table.rstrip("\n")
    await bridge.aclose()
    await manager.stop_all()


async def test_tool_call_renders_status_line_before_the_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    client = FakeTelegramClient()
    manager = await make_manager(
        ScriptedLLM([tool_call_reply(), reply()]), session_factory, registry
    )
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    tool_line = TOOL_LINE_TEMPLATE.format(name=ECHO_TOOL)
    expected = f"{tool_line}\n{REPLY}"
    await wait_until(lambda: client.current_text() == expected if client.sent else False)

    assert client.sent == [(CHAT_ID, tool_line)]
    assert client.edited == [(CHAT_ID, 1, expected)]
    await bridge.aclose()
    await manager.stop_all()


async def test_cancel_appends_the_cancelled_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Whoever stops the run, the chat has to show that it stopped.

    The stop reaches the dialog rather than the surface — a user asking to
    stop is routed like anything else, and the web UI presses the API. The
    bridge's job is only to render what comes back out.
    """
    llm = StallingLLM()
    client = FakeTelegramClient()
    manager = await make_manager(llm, session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: bool(client.sent))
    runner = await manager.get_or_create_runner(TELEGRAM_USER_ID, TELEGRAM_CHANNEL)
    await runner.cancel()
    llm.release.set()
    expected = f"{PARTIAL}\n{CANCELLED_LINE}"
    await wait_until(lambda: client.current_text() == expected if client.edited else False)

    assert client.current_text() == expected
    await bridge.aclose()
    await manager.stop_all()


async def test_llm_failure_appends_the_error_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeTelegramClient()
    manager = await make_manager(FailingLLM(), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: "❌ Ошибка" in client.current_text() if client.sent else False)

    assert client.current_text().startswith(PARTIAL)
    assert "RuntimeError: boom" in client.current_text()
    await bridge.aclose()
    await manager.stop_all()


async def test_markdown_reply_is_delivered_verbatim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Telegram renders the Markdown natively, so nothing is converted on the
    way out — what the agent wrote is what is sent."""
    client = FakeTelegramClient()
    content = "**жирный**\n```\ncode\n```"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi")
    await wait_until(lambda: client.current_text() == content if client.sent else False)

    assert client.sent == [(CHAT_ID, content)]
    await bridge.aclose()
    await manager.stop_all()


async def test_a_table_reply_is_sent_rich_and_still_threads_its_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`sendRichMessage` takes `reply_parameters`, so per-exchange threading
    survives the move off the plain-text send."""
    client = FakeTelegramClient()
    content = "|col|val|\n|---|---|\n|a|1|"
    manager = await make_manager(ScriptedLLM([reply(content)]), session_factory)
    bridge = make_bridge(client, manager)

    await bridge.handle_text("hi", client_message_id=str(QUESTION_MESSAGE_ID))
    await wait_until(lambda: client.current_text() == content if client.sent else False)

    assert client.sent == [(CHAT_ID, content)]
    assert client.replies[0] == QUESTION_MESSAGE_ID
    await bridge.aclose()
    await manager.stop_all()


# --- reply-target routing (deterministic reply shortcut) --------------------


def make_fake_bridge(client: FakeTelegramClient, runner: FakeRunner) -> TelegramBridge:
    return TelegramBridge(
        user_id=TELEGRAM_USER_ID,
        chat_id=CHAT_ID,
        runner_provider=make_fake_provider(runner),
        client=client,
        options=TelegramBridgeOptions(edit_throttle_seconds=NO_THROTTLE),
    )


async def test_reply_target_is_recorded_and_used_for_a_matching_reply() -> None:
    """A message the bridge sends for an exchange becomes that exchange's reply target."""
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge._render(TextDelta(text=REPLY), "x1")
    await bridge._render(Finished(message=reply()), "x1")
    assert client.sent == [(CHAT_ID, REPLY)]
    assert bridge._reply_targets == {1: "x1"}  # the sent message id maps to its exchange

    await bridge.handle_text("thanks", reply_to_message_id=1)

    assert runner.submitted == [("thanks", None, "x1")]
    await bridge.aclose()


async def test_unmatched_reply_target_submits_without_an_exchange() -> None:
    """A reply to an id the bridge never sent falls back to the router (no exchange)."""
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge._render(TextDelta(text=REPLY), "x1")
    await bridge._render(Finished(message=reply()), "x1")

    await bridge.handle_text("thanks", reply_to_message_id=999)

    assert runner.submitted == [("thanks", None, None)]
    await bridge.aclose()


async def test_absent_reply_target_submits_without_an_exchange() -> None:
    """A plain (non-reply) submit passes no `reply_to_exchange_id` at all."""
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge.handle_text("hi")

    assert runner.submitted == [("hi", None, None)]
    await bridge.aclose()


async def test_reply_target_map_is_bounded() -> None:
    """The reply-target map evicts the oldest mapping once past its cap."""
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)
    overflow = REPLY_TARGET_MAP_SIZE + 5

    for message_id in range(overflow):
        bridge._record_reply_target(message_id, f"exchange-{message_id}")

    assert len(bridge._reply_targets) == REPLY_TARGET_MAP_SIZE
    assert 0 not in bridge._reply_targets  # the oldest mappings were evicted
    assert overflow - 1 in bridge._reply_targets  # the newest survives


async def test_concurrent_exchanges_render_into_separate_messages() -> None:
    """Two exchanges streaming at once produce two distinct Telegram messages.

    Each carries its own content, and both drafts are cleaned up once their
    exchange's terminal event arrives.
    """
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge._render(TextDelta(text="alpha"), "x1")
    await bridge._render(TextDelta(text="beta"), "x2")
    await bridge._render(Finished(message=reply("alpha")), "x1")
    await bridge._render(Finished(message=reply("beta")), "x2")

    assert len(client.sent) == EXPECTED_MESSAGE_COUNT
    (chat_id_1, text_1), (chat_id_2, text_2) = client.sent
    assert (chat_id_1, text_1) == (CHAT_ID, "alpha")
    assert (chat_id_2, text_2) == (CHAT_ID, "beta")
    # distinct message ids, each correctly mapped back to the exchange it answered
    assert bridge._reply_targets == {1: "x1", 2: "x2"}
    assert bridge._drafts == {}  # both terminal events popped their draft


# --- terminal-flush retry -----------------------------------------------------


async def test_terminal_flush_retries_once_before_giving_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first failed terminal flush is retried once; the retry delivers the text."""
    monkeypatch.setattr(bridge_module, "TERMINAL_FLUSH_RETRY_DELAY_SECONDS", 0.0)
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge._render(TextDelta(text=PARTIAL), "x1")  # sends the head, message_id=1
    client.edit_failures = [TelegramApiError("temporary hiccup")]

    await bridge._render(Cancelled(), "x1")

    expected = f"{PARTIAL}\n{CANCELLED_LINE}"
    assert client.edited[-1] == (CHAT_ID, 1, expected)
    assert bridge._drafts == {}  # popped exactly once, on the successful retry
    await bridge.aclose()


async def test_terminal_flush_pops_the_draft_even_when_both_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A terminal flush failing twice still frees the draft and logs loudly.

    A permanently stale draft would corrupt or swallow the next answer
    delivered under the same exchange id, so the pop must happen regardless
    of the flush outcome; the lost message is reported as an error, not a
    warning, so it stands out from routine render hiccups.
    """
    monkeypatch.setattr(bridge_module, "TERMINAL_FLUSH_RETRY_DELAY_SECONDS", 0.0)
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = make_fake_bridge(client, runner)

    await bridge._render(TextDelta(text=PARTIAL), "x1")  # sends the head, message_id=1
    client.edit_failures = [
        TelegramApiError("temporary hiccup"),
        TelegramApiError("still down"),
    ]

    with caplog.at_level(logging.ERROR, logger=bridge_module.__name__):
        await bridge._render(Cancelled(), "x1")

    assert bridge._drafts == {}  # never left stale despite the lost message
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    await bridge.aclose()


async def test_material_submits_without_promising_an_answer() -> None:
    """A forward must not raise a typing indicator: no answer follows it directly."""
    client = FakeTelegramClient()
    runner = FakeRunner()
    bridge = TelegramBridge(
        user_id=TELEGRAM_USER_ID,
        chat_id=CHAT_ID,
        runner_provider=make_fake_provider(runner),
        client=client,
        options=TelegramBridgeOptions(edit_throttle_seconds=NO_THROTTLE),
    )

    await bridge.handle_text(
        "[переслано от Иван] чужой текст",
        client_message_id="7",
        kind=MessageKind.MATERIAL,
        origin="Иван",
    )

    assert runner.kinds == [MessageKind.MATERIAL]
    assert client.actions == []
    await bridge.aclose()


async def test_the_bridge_drops_a_runner_that_handed_the_dialog_over(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """It must not resubscribe here: re-resolving would claim the dialog
    straight back from whoever just took it, and the two processes would
    trade it forever. The next message rebinds through the normal path.
    """
    client = FakeTelegramClient()
    manager = await make_manager([], session_factory)
    bridge = make_bridge(client, manager)
    try:
        await bridge.start()
        runner = await manager.get_or_create_runner(TELEGRAM_USER_ID, TELEGRAM_CHANNEL)

        await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, "another-node")
        await manager._beat_once()
        await asyncio.wait_for(bridge._forwarder, timeout=WAIT_TIMEOUT_SECONDS)

        assert bridge._runner is None
    finally:
        await bridge.aclose()
        await manager.stop_all()


async def draft_store() -> SqlAlchemyDraftStore:
    """A draft store on its own in-memory copy of the surface schema."""
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    return SqlAlchemyDraftStore(create_session_factory(engine))


async def test_a_dialog_that_moved_keeps_writing_into_the_same_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The failure this prevents: a deploy mid-answer leaves the user with a
    truncated message and a second, complete one underneath it."""
    drafts = await draft_store()
    first_client = FakeTelegramClient()
    first = make_bridge(first_client, await make_manager([], session_factory), drafts=drafts)
    await first._render_safely(TextDelta(text="Согласно отчёту"), EXCHANGE_ID)
    assert first_client.sent  # the message exists now
    message_id = first_client._next_message_id

    # another process picks the dialog up and re-answers it from scratch
    second_client = FakeTelegramClient()
    second = make_bridge(second_client, await make_manager([], session_factory), drafts=drafts)
    await second._restore_drafts()
    await second._render_safely(TextDelta(text="Согласно отчёту за третий квартал"), EXCHANGE_ID)

    assert second_client.sent == []  # no second message was started
    assert [edit[1] for edit in second_client.edited] == [message_id]


async def test_a_finished_answer_is_no_longer_remembered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Otherwise the next answer of the same exchange would edit a message
    that already holds a final reply."""
    drafts = await draft_store()
    client = FakeTelegramClient()
    bridge = make_bridge(client, await make_manager([], session_factory), drafts=drafts)
    await bridge._render_safely(TextDelta(text="почти"), EXCHANGE_ID)
    assert await drafts.load(CHAT_ID) != []

    await bridge._render_safely(Finished(message=reply("готово")), EXCHANGE_ID)

    assert await drafts.load(CHAT_ID) == []


async def test_a_notice_without_an_exchange_is_never_remembered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Broker notices and background results are one-shot: there is no
    exchange to key them by, and nothing to continue after a move."""
    drafts = await draft_store()
    client = FakeTelegramClient()
    bridge = make_bridge(client, await make_manager([], session_factory), drafts=drafts)

    await bridge._render_safely(TextDelta(text="фоновая работа готова"), None)

    assert await drafts.load(CHAT_ID) == []
