"""Tests for the Telegram long-poll loop and the bridge registry."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import cast

import httpx
import pytest
from octoforge_core import (
    AgentLoop,
    Attachment,
    AttachmentKind,
    ChatMessage,
    ConversationManager,
    MessageKind,
    MessageRole,
    ToolRegistry,
    ToolSpec,
)
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
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.speech.api import AudioData, TranscriptionClient
from octoforge_core.tasks.store import SqlAlchemyTaskStore
from octoforge_core.vision.api import ImageData, VisionClient
from octoforge_telegram.bridge import RunnerProvider
from octoforge_telegram.client import USER_ID_PREFIX, TelegramApiError
from octoforge_telegram.images import REF_PREFIX
from octoforge_telegram.invites.api import MemberProfile
from octoforge_telegram.invites.store import SqlAlchemyInviteStore
from octoforge_telegram.models import (
    TelegramChat,
    TelegramChatType,
    TelegramForwardOrigin,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramReplyToMessage,
    TelegramUpdate,
    TelegramUser,
    TelegramVoice,
)
from octoforge_telegram.poller import (
    ACCESS_DENIED_TEXT,
    CAPTION_LABEL,
    COMMAND_START,
    DEFAULT_VOICE_MAX_SECONDS,
    GREETING_TEXT,
    GROUP_NOTICE,
    IMAGE_FAILED_PLACEHOLDER,
    IMAGE_TAG,
    IMAGE_TAG_NUMBERED,
    INGESTION_PROMPT,
    INVITE_INVALID_TEXT,
    MATERIAL_ATTRIBUTION_ANONYMOUS,
    MATERIAL_PLACEHOLDER,
    SECRETS_DISABLED_TEXT,
    TEXT_ONLY_NOTICE,
    VOICE_EMPTY_NOTICE,
    VOICE_TAG,
    VOICE_TOO_SHORT_NOTICE,
    WELCOME_TEXT,
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
    chat_id_from_user_id,
)
from octoforge_telegram.schema import TelegramSurfaceBase
from octoforge_telegram.surface import TelegramSurface
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TELEGRAM_USER_ID = 12345
USER_ID = f"{USER_ID_PREFIX}{TELEGRAM_USER_ID}"
CHANNEL = "telegram"
# the album window a test waits out (the production one is 1.5s)
ALBUM_TEST_QUIET_SECONDS = 0.02
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
# marks a recorded call as the Rich Message path (answers) rather than a
# plain notice; the fake client has no parse_mode to record for those
RICH = "rich"
FIRST_UPDATE_ID = 41
SECOND_UPDATE_ID = 42
EXPECTED_TWO_REPLIES = 2
EXPECTED_GREETING_COUNT = 2
EXPECTED_CALLS_AFTER_DRAIN = 2
MIN_CALLS_AFTER_RECOVERY = 3
MIN_POLL_ONCE_CALLS_AFTER_FAILURE = 2


class FakeTelegramClient:
    """TelegramClient stub with scripted poll batches and recorded outbound calls."""

    def __init__(self, batches: list[list[TelegramUpdate]] | None = None) -> None:
        self._batches = list(batches) if batches is not None else []
        self.poll_calls: list[tuple[int | None, float]] = []
        self.failures: list[Exception] = []
        self.sent: list[tuple[int, str, str | None]] = []
        self.replies: list[int | None] = []
        self.edited: list[tuple[int, int, str, str | None]] = []
        self.downloaded_file_ids: list[str] = []
        self.download_error: Exception | None = None
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

    async def send_rich_message(
        self, chat_id: int, markdown: str, reply_to_message_id: int | None = None
    ) -> int:
        # answers go out as Rich Messages; notices stay on plain send_message
        self._next_message_id += 1
        self.sent.append((chat_id, markdown, RICH))
        self.replies.append(reply_to_message_id)
        return self._next_message_id

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        self.edited.append((chat_id, message_id, markdown, RICH))

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass

    async def get_file(self, file_id: str) -> str:
        return f"path/{file_id}"

    async def download_file(self, file_path: str) -> bytes:
        if self.download_error is not None:
            raise self.download_error
        self.downloaded_file_ids.append(file_path.removeprefix("path/"))
        return b"fake-image-bytes"


class RecordingBridge:
    """TelegramBridge stub recording `handle_text` calls (poller-forwarding tests only)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None]] = []
        self.kinds: list[tuple[MessageKind, str | None]] = []
        self.attachments: list[tuple[Attachment, ...]] = []

    async def handle_text(  # noqa: PLR0913, PLR0917 — mirrors TelegramBridge.handle_text
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
        kind: MessageKind = MessageKind.OWN,
        origin: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        self.calls.append((content, client_message_id, reply_to_message_id))
        self.kinds.append((kind, origin))
        self.attachments.append(attachments)


class ExplodingBridge:
    """TelegramBridge stub whose `handle_text` always raises (resilience tests only)."""

    async def handle_text(
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        raise RuntimeError("boom")

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


class FakeVisionClient:
    """VisionClient stub returning a scripted description, recording every call."""

    def __init__(self, description: str) -> None:
        self._description = description
        self.calls: list[tuple[tuple[ImageData, ...], str]] = []

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        self.calls.append((images, prompt))
        return self._description


class RaisingVisionClient:
    """VisionClient stub that always fails (fallback-path tests only)."""

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        raise RuntimeError("vision boom")


def make_update(
    update_id: int,
    text: str | None = "hi",
    chat_type: TelegramChatType = TelegramChatType.PRIVATE,
    message_id: int | None = None,
    sender_id: int = TELEGRAM_USER_ID,
) -> TelegramUpdate:
    # message_id defaults to update_id for convenience but is an INDEPENDENT
    # value in Telegram: tests probing the dedup/reply key pass it explicitly
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=message_id if message_id is not None else update_id,
            from_user=TelegramUser(id=sender_id),
            chat=TelegramChat(id=sender_id, type=chat_type),
            text=text,
        ),
    )


