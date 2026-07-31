"""Tests for the dialog API."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationEvent,
    ConversationManager,
    MessageRole,
    ToolRegistry,
    ToolSpec,
)
from octoforge_core.agent.events import Cancelled, Failed, Finished
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import ManagerStores, OwnershipConfig, RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.models import DialogRow
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.tasks.store import InMemoryTaskStore
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.api.dialog import SSE_MEDIA_TYPE, STATUS_ACCEPTED
from octoforge_web.api.dialog import cancel as cancel_endpoint
from octoforge_web.api.dialog import events as events_endpoint
from octoforge_web.api.dialog import post_message as post_message_endpoint
from octoforge_web.api.schemas import PostMessageRequest
from octoforge_web.auth import hash_password
from octoforge_web.config import Settings
from octoforge_web.deps import CHANNEL_HEADER, get_channel
from octoforge_web.main import create_app
from octoforge_web.telegram.client import TELEGRAM_CHANNEL

REPLY_CONTENT = "final answer"
SSE_DATA_PREFIX = "data:"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_FRAMES = 50
TEST_BASE_URL = "http://test-llm/v1"
ADMIN_USER = "operator"
ADMIN_PASSWORD = "console-secret"
ADMIN_ITERATIONS = 1_000
EVENTS_TIMEOUT_SECONDS = 5.0
FINISHED_TYPE = "finished"
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
MAX_PROCESSES = 5
USER_A = "alice"
USER_B = "bob"
CHANNEL = "web"
SECRET_A = "alice secret question"
FIRST_CLIENT_KEY = "client-key-1"
SECOND_CLIENT_KEY = "client-key-2"
EXPECTED_HISTORY_LEN = 4
POLL_SECONDS = 0.01
EXPECTED_TWO_DIALOGS = 2
USER_ID_HEADER = "X-User-Id"


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self.requests.append(list(messages))
        return Completion(message=self._replies.pop(0))

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class PassthroughRouter:
    """MessageRouter stub always passing through (the actor starts a new process)."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        return RouteDecision()


def reply(content: str = REPLY_CONTENT) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


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
            tasks=InMemoryTaskStore(),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
        ),
        ownership=OwnershipConfig(node_id="test-node"),
    )


def is_terminal(event: ConversationEvent) -> bool:
    return isinstance(event.payload, (Finished, Failed, Cancelled))


async def collect_until_terminal(queue: asyncio.Queue[ConversationEvent]) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=EVENTS_TIMEOUT_SECONDS)
        if is_terminal(event):
            return


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Basic credentials as a header: this TestClient takes no `auth=` argument."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/octoforge-test.db"
    settings = Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=database_url,
        admin_username=ADMIN_USER,
        admin_password_hash=hash_password(ADMIN_PASSWORD, iterations=ADMIN_ITERATIONS),
    )
    # every endpoint but the health probes sits behind the operator credential
    with TestClient(
        create_app(settings), headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD)
    ) as test_client:
        yield test_client


def test_missing_user_id_header_is_rejected(client: TestClient) -> None:
    message = client.post("/api/dialog/messages", json={"content": "hi"})
    cancel = client.post("/api/dialog/cancel")
    events = client.get("/api/dialog/events")

    assert message.status_code == HTTPStatus.BAD_REQUEST
    assert cancel.status_code == HTTPStatus.BAD_REQUEST
    assert events.status_code == HTTPStatus.BAD_REQUEST


def test_empty_user_id_header_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/dialog/messages",
        json={"content": "hi"},
        headers={USER_ID_HEADER: "  "},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_post_message_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/dialog/messages",
        json={"content": "hi"},
        headers={USER_ID_HEADER: USER_A},
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json() == {"status": STATUS_ACCEPTED}


