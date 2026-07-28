"""Tests for the Telegram long-poll loop and the bridge registry."""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationManager,
    MessageRole,
    ToolRegistry,
    ToolSpec,
)
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import ConversationRunner, RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.tasks.store import InMemoryTaskStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.telegram.bridge import PARSE_MODE_HTML, RunnerProvider
from octoforge_web.telegram.client import USER_ID_PREFIX
from octoforge_web.telegram.invites.models import InviteBase
from octoforge_web.telegram.invites.store import SqlAlchemyInviteStore
from octoforge_web.telegram.models import (
    TelegramChat,
    TelegramChatType,
    TelegramMessage,
    TelegramReplyToMessage,
    TelegramUpdate,
    TelegramUser,
)
from octoforge_web.telegram.poller import (
    ACCESS_DENIED_TEXT,
    COMMAND_CANCEL,
    COMMAND_START,
    GREETING_TEXT,
    GROUP_NOTICE,
    INVITE_INVALID_TEXT,
    SECRETS_DISABLED_TEXT,
    TEXT_ONLY_NOTICE,
    WELCOME_TEXT,
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
    chat_id_from_user_id,
)

TELEGRAM_USER_ID = 12345
USER_ID = f"{USER_ID_PREFIX}{TELEGRAM_USER_ID}"
CHANNEL = "telegram"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
NO_THROTTLE = 0.0
NO_BACKOFF = 0.0
POLL_TIMEOUT = 30.0
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
WAIT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.01
IDLE_BATCH_SECONDS = 0.05
REPLY = "pong"
FIRST_UPDATE_ID = 41
SECOND_UPDATE_ID = 42
EXPECTED_TWO_REPLIES = 2
EXPECTED_GREETING_COUNT = 2
EXPECTED_CALLS_AFTER_DRAIN = 2
MIN_CALLS_AFTER_RECOVERY = 3


class FakeTelegramClient:
    """TelegramClient stub with scripted poll batches and recorded outbound calls."""

    def __init__(self, batches: list[list[TelegramUpdate]] | None = None) -> None:
        self._batches = list(batches) if batches is not None else []
        self.poll_calls: list[tuple[int | None, float]] = []
        self.failures: list[Exception] = []
        self.sent: list[tuple[int, str, str | None]] = []
        self.replies: list[int | None] = []
        self.edited: list[tuple[int, int, str, str | None]] = []
        self._next_message_id = 0

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        self.poll_calls.append((offset, timeout_seconds))
        if self.failures:
            raise self.failures.pop(0)
        if self._batches:
            return self._batches.pop(0)
        await asyncio.sleep(IDLE_BATCH_SECONDS)
        return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        self._next_message_id += 1
        self.sent.append((chat_id, text, parse_mode))
        self.replies.append(reply_to_message_id)
        return self._next_message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.edited.append((chat_id, message_id, text, parse_mode))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass


class RecordingBridge:
    """TelegramBridge stub recording `handle_text` calls (poller-forwarding tests only)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None]] = []

    async def handle_text(
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        self.calls.append((content, client_message_id, reply_to_message_id))

    async def cancel(self) -> None:
        raise AssertionError("cancel should not be called in these tests")


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


class PassthroughRouter:
    """MessageRouter stub always starting a new process."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


def make_update(
    update_id: int,
    text: str | None = "hi",
    chat_type: TelegramChatType = TelegramChatType.PRIVATE,
    message_id: int | None = None,
) -> TelegramUpdate:
    # message_id defaults to update_id for convenience but is an INDEPENDENT
    # value in Telegram: tests probing the dedup/reply key pass it explicitly
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=message_id if message_id is not None else update_id,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=chat_type),
            text=text,
        ),
    )


async def forbidden_provider(user_id: str, channel: str) -> ConversationRunner:
    raise AssertionError("runner should not be requested")


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def make_manager(
    replies: list[ChatMessage],
    session_factory: async_sessionmaker[AsyncSession],
) -> ConversationManager:
    loop = AgentLoop(
        llm_client=ScriptedLLM(replies),
        registry=ToolRegistry(),
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
        dialogs=SqlAlchemyDialogRepository(session_factory),
        messages=SqlAlchemyMessageRepository(session_factory),
        tasks=InMemoryTaskStore(),
        exchanges=SqlAlchemyExchangeRepository(session_factory),
    )


def make_poller(
    client: FakeTelegramClient,
    provider: RunnerProvider = forbidden_provider,
    membership: TelegramMembership | None = None,
    secrets_link: Callable[[str], str] | None = None,
) -> TelegramPoller:
    registry = TelegramBridgeRegistry(
        runner_provider=provider,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
    )
    return TelegramPoller(
        client=client,
        registry=registry,
        options=TelegramPollerOptions(
            poll_timeout_seconds=POLL_TIMEOUT,
            error_backoff_seconds=NO_BACKOFF,
            membership=membership,
            secrets_link=secrets_link,
        ),
    )


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_start_command_greets_without_runner() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=COMMAND_START))

    assert client.sent == [(TELEGRAM_USER_ID, GREETING_TEXT, None)]