def make_photo_update(
    update_id: int,
    file_id: str = "photo-1",
    caption: str | None = None,
    forward_origin: TelegramForwardOrigin | None = None,
    media_group_id: str | None = None,
) -> TelegramUpdate:
    """An update carrying a single-size photo (own, forwarded or an album item)."""
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=update_id,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            caption=caption,
            photo=[TelegramPhotoSize(file_id=file_id, width=800, height=600)],
            forward_origin=forward_origin,
            media_group_id=media_group_id,
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
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(session_factory),
            messages=SqlAlchemyMessageRepository(session_factory),
            tasks=SqlAlchemyTaskStore(session_factory),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
        ),
        ownership=OwnershipConfig(node_id="test-node"),
    )


def make_poller(  # noqa: PLR0913, PLR0917 — a builder mirroring the options bundle
    client: FakeTelegramClient,
    provider: RunnerProvider = forbidden_provider,
    membership: TelegramMembership | None = None,
    secrets_link: Callable[[str], str] | None = None,
    vision: VisionClient | None = None,
    speech: TranscriptionClient | None = None,
    voice_max_seconds: float = DEFAULT_VOICE_MAX_SECONDS,
) -> TelegramPoller:
    """A poller whose album window is short enough for a test to wait it out."""
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
            vision=vision,
            speech=speech,
            voice_max_seconds=voice_max_seconds,
            album_quiet_seconds=ALBUM_TEST_QUIET_SECONDS,
        ),
    )


def use_bridge(poller: TelegramPoller, bridge: object) -> None:
    """Point the poller's registry at one stub bridge."""
    poller._registry.get_or_create = lambda user_id, chat_id: bridge  # type: ignore[assignment,method-assign,return-value]


async def settle(poller: TelegramPoller, user_id: str | None = None) -> None:
    """Wait until the workers have finished what they were given.

    Scoped to one user when another is deliberately parked mid-ingestion.
    """
    inboxes = poller._inboxes

    def drained() -> bool:
        watched = (
            [inboxes[user_id]] if user_id is not None and user_id in inboxes else inboxes.values()
        )
        return all(not inbox.pending and not inbox.busy for inbox in watched)

    await wait_until(drained)


async def deliver(
    poller: TelegramPoller, update: TelegramUpdate, user_id: str | None = None
) -> None:
    """Enqueue one update and wait for it to be handled.

    `dispatch` only queues now, so a test that asserts the effect of a single
    message waits for its user's worker here.
    """
    await poller.dispatch(update)
    await settle(poller, user_id)


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(poll(), WAIT_TIMEOUT_SECONDS)


async def test_start_command_greets_without_runner() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await deliver(poller, make_update(FIRST_UPDATE_ID, text=COMMAND_START))

    assert client.sent == [(TELEGRAM_USER_ID, GREETING_TEXT, None)]


async def test_group_chat_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await deliver(poller, make_update(FIRST_UPDATE_ID, chat_type=TelegramChatType.GROUP))

    assert client.sent == [(TELEGRAM_USER_ID, GROUP_NOTICE, None)]


async def test_non_text_message_gets_a_notice() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await deliver(poller, make_update(FIRST_UPDATE_ID, text=None))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_text_message_reaches_the_dialog_and_renders_the_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reply = ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)
    manager = await make_manager([reply], session_factory)
    client = FakeTelegramClient()
    poller = make_poller(client, manager.get_or_create_runner)

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, RICH)
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

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping", message_id=1001))
    await wait_until(lambda: len(client.sent) == 1)
    # a Telegram redelivery carries a NEW update_id but the SAME message_id:
    # the chat-level message id is the idempotency key now
    await deliver(poller, make_update(FIRST_UPDATE_ID + 7, text="ping", message_id=1001))
    await deliver(poller, make_update(SECOND_UPDATE_ID, text="ping", message_id=1002))
    await wait_until(lambda: len(client.sent) == EXPECTED_TWO_REPLIES)

    assert len(client.sent) == EXPECTED_TWO_REPLIES
    # end-to-end: each answer replies to the message that asked it
    assert client.replies == [1001, 1002]
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