def test_post_message_accepts_reply_to_exchange_id(client: TestClient) -> None:
    response = client.post(
        "/api/dialog/messages",
        json={"content": "hi", "reply_to_exchange_id": "ex-1"},
        headers={USER_ID_HEADER: USER_A},
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json() == {"status": STATUS_ACCEPTED}


async def test_dialog_is_get_or_created_per_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([reply(), reply(), reply()], session_factory)
    runner_a = await manager.get_or_create_runner(USER_A, CHANNEL)
    runner_b = await manager.get_or_create_runner(USER_B, CHANNEL)
    queue_a = runner_a.subscribe()
    queue_b = runner_b.subscribe()

    await post_message_endpoint(PostMessageRequest(content="one"), USER_A, CHANNEL, manager)
    await collect_until_terminal(queue_a)
    await post_message_endpoint(PostMessageRequest(content="two"), USER_A, CHANNEL, manager)
    await collect_until_terminal(queue_a)
    await post_message_endpoint(PostMessageRequest(content="three"), USER_B, CHANNEL, manager)
    await collect_until_terminal(queue_b)

    assert await manager.get_or_create_runner(USER_A, CHANNEL) is runner_a
    assert await manager.get_or_create_runner(USER_B, CHANNEL) is runner_b
    async with session_factory() as session:
        dialogs = (await session.scalars(select(DialogRow))).all()
    assert len(dialogs) == EXPECTED_TWO_DIALOGS
    # settle-exchange writes happen after the terminal event: stop the actors
    # before the fixture disposes the engine, or a pump races a closed pool
    await manager.stop_all()
    assert {row.user_id for row in dialogs} == {USER_A, USER_B}
    assert all(row.channel == CHANNEL for row in dialogs)


async def test_post_message_deduplicates_client_message_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([reply(), reply()], session_factory)
    runner = await manager.get_or_create_runner(USER_A, CHANNEL)
    queue = runner.subscribe()

    await post_message_endpoint(
        PostMessageRequest(content="one", client_message_id=FIRST_CLIENT_KEY),
        USER_A,
        CHANNEL,
        manager,
    )
    await collect_until_terminal(queue)
    await post_message_endpoint(  # a client retry with the same key
        PostMessageRequest(content="one", client_message_id=FIRST_CLIENT_KEY),
        USER_A,
        CHANNEL,
        manager,
    )
    await post_message_endpoint(
        PostMessageRequest(content="two", client_message_id=SECOND_CLIENT_KEY),
        USER_A,
        CHANNEL,
        manager,
    )
    await collect_until_terminal(queue)

    async def _history_complete() -> None:  # finalize persists after the terminal event
        while len(runner.history()) < EXPECTED_HISTORY_LEN:
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_history_complete(), timeout=EVENTS_TIMEOUT_SECONDS)
    # assistant messages carry the producing task's id since the broker refactor;
    # the dedup contract is about contents and order
    assert [message.content for message in runner.history()] == [
        "one",
        REPLY_CONTENT,
        "two",
        REPLY_CONTENT,
    ]
    await manager.stop_all()


async def test_users_are_isolated(session_factory: async_sessionmaker[AsyncSession]) -> None:
    manager = await make_manager([reply()], session_factory)
    runner_a = await manager.get_or_create_runner(USER_A, CHANNEL)
    runner_b = await manager.get_or_create_runner(USER_B, CHANNEL)
    queue_a = runner_a.subscribe()
    queue_b = runner_b.subscribe()

    await post_message_endpoint(PostMessageRequest(content=SECRET_A), USER_A, CHANNEL, manager)
    await collect_until_terminal(queue_a)

    assert queue_b.empty()
    assert all(SECRET_A not in message.content for message in runner_b.history())
    await manager.stop_all()