async def test_group_chat_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, chat_type=TelegramChatType.GROUP))

    assert client.sent == [(TELEGRAM_USER_ID, GROUP_NOTICE, None)]


async def test_non_text_message_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=None))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_text_message_reaches_the_dialog_and_renders_the_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, PARSE_MODE_HTML)
    await manager.stop_all()


async def test_redelivered_update_is_not_answered_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Telegram re-sends an update until it gets a 200; update_id is the
    # idempotency key, so the redelivery must not produce a second run.
    replies = [
        ChatMessage(role=MessageRole.ASSISTANT, content=REPLY),
        ChatMessage(role=MessageRole.ASSISTANT, content=REPLY),
    ]
    manager = await make_manager(replies, session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping", message_id=1001))
    await wait_until(lambda: len(client.sent) == 1)
    # a Telegram redelivery carries a NEW update_id but the SAME message_id:
    # the chat-level message id is the idempotency key now
    await poller.dispatch(make_update(FIRST_UPDATE_ID + 7, text="ping", message_id=1001))
    await poller.dispatch(make_update(SECOND_UPDATE_ID, text="ping", message_id=1002))
    await wait_until(lambda: len(client.sent) == EXPECTED_TWO_REPLIES)

    assert len(client.sent) == EXPECTED_TWO_REPLIES
    # end-to-end: each answer replies to the message that asked it
    assert client.replies == [1001, 1002]
    await manager.stop_all()


async def test_cancel_command_is_accepted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=COMMAND_CANCEL))

    assert client.sent == []
    await manager.stop_all()


async def test_run_advances_the_offset_per_update() -> None:
    updates = [
        make_update(FIRST_UPDATE_ID, text=COMMAND_START),
        make_update(SECOND_UPDATE_ID, text=COMMAND_START),
    ]
    client = FakeTelegramClient(batches=[[], updates])
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.sent) == EXPECTED_GREETING_COUNT)
    finally:
        task.cancel()

    assert client.poll_calls[0] == (-1, 0.0)
    assert client.poll_calls[1] == (None, POLL_TIMEOUT)
    assert client.poll_calls[-1][0] == SECOND_UPDATE_ID + 1


async def test_backlog_is_drained_on_start() -> None:
    client = FakeTelegramClient(batches=[[make_update(FIRST_UPDATE_ID)]])
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.poll_calls) >= EXPECTED_CALLS_AFTER_DRAIN)
    finally:
        task.cancel()

    assert client.poll_calls[0] == (-1, 0.0)
    assert client.poll_calls[1] == (FIRST_UPDATE_ID + 1, POLL_TIMEOUT)
    assert client.sent == []


async def test_poller_recovers_from_poll_errors() -> None:
    client = FakeTelegramClient()
    client.failures.append(httpx.ConnectError("boom"))
    poller = make_poller(client)
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: len(client.poll_calls) >= MIN_CALLS_AFTER_RECOVERY)
    finally:
        task.cancel()

    assert len(client.poll_calls) >= MIN_CALLS_AFTER_RECOVERY


def test_chat_id_from_user_id() -> None:
    assert chat_id_from_user_id(USER_ID) == TELEGRAM_USER_ID
    assert chat_id_from_user_id("alice") is None
    assert chat_id_from_user_id(f"{USER_ID_PREFIX}abc") is None


async def test_dispatch_forwards_the_reply_target_to_the_bridge() -> None:
    """`reply_to_message.message_id` on the update reaches `bridge.handle_text`."""
    client = FakeTelegramClient()
    poller = make_poller(client)
    fake_bridge = RecordingBridge()
    poller._registry._bridges[USER_ID] = fake_bridge  # type: ignore[assignment]
    update = TelegramUpdate(
        update_id=FIRST_UPDATE_ID,
        message=TelegramMessage(
            message_id=2001,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            text="reply text",
            reply_to_message=TelegramReplyToMessage(message_id=999),
        ),
    )

    await poller.dispatch(update)

    assert fake_bridge.calls == [("reply text", "2001", 999)]


async def test_dispatch_forwards_no_reply_target_when_absent() -> None:
    """A plain (non-reply) message forwards `reply_to_message_id=None`."""
    client = FakeTelegramClient()
    poller = make_poller(client)
    fake_bridge = RecordingBridge()
    poller._registry._bridges[USER_ID] = fake_bridge  # type: ignore[assignment]

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="plain"))

    assert fake_bridge.calls == [("plain", str(FIRST_UPDATE_ID), None)]