async def test_a_poison_message_kills_neither_the_worker_nor_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug or DB blip while handling a message must not propagate.

    Regression: the old narrow `except (httpx.HTTPError, TelegramApiError)`
    let anything else (an invites-store error, an unanticipated bug) escape
    and kill the poll loop for every Telegram user. The catch now lives in
    the user's worker, which owns the ordering of one dialog and must
    survive a message it cannot handle.
    """
    client = FakeTelegramClient()
    poller = make_poller(client)
    poller._registry._bridges[USER_ID] = ExplodingBridge()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        await deliver(poller, make_update(FIRST_UPDATE_ID, text="hi"))
        await deliver(poller, make_update(SECOND_UPDATE_ID, text="hi again"))

    assert any("Failed to handle" in record.message for record in caplog.records)
    inbox = poller._inboxes[USER_ID]
    assert inbox.worker is not None and not inbox.worker.done()  # still draining


async def test_run_forever_survives_an_unexpected_poll_exception() -> None:
    """`run_forever`'s own top-level catch-all outlives whatever escapes `_poll_once`.

    This is the last line of defense: even a failure `_dispatch_safely`
    cannot see (e.g. one raised before it, inside `_poll_once` itself) must
    only back off and retry, never stop the loop.
    """
    client = FakeTelegramClient(batches=[[]])  # drains cleanly
    poller = make_poller(client)
    calls = 0
    original_poll_once = poller._poll_once

    async def flaky_poll_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected blip")
        await original_poll_once()

    poller._poll_once = flaky_poll_once  # type: ignore[method-assign]
    task = asyncio.create_task(poller.run_forever())
    try:
        await wait_until(lambda: calls >= MIN_POLL_ONCE_CALLS_AFTER_FAILURE)
        assert not task.done()  # the loop kept polling despite the exception
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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

    await deliver(poller, update)

    assert fake_bridge.calls == [("reply text", "2001", 999)]


async def test_dispatch_forwards_no_reply_target_when_absent() -> None:
    """A plain (non-reply) message forwards `reply_to_message_id=None`."""
    client = FakeTelegramClient()
    poller = make_poller(client)
    fake_bridge = RecordingBridge()
    poller._registry._bridges[USER_ID] = fake_bridge  # type: ignore[assignment]

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="plain"))

    assert fake_bridge.calls == [("plain", str(FIRST_UPDATE_ID), None)]


# --- membership gate ---------------------------------------------------------

RESEAT_ACCOUNT = 424242
ADMIN_TELEGRAM_ID = 999
INVITE_NOTE = "test invite"


@pytest.fixture
async def invite_store() -> AsyncIterator[SqlAlchemyInviteStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
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

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, RICH)
    await manager.stop_all()


async def test_stranger_is_denied_and_gets_no_runner(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping"))

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

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping"))
    await wait_until(lambda: bool(client.sent))

    assert client.sent[0] == (TELEGRAM_USER_ID, REPLY, RICH)
    await manager.stop_all()


async def test_revoked_invite_is_denied(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    invite = await invite_store.create(INVITE_NOTE)
    await invite_store.claim(invite.code, USER_ID)
    await invite_store.revoke(invite.id)
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="ping"))

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

    await deliver(poller, make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} {invite.code}"))

    assert client.sent == [(TELEGRAM_USER_ID, WELCOME_TEXT, None)]
    claimed = await invite_store.get_by_user(USER_ID)
    assert claimed is not None and claimed.code == invite.code

    await deliver(poller, make_update(SECOND_UPDATE_ID, text="ping"))
    await wait_until(lambda: len(client.sent) == EXPECTED_TWO_REPLIES)

    assert client.sent[1] == (TELEGRAM_USER_ID, REPLY, RICH)
    await manager.stop_all()


async def test_start_with_invalid_code_is_denied(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await deliver(poller, make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} wrong-code"))

    assert client.sent == [(TELEGRAM_USER_ID, INVITE_INVALID_TEXT, None)]


async def test_start_with_expired_code_is_denied() -> None:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    expiring = SqlAlchemyInviteStore(create_session_factory(engine), ttl_seconds=0)
    invite = await expiring.create(INVITE_NOTE)
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(expiring))

    await deliver(poller, make_update(FIRST_UPDATE_ID, text=f"{COMMAND_START} {invite.code}"))

    assert client.sent == [(TELEGRAM_USER_ID, INVITE_INVALID_TEXT, None)]
    await engine.dispose()


async def test_secrets_command_replies_with_a_link_and_never_reaches_the_dialog() -> None:
    """The T2 ingestion entry: /secrets is intercepted before the pipeline."""
    client = FakeTelegramClient()
    issued: list[str] = []

    def link(external_id: str) -> str:
        issued.append(external_id)
        return f"https://example.com/secrets.html?token=tok-{external_id}"

    # forbidden_provider raises if the message ever reaches the dialog pipeline
    poller = make_poller(client, secrets_link=link)

    await deliver(poller, make_update(1, "/secrets"))

    # The ACCOUNT id, bare — not the `tg:` handle. Ingestion may be running out
    # of process, where nothing can say who an account belongs to; the service
    # resolves the person when the form arrives. Handing over the handle put
    # the form in a namespace of its own and made every saved secret invisible
    # to the agent.
    assert issued == [USER_ID.removeprefix("tg:")]
    ((_, text, _),) = client.sent
    assert "secrets.html?token=tok-" in text
    assert "Никогда не присылайте секреты" in text


async def test_secrets_command_without_the_feature_reports_it() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client)

    await deliver(poller, make_update(1, "/secrets"))

    ((_, text, _),) = client.sent
    assert text == SECRETS_DISABLED_TEXT


class RecordingDirectory:
    """MemberDirectory fake capturing every record call."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, str | None]] = []

    async def record(
        self, user_id: str, first_name: str, last_name: str, username: str | None
    ) -> None:
        self.records.append((user_id, first_name, last_name, username))

    async def get(self, user_id: str) -> None:
        return None

    async def list_all(self) -> list[MemberProfile]:
        return []