async def test_post_message_forwards_reply_to_exchange_id(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reply_to_exchange_id` on the request reaches `ConversationRunner.submit`.

    A Telegram (or other) transport that already resolved an explicit reply
    passes the exchange id straight through the API, skipping the LLM
    router (`ConversationRunner.submit`'s deterministic reply shortcut).
    """
    manager = await make_manager([], session_factory)
    runner = await manager.get_or_create_runner(USER_A, CHANNEL)
    submitted: list[tuple[str, str | None, str | None]] = []

    async def fake_submit(
        content: str,
        client_message_id: str | None = None,
        reply_to_exchange_id: str | None = None,
    ) -> None:
        submitted.append((content, client_message_id, reply_to_exchange_id))

    monkeypatch.setattr(runner, "submit", fake_submit)

    await post_message_endpoint(
        PostMessageRequest(content="hi", reply_to_exchange_id="ex-1"),
        USER_A,
        CHANNEL,
        manager,
    )

    assert submitted == [("hi", None, "ex-1")]
    await manager.stop_all()


async def test_cancel_accepted(session_factory: async_sessionmaker[AsyncSession]) -> None:
    manager = await make_manager([], session_factory)

    result = await cancel_endpoint(USER_A, CHANNEL, manager)

    assert result.status == STATUS_ACCEPTED
    await manager.stop_all()


async def test_events_endpoint_streams_frames(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager([reply()], session_factory)

    response = await events_endpoint(USER_A, CHANNEL, manager)

    assert response.media_type == SSE_MEDIA_TYPE

    async def collect_frames() -> list[dict[str, object]]:
        runner = await manager.get_or_create_runner(USER_A, CHANNEL)
        await runner.submit("hi")
        payloads: list[dict[str, object]] = []
        async for frame in response.body_iterator:
            text = frame if isinstance(frame, str) else frame.decode()
            if not text.startswith(SSE_DATA_PREFIX):
                continue
            payload = json.loads(text[len(SSE_DATA_PREFIX) :])
            payloads.append(payload)
            if payload["type"] == FINISHED_TYPE or len(payloads) >= MAX_FRAMES:
                break
        return payloads

    payloads = await asyncio.wait_for(collect_frames(), timeout=EVENTS_TIMEOUT_SECONDS)

    types = [payload["type"] for payload in payloads]
    assert "text_delta" in types
    assert "assistant_message" in types
    assert types[-1] == FINISHED_TYPE
    assert payloads[-1]["content"] == REPLY_CONTENT
    assert all(payload["dialog_id"] for payload in payloads)
    seqs = [payload["seq"] for payload in payloads]
    assert seqs == sorted(seqs)
    await manager.stop_all()


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


def test_health_ready_reports_database(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ready", "database": "ok"}


def test_index_page_is_served(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == HTTPStatus.OK
    assert "text/html" in response.headers["content-type"]


async def test_events_endpoint_ends_the_stream_when_the_dialog_moves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A client left hanging on the previous owner sees the agent go silent.
    Ending the stream is what makes it reconnect — and reconnecting is what
    routes it to whoever runs the dialog now.
    """
    manager = await make_manager([reply()], session_factory)
    response = await events_endpoint(USER_A, CHANNEL, manager)
    runner = await manager.get_or_create_runner(USER_A, CHANNEL)

    await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, "another-node")
    await manager._beat_once()

    async def drain_until_closed() -> list[str]:
        frames: list[str] = []
        async for frame in response.body_iterator:
            frames.append(frame if isinstance(frame, str) else frame.decode())
        return frames

    frames = await asyncio.wait_for(drain_until_closed(), timeout=EVENTS_TIMEOUT_SECONDS)

    # the stream ends rather than emitting a terminal frame: there is nothing
    # left to say, and the client's own reconnect is the recovery
    assert not [frame for frame in frames if frame.startswith(SSE_DATA_PREFIX)]
    await manager.stop_all()


def channel_request(headers: dict[str, str], declared: str = CHANNEL) -> Request:
    """A request carrying only what `get_channel` looks at."""
    app = SimpleNamespace(state=SimpleNamespace(channel=declared))
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_a_request_without_a_channel_addresses_the_one_the_process_serves() -> None:
    """Every existing client sends no channel at all and must keep working."""
    assert get_channel(channel_request({})) == CHANNEL


def test_a_named_channel_addresses_that_surface() -> None:
    """One process serving several surfaces is the point: without it a
    deployment needs a fleet per channel, and balancing users across a single
    fleet has nothing to balance."""
    assert get_channel(channel_request({CHANNEL_HEADER: TELEGRAM_CHANNEL})) == TELEGRAM_CHANNEL


def test_an_unknown_channel_is_refused() -> None:
    """An accepted typo would strand the user's messages in a dialog nobody reads."""
    with pytest.raises(HTTPException) as raised:
        get_channel(channel_request({CHANNEL_HEADER: "telgram"}))

    assert raised.value.status_code == HTTPStatus.BAD_REQUEST


def test_an_unknown_channel_is_refused_over_http(client: TestClient) -> None:
    response = client.post(
        "/api/dialog/messages",
        json={"content": "hi"},
        headers={USER_ID_HEADER: USER_A, CHANNEL_HEADER: "telgram"},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