async def test_warm_starts_bridges_for_known_telegram_dialogs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([], session_factory)
    requested: list[tuple[str, str]] = []

    async def provider(user_id: str, channel: str) -> ConversationRunner:
        requested.append((user_id, channel))
        return await manager.get_or_create_runner(user_id, channel)

    client = FakeTelegramClient()
    registry = TelegramBridgeRegistry(
        runner_provider=provider, client=client, edit_throttle_seconds=NO_THROTTLE
    )

    await registry.warm([USER_ID, "alice", f"{USER_ID_PREFIX}not-a-number"])

    assert requested == [(USER_ID, CHANNEL)]
    await manager.stop_all()
    await registry.aclose()


# --- membership gate ---------------------------------------------------------

ADMIN_TELEGRAM_ID = 999
INVITE_NOTE = "test invite"


@pytest.fixture
async def invite_store() -> AsyncIterator[SqlAlchemyInviteStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(InviteBase.metadata.create_all)
    yield SqlAlchemyInviteStore(create_session_factory(engine))
    await engine.dispose()


def make_membership(
    invite_store: SqlAlchemyInviteStore, admin_ids: list[int] | None = None
) -> TelegramMembership:
    return TelegramMembership(invite_store, admin_ids or [])


async def test_admin_passes_the_gate_without_invite(
    session_factory: async_sessionmaker[AsyncSession],
    invite_store: SqlAlchemyInviteStore,
) -> None:
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(
        client,
        manager.get_or_create_runner,
        membership=make_membership(invite_store, [TELEGRAM_USER_ID]),
    )

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, PARSE_MODE_HTML)
    await manager.stop_all()


async def test_stranger_is_denied_and_gets_no_runner(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))

    assert client.sent == [(TELEGRAM_USER_ID, ACCESS_DENIED_TEXT, None)]


async def test_claimed_invite_passes_the_gate(
    session_factory: async_sessionmaker[AsyncSession],
    invite_store: SqlAlchemyInviteStore,
) -> None:
    invite = await invite_store.create(INVITE_NOTE)
    await invite_store.claim(invite.code, USER_ID)
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(
        client, manager.get_or_create_runner, membership=make_membership(invite_store)
    )

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, PARSE_MODE_HTML)
    await manager.stop_all()


async def test_revoked_invite_is_denied(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    invite = await invite_store.create(INVITE_NOTE)
    await invite_store.claim(invite.code, USER_ID)
    await invite_store.revoke(invite.id)
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text="ping"))

    assert client.sent == [(TELEGRAM_USER_ID, ACCESS_DENIED_TEXT, None)]


async def test_start_with_code_claims_and_welcomes(
    session_factory: async_sessionmaker[AsyncSession],
    invite_store: SqlAlchemyInviteStore,
) -> None:
    invite = await invite_store.create(INVITE_NOTE)
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(
        client, manager.get_or_create_runner, membership=make_membership(invite_store)
    )

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} {invite.code}"))

    assert client.sent == [(TELEGRAM_USER_ID, WELCOME_TEXT, None)]
    claimed = await invite_store.get_by_user(USER_ID)
    assert claimed is not None and claimed.code == invite.code

    await poller.dispatch(make_update(SECOND_UPDATE_ID, text="ping"))
    await wait_until(lambda: len(client.sent) == EXPECTED_TWO_REPLIES)

    assert client.sent[1] == (TELEGRAM_USER_ID, REPLY, PARSE_MODE_HTML)
    await manager.stop_all()


async def test_start_with_invalid_code_is_denied(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} wrong-code"))

    assert client.sent == [(TELEGRAM_USER_ID, INVITE_INVALID_TEXT, None)]


async def test_start_with_expired_code_is_denied() -> None:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(InviteBase.metadata.create_all)
    expiring = SqlAlchemyInviteStore(create_session_factory(engine), ttl_seconds=0)
    invite = await expiring.create(INVITE_NOTE)
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(expiring))

    await poller.dispatch(make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} {invite.code}"))

    assert client.sent == [(TELEGRAM_USER_ID, INVITE_INVALID_TEXT, None)]
    await engine.dispose()


async def test_secrets_command_replies_with_a_link_and_never_reaches_the_dialog() -> None:
    """The T2 ingestion entry: /secrets is intercepted before the pipeline."""
    client = FakeTelegramClient()
    issued: list[str] = []

    def link(user_id: str) -> str:
        issued.append(user_id)
        return f"https://example.com/secrets.html?token=tok-{user_id}"

    # forbidden_provider raises if the message ever reaches the dialog pipeline
    poller = make_poller(client, secrets_link=link)

    await poller.dispatch(make_update(1, "/secrets"))

    assert issued == [USER_ID]
    ((_, text, _),) = client.sent
    assert "secrets.html?token=tok-" in text
    assert "Никогда не присылайте секреты" in text


async def test_secrets_command_without_the_feature_reports_it() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await poller.dispatch(make_update(1, "/secrets"))

    ((_, text, _),) = client.sent
    assert text == SECRETS_DISABLED_TEXT