async def test_gated_member_profile_is_recorded_on_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager(
        [ChatMessage(role=MessageRole.ASSISTANT, content=REPLY)], session_factory
    )
    client = FakeTelegramClient()
    directory = RecordingDirectory()
    registry = TelegramBridgeRegistry(
        runner_provider=manager.get_or_create_runner,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
    )
    poller = TelegramPoller(
        client=client,
        registry=registry,
        options=TelegramPollerOptions(
            poll_timeout_seconds=POLL_TIMEOUT,
            error_backoff_seconds=NO_BACKOFF,
            directory=directory,
        ),
    )
    update = TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            message_id=1,
            from_user=TelegramUser(
                id=TELEGRAM_USER_ID, first_name="Alice", last_name="Smith", username="alice"
            ),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            text="hello",
        ),
    )
    await deliver(poller, update)
    assert directory.records == [(USER_ID, "Alice", "Smith", "alice")]
    await manager.stop_all()


async def test_stranger_denied_by_the_gate_is_not_recorded(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    client = FakeTelegramClient()
    directory = RecordingDirectory()
    registry = TelegramBridgeRegistry(
        runner_provider=forbidden_provider,
        client=client,
        edit_throttle_seconds=NO_THROTTLE,
    )
    poller = TelegramPoller(
        client=client,
        registry=registry,
        options=TelegramPollerOptions(
            poll_timeout_seconds=POLL_TIMEOUT,
            error_backoff_seconds=NO_BACKOFF,
            membership=make_membership(invite_store),
            directory=directory,
        ),
    )
    await deliver(poller, make_update(1, text="hello"))
    assert directory.records == []


def make_forward_update(
    update_id: int,
    text: str | None = "чужой текст",
    origin: dict[str, object] | None = None,
    media_group_id: str | None = None,
    caption: str | None = None,
) -> TelegramUpdate:
    """An update carrying a forwarded message (optionally an album item)."""
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=update_id,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            text=text,
            caption=caption,
            media_group_id=media_group_id,
            forward_origin=TelegramForwardOrigin.model_validate(
                origin
                or {"type": "user", "date": 1, "sender_user": {"id": 5, "first_name": "Иван"}}
            ),
        ),
    )


async def test_forwarded_message_reaches_the_dialog_as_attributed_material() -> None:
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client)
    use_bridge(poller, bridge)

    await deliver(poller, make_forward_update(1))

    (content, client_message_id, _), (kind, origin) = bridge.calls[0], bridge.kinds[0]
    assert content == "[переслано от Иван] чужой текст"
    assert client_message_id == "1"
    assert kind is MessageKind.MATERIAL
    assert origin == "Иван"
    assert client.sent == []  # no "text only" notice, no greeting


async def test_forwarded_attachment_becomes_one_placeholder_per_album() -> None:
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client)
    use_bridge(poller, bridge)

    await poller.dispatch(
        make_forward_update(1, text=None, media_group_id="album", caption="гляди")
    )
    await poller.dispatch(make_forward_update(2, text=None, media_group_id="album"))
    await poller.dispatch(make_forward_update(3, text=None, media_group_id="album"))
    await settle(poller)

    assert [call[0] for call in bridge.calls] == ["[переслано от Иван] гляди"]
    assert client.sent == []  # the album never triggers the text-only notice


async def test_non_text_message_is_answered_only_past_the_gate(
    invite_store: SqlAlchemyInviteStore,
) -> None:
    """A stranger's photo must not make the bot reply at all."""
    client = FakeTelegramClient()
    poller = make_poller(client, forbidden_provider, membership=make_membership(invite_store))

    await deliver(poller, make_update(1, text=None))

    assert [text for _, text, _ in client.sent] == [ACCESS_DENIED_TEXT]


# --- vision (image ingestion) -------------------------------------------------

VISION_DESCRIPTION = "Фото: кот сидит на подоконнике возле окна."
PHOTO_FILE_ID = "photo-42"
FORWARD_ORIGIN_PAYLOAD = {
    "type": "user",
    "date": 1,
    "sender_user": {"id": 5, "first_name": "Иван"},
}


def make_forward_origin() -> TelegramForwardOrigin:
    return TelegramForwardOrigin.model_validate(FORWARD_ORIGIN_PAYLOAD)


async def test_bare_photo_is_material_like_a_forward() -> None:
    """No caption means the user shared something without asking anything.

    The bot must not invent a request out of a picture: the image collects
    like a forward, and the agent asks what to do with it.
    """
    client = FakeTelegramClient()
    vision = FakeVisionClient(VISION_DESCRIPTION)
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    await deliver(poller, make_photo_update(1, file_id=PHOTO_FILE_ID))

    (content, client_message_id, _), (kind, origin) = bridge.calls[0], bridge.kinds[0]
    assert content == f"{IMAGE_TAG} {VISION_DESCRIPTION}"
    assert client_message_id == "1"
    assert kind is MessageKind.MATERIAL
    assert origin is None  # nobody forwarded it, so no attribution
    assert MATERIAL_ATTRIBUTION_ANONYMOUS not in content
    expected_ref = f"{REF_PREFIX}{PHOTO_FILE_ID}"
    assert bridge.attachments[0] == (Attachment(kind=AttachmentKind.IMAGE, ref=expected_ref),)
    assert client.downloaded_file_ids == [PHOTO_FILE_ID]
    assert vision.calls[0][1] == INGESTION_PROMPT
    assert client.sent == []  # no text-only notice, vision handled it


async def test_captioned_photo_is_the_user_speaking() -> None:
    """A caption IS the request; the picture is its context, so kind stays OWN."""
    client = FakeTelegramClient()
    vision = FakeVisionClient(VISION_DESCRIPTION)
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    await deliver(poller, make_photo_update(1, caption="что это?"))

    content = bridge.calls[0][0]
    assert content == f"{IMAGE_TAG} {VISION_DESCRIPTION}\n\n{CAPTION_LABEL} что это?"
    assert bridge.kinds[0][0] is MessageKind.OWN


async def test_forwarded_photo_with_vision_is_material_with_attribution() -> None:
    client = FakeTelegramClient()
    vision = FakeVisionClient(VISION_DESCRIPTION)
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    await deliver(poller, make_photo_update(1, forward_origin=make_forward_origin()))

    (content, _, _), (kind, call_origin) = bridge.calls[0], bridge.kinds[0]
    assert content == f"[переслано от Иван] {IMAGE_TAG} {VISION_DESCRIPTION}"
    assert kind is MessageKind.MATERIAL
    assert call_origin == "Иван"


async def test_own_photo_without_vision_keeps_todays_notice() -> None:
    """No vision configured: an uncaptioned photo behaves exactly like before the feature."""
    client = FakeTelegramClient()
    poller = make_poller(client)  # vision=None

    await deliver(poller, make_photo_update(1))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]
    assert client.downloaded_file_ids == []


async def test_forwarded_photo_without_vision_keeps_the_placeholder() -> None:
    """No vision configured: a forwarded photo behaves exactly like before the feature."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client)  # vision=None
    use_bridge(poller, bridge)

    await deliver(poller, make_photo_update(1, forward_origin=make_forward_origin()))

    content = bridge.calls[0][0]
    assert content == f"[переслано от Иван] {MATERIAL_PLACEHOLDER}"
    assert client.downloaded_file_ids == []


async def test_own_photo_vision_failure_falls_back_without_propagating() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, vision=RaisingVisionClient())

    await deliver(poller, make_photo_update(1))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_own_photo_download_failure_falls_back_without_propagating() -> None:
    client = FakeTelegramClient()
    client.download_error = TelegramApiError("boom")
    poller = make_poller(client, vision=FakeVisionClient(VISION_DESCRIPTION))

    await deliver(poller, make_photo_update(1))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_forwarded_photo_vision_failure_falls_back_to_the_placeholder() -> None:
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, vision=RaisingVisionClient())
    use_bridge(poller, bridge)

    await deliver(poller, make_photo_update(1, forward_origin=make_forward_origin()))

    content = bridge.calls[0][0]
    assert content == f"[переслано от Иван] {MATERIAL_PLACEHOLDER}"


async def test_image_document_is_treated_as_a_photo() -> None:
    """An image sent as a document (not a compressed photo) is described too."""
    client = FakeTelegramClient()
    vision = FakeVisionClient(VISION_DESCRIPTION)
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)
    update = TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            message_id=1,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            document={"file_id": "doc-1", "mime_type": "image/png"},
        ),
    )

    await deliver(poller, update)

    assert bridge.calls[0][0] == f"{IMAGE_TAG} {VISION_DESCRIPTION}"
    assert client.downloaded_file_ids == ["doc-1"]


# --- albums (one entry per burst, every page described) -----------------------

ALBUM_ID = "album-1"
PAGES = ("page-1", "page-2", "page-3")


class PerImageVisionClient:
    """VisionClient stub answering per file id, optionally failing for some.

    The poller downloads before it describes, so the stub is keyed by the
    call order the fake client records — enough to tell "page 2 failed" from
    "page 2 was never looked at".
    """

    def __init__(self, client: "FakeTelegramClient", failing: frozenset[str] = frozenset()) -> None:
        self._client = client
        self._failing = failing
        self.described: list[str] = []

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        file_id = self._client.downloaded_file_ids[-1]
        if file_id in self._failing:
            raise RuntimeError(f"vision boom for {file_id}")
        self.described.append(file_id)
        return f"описание {file_id}"


def make_album(caption: str | None = "вот меню в картинках") -> list[TelegramUpdate]:
    """Three photos of one album; only the first carries the caption, as Telegram does."""
    return [
        make_photo_update(
            index + 1,
            file_id=file_id,
            caption=caption if index == 0 else None,
            media_group_id=ALBUM_ID,
        )
        for index, file_id in enumerate(PAGES)
    ]


async def test_album_becomes_one_message_with_every_page_described() -> None:
    """Three photos with one question is ONE act of the user's.

    Dropping the tail (what the poller used to do) answered a question about
    a three-page menu from page one; splitting it into three would make the
    uncaptioned pages material of their own, collected and answered apart
    from the question the captioned page carries.
    """
    client = FakeTelegramClient()
    vision = PerImageVisionClient(client)
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    for update in make_album():
        await poller.dispatch(update)
    await settle(poller)

    assert len(bridge.calls) == 1
    content, client_message_id, _ = bridge.calls[0]
    assert content == (
        f"{IMAGE_TAG_NUMBERED.format(index=1, total=3)} описание page-1\n\n"
        f"{IMAGE_TAG_NUMBERED.format(index=2, total=3)} описание page-2\n\n"
        f"{IMAGE_TAG_NUMBERED.format(index=3, total=3)} описание page-3\n\n"
        f"{CAPTION_LABEL} вот меню в картинках"
    )
    assert client_message_id == "1"  # the album's first item: dedup key and reply target
    assert bridge.kinds[0][0] is MessageKind.OWN  # the caption is the user speaking
    assert bridge.attachments[0] == tuple(
        Attachment(kind=AttachmentKind.IMAGE, ref=f"{REF_PREFIX}{file_id}") for file_id in PAGES
    )
    assert sorted(vision.described) == sorted(PAGES)  # every page looked at, none dropped


async def test_album_without_a_caption_is_material() -> None:
    """Same rule as a single bare photo: nothing was asked, so it collects."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, vision=PerImageVisionClient(client))
    use_bridge(poller, bridge)

    for update in make_album(caption=None):
        await poller.dispatch(update)
    await settle(poller)

    assert len(bridge.calls) == 1
    assert bridge.kinds[0][0] is MessageKind.MATERIAL
    assert CAPTION_LABEL not in bridge.calls[0][0]


async def test_album_page_that_could_not_be_described_keeps_its_slot() -> None:
    """A page the model choked on must be visible as a gap, not vanish."""
    client = FakeTelegramClient()
    vision = PerImageVisionClient(client, failing=frozenset({"page-2"}))
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    for update in make_album():
        await poller.dispatch(update)
    await settle(poller)

    content = bridge.calls[0][0]
    assert f"{IMAGE_TAG_NUMBERED.format(index=2, total=3)} {IMAGE_FAILED_PLACEHOLDER}" in content
    assert "описание page-3" in content
    # the file is still attached: `image_look` can go back to it later
    assert len(bridge.attachments[0]) == len(PAGES)


async def test_a_following_message_submits_the_album_before_itself() -> None:
    """Order is what the user typed: the album, then the question about it."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, vision=PerImageVisionClient(client))
    use_bridge(poller, bridge)

    for update in make_album(caption=None):
        await poller.dispatch(update)
    await deliver(poller, make_update(9, text="что тут по белку?"))
    await settle(poller)

    assert [call[1] for call in bridge.calls] == ["1", "9"]
    assert bridge.calls[1][0] == "что тут по белку?"


async def test_there_is_no_stop_command_left_in_the_loop() -> None:
    """Stopping is something the user asks for, not a command the poller knows.

    The router is what decides which exchanges a "стой" refers to, and it can
    say *which* of several running answers to stop — a command could only ever
    mean "all of them". So the poll loop keeps no special case: what looks
    like the old command is ordinary text and travels the ordinary path.
    """
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client)
    use_bridge(poller, bridge)

    await deliver(poller, make_update(FIRST_UPDATE_ID, text="/cancel", message_id=1001))

    assert [call[0] for call in bridge.calls] == ["/cancel"]
    assert client.sent == []


async def test_one_users_slow_ingestion_does_not_stall_another() -> None:
    """The point of the whole refactor: ingestion is per-user, the loop is shared.

    While one dialog waits on a slow vision call, another dialog's plain text
    must reach the agent — before, describing a picture froze the poll loop
    and with it every other user.
    """
    other_id = TELEGRAM_USER_ID + 1
    client = FakeTelegramClient()
    vision = SlowVisionClient()
    bridges = {USER_ID: RecordingBridge(), f"{USER_ID_PREFIX}{other_id}": RecordingBridge()}
    poller = make_poller(client, vision=vision)
    poller._registry.get_or_create = lambda user_id, chat_id: bridges[user_id]  # type: ignore[assignment,method-assign,return-value]

    await poller.dispatch(make_photo_update(1, caption="что это?"))
    await asyncio.wait_for(vision.entered.wait(), WAIT_TIMEOUT_SECONDS)

    other_user = f"{USER_ID_PREFIX}{other_id}"
    await deliver(poller, make_update(2, text="привет", sender_id=other_id), other_user)

    assert [call[0] for call in bridges[other_user].calls] == ["привет"]
    assert bridges[USER_ID].calls == []  # still blocked on its own picture
    vision.release.set()
    await settle(poller)


async def test_album_without_vision_yields_one_notice() -> None:
    """Vision off: the pre-vision behavior, one notice for the whole burst."""
    client = FakeTelegramClient()
    poller = make_poller(client)  # vision=None

    for update in make_album(caption=None):
        await poller.dispatch(update)
    await settle(poller)

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]
    assert client.downloaded_file_ids == []


class SlowVisionClient:
    """VisionClient stub that blocks until released (ordering-race tests only)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        self.entered.set()
        await self.release.wait()
        return VISION_DESCRIPTION


async def test_a_message_typed_mid_description_still_lands_after_the_album() -> None:
    """The timer can fire a second before the user types, and describing is slow.

    Without the per-user submit lock the question would reach the dialog
    ahead of the pictures it is about.
    """
    client = FakeTelegramClient()
    vision = SlowVisionClient()
    bridge = RecordingBridge()
    poller = make_poller(client, vision=vision)
    use_bridge(poller, bridge)

    for update in make_album(caption=None):
        await poller.dispatch(update)
    await asyncio.wait_for(vision.entered.wait(), WAIT_TIMEOUT_SECONDS)  # submit in flight

    typing = asyncio.create_task(poller.dispatch(make_update(9, text="что тут по белку?")))
    await asyncio.sleep(POLL_SECONDS)
    assert bridge.calls == []  # the question waits for the album, not the other way round

    vision.release.set()
    await typing
    await settle(poller)
    assert [call[1] for call in bridge.calls] == ["1", "9"]


# --- voice messages (speech ingestion) ---------------------------------------

TRANSCRIPT = "посмотри меню и подбери что-нибудь белковое"
VOICE_FILE_ID = "voice-7"
VOICE_DURATION = 12
REPLIED_TO_MESSAGE_ID = 77


class FakeTranscriptionClient:
    """TranscriptionClient stub returning a scripted transcript, recording calls."""

    def __init__(self, transcript: str = TRANSCRIPT) -> None:
        self._transcript = transcript
        self.calls: list[AudioData] = []

    async def transcribe(self, audio: AudioData) -> str:
        self.calls.append(audio)
        return self._transcript


class RaisingTranscriptionClient:
    """TranscriptionClient stub that always fails (fallback-path tests only)."""

    async def transcribe(self, audio: AudioData) -> str:
        raise RuntimeError("stt boom")


def make_voice_update(  # noqa: PLR0913, PLR0917 — a test builder mirroring the API shape
    update_id: int,
    file_id: str = VOICE_FILE_ID,
    duration: int = VOICE_DURATION,
    caption: str | None = None,
    forward_origin: TelegramForwardOrigin | None = None,
    reply_to_message_id: int | None = None,
) -> TelegramUpdate:
    """An update carrying a recorded voice note (own or forwarded)."""
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=update_id,
            from_user=TelegramUser(id=TELEGRAM_USER_ID),
            chat=TelegramChat(id=TELEGRAM_USER_ID, type=TelegramChatType.PRIVATE),
            caption=caption,
            voice=TelegramVoice(file_id=file_id, duration=duration, mime_type="audio/ogg"),
            forward_origin=forward_origin,
            reply_to_message=(
                TelegramReplyToMessage(message_id=reply_to_message_id)
                if reply_to_message_id is not None
                else None
            ),
        ),
    )


async def test_own_voice_message_is_the_user_speaking() -> None:
    """A recording the user made IS their request — the opposite of a bare photo.

    Sharing a picture asks nothing, so it collects as material; recording a
    voice message is the user talking, so the transcript opens an obligation
    and starts a run like typed text would.
    """
    client = FakeTelegramClient()
    speech = FakeTranscriptionClient()
    bridge = RecordingBridge()
    poller = make_poller(client, speech=speech)
    use_bridge(poller, bridge)

    await deliver(poller, make_voice_update(1))

    (content, client_message_id, _), (kind, origin) = bridge.calls[0], bridge.kinds[0]
    assert content == f"{VOICE_TAG} {TRANSCRIPT}"
    assert client_message_id == "1"
    assert kind is MessageKind.OWN
    assert origin is None
    assert bridge.attachments[0] == (
        Attachment(kind=AttachmentKind.AUDIO, ref=f"{REF_PREFIX}{VOICE_FILE_ID}"),
    )
    assert client.downloaded_file_ids == [VOICE_FILE_ID]
    assert client.sent == []  # no notice: the recording was understood


async def test_a_voice_reply_keeps_its_reply_target() -> None:
    """Answering by voice must resolve back to the exchange it replies to."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, speech=FakeTranscriptionClient())
    use_bridge(poller, bridge)

    await deliver(poller, make_voice_update(1, reply_to_message_id=REPLIED_TO_MESSAGE_ID))

    assert bridge.calls[0][2] == REPLIED_TO_MESSAGE_ID


async def test_forwarded_voice_is_someone_elses_words() -> None:
    """A forwarded recording is material with attribution, like forwarded text."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, speech=FakeTranscriptionClient())
    use_bridge(poller, bridge)

    await deliver(poller, make_voice_update(1, forward_origin=make_forward_origin()))

    (content, _, _), (kind, origin) = bridge.calls[0], bridge.kinds[0]
    assert content == f"[переслано от Иван] {VOICE_TAG} {TRANSCRIPT}"
    assert kind is MessageKind.MATERIAL
    assert origin == "Иван"


async def test_the_recording_travels_with_a_name_the_provider_accepts() -> None:
    """Telegram voice notes have no file name; the fallback must be a usable one."""
    client = FakeTelegramClient()
    speech = FakeTranscriptionClient()
    poller = make_poller(client, speech=speech)
    use_bridge(poller, RecordingBridge())

    await deliver(poller, make_voice_update(1))

    assert speech.calls[0].file_name == "voice.ogg"
    assert speech.calls[0].media_type == "audio/ogg"


async def test_a_mistap_is_refused_before_anything_is_downloaded() -> None:
    """Silence does not come back empty: the recognizer invents words for it.

    Measured against this deployment's model — a 0.2s silent clip produced a
    confident "Продолжение следует...", with `no_speech_prob` at 0. So the
    guard is the duration the update already carries, not the transcript.
    """
    client = FakeTelegramClient()
    speech = FakeTranscriptionClient()
    bridge = RecordingBridge()
    poller = make_poller(client, speech=speech)
    use_bridge(poller, bridge)

    await deliver(poller, make_voice_update(1, duration=0))

    assert [text for _, text, _ in client.sent] == [VOICE_TOO_SHORT_NOTICE]
    assert client.downloaded_file_ids == []
    assert speech.calls == []
    assert bridge.calls == []


async def test_a_recording_over_the_cap_is_refused_before_the_download() -> None:
    """The cap protects latency and the provider's daily audio quota alike."""
    client = FakeTelegramClient()
    speech = FakeTranscriptionClient()
    poller = make_poller(client, speech=speech, voice_max_seconds=60.0)
    use_bridge(poller, RecordingBridge())

    await deliver(poller, make_voice_update(1, duration=600))

    assert client.sent and "минут" in client.sent[0][1]
    assert client.downloaded_file_ids == []
    assert speech.calls == []


async def test_an_unintelligible_recording_asks_instead_of_guessing() -> None:
    """An empty transcript must not become an empty user message."""
    client = FakeTelegramClient()
    bridge = RecordingBridge()
    poller = make_poller(client, speech=FakeTranscriptionClient(transcript="   "))
    use_bridge(poller, bridge)

    await deliver(poller, make_voice_update(1))

    assert [text for _, text, _ in client.sent] == [VOICE_EMPTY_NOTICE]
    assert bridge.calls == []


async def test_voice_without_speech_configured_keeps_todays_notice() -> None:
    """Speech off: a recording behaves exactly like before the feature."""
    client = FakeTelegramClient()
    poller = make_poller(client)  # speech=None

    await deliver(poller, make_voice_update(1))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]
    assert client.downloaded_file_ids == []


async def test_transcription_failure_falls_back_without_propagating() -> None:
    client = FakeTelegramClient()
    poller = make_poller(client, speech=RaisingTranscriptionClient())

    await deliver(poller, make_voice_update(1))

    assert client.sent == [(TELEGRAM_USER_ID, TEXT_ONLY_NOTICE, None)]


async def test_a_person_who_changed_telegram_keeps_their_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """What the whole identity change is for. The dialog belongs to the
    person, so moving accounts must not strand it — and the bot must start
    writing to the new chat without anything else being touched."""
    manager = await make_manager([], session_factory)
    identities = SqlAlchemyIdentityStore(session_factory)
    registry = TelegramBridgeRegistry(
        runner_provider=manager.get_or_create_runner,
        client=FakeTelegramClient(),
        edit_throttle_seconds=NO_THROTTLE,
        identities=identities,
    )
    try:
        person = await identities.resolve_or_create(CHANNEL, "111")
        before = await registry.gateway_for(f"{USER_ID_PREFIX}111", 111)

        await identities.reseat(CHANNEL, person, "222")
        after = await registry.gateway_for(f"{USER_ID_PREFIX}222", 222)

        # same person, so the same dialog and the same bridge
        assert await identities.resolve(CHANNEL, "222") == person
        assert after is before
    finally:
        await registry.aclose()
        await manager.stop_all()


async def test_the_delivery_address_comes_from_the_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """It used to be parsed back out of who somebody was. A person's id
    carries no structure now, so it has to be looked up — which is the point."""
    manager = await make_manager([], session_factory)
    identities = SqlAlchemyIdentityStore(session_factory)
    registry = TelegramBridgeRegistry(
        runner_provider=manager.get_or_create_runner,
        client=FakeTelegramClient(),
        edit_throttle_seconds=NO_THROTTLE,
        identities=identities,
    )
    try:
        person = await identities.resolve_or_create(CHANNEL, str(RESEAT_ACCOUNT))

        assert chat_id_from_user_id(person) is None  # nothing to parse, by design
        assert await registry._chat_of(person) == RESEAT_ACCOUNT
    finally:
        await registry.aclose()
        await manager.stop_all()


async def test_a_pod_that_only_renders_never_polls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two readers on one token steal each other's updates, and the bot goes
    quiet in a way nothing reports."""
    polled = asyncio.Event()

    class WatchfulPoller:
        async def run_forever(self) -> None:
            polled.set()

    manager = await make_manager([], session_factory)
    registry = TelegramBridgeRegistry(
        runner_provider=manager.get_or_create_runner,
        client=FakeTelegramClient(),
        edit_throttle_seconds=0.0,
    )
    surface = TelegramSurface(
        registry=registry,
        poller=cast(TelegramPoller, WatchfulPoller()),
        polls=False,
    )
    try:
        await surface.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not polled.is_set()
    finally:
        await surface.aclose()
        await manager.stop_all()
