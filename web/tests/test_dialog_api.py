"""Tests for the dialog API."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationEvent,
    ConversationManager,
    DialogRepository,
    MessageRepository,
    MessageRole,
    ToolRegistry,
    ToolSpec,
)
from octoforge_core.agent.events import Cancelled, Failed, Finished
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ProcessInfo, RouteDecision
from octoforge_core.agent.runner import RunnerConfig
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.models import DialogRow
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
from octoforge_web.config import Settings
from octoforge_web.main import create_app

REPLY_CONTENT = "final answer"
SSE_DATA_PREFIX = "data:"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_FRAMES = 50
TEST_BASE_URL = "http://test-llm/v1"
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
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
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
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=InMemoryTaskStore(),
    )


def is_terminal(event: ConversationEvent) -> bool:
    return isinstance(event.payload, (Finished, Failed, Cancelled))


async def collect_until_terminal(queue: asyncio.Queue[ConversationEvent]) -> None:
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=EVENTS_TIMEOUT_SECONDS)
        if is_terminal(event):
            return


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/octoforge-test.db"
    settings = Settings(llm_base_url=TEST_BASE_URL, database_url=database_url)
    with TestClient(create_app(settings)) as test_client:
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
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="one"),
        reply(),
        ChatMessage(role=MessageRole.USER, content="two"),
        reply(),
    ]


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


async def test_cancel_accepted(session_factory: async_sessionmaker[AsyncSession]) -> None:
    manager = await make_manager([], session_factory)

    result = await cancel_endpoint(USER_A, CHANNEL, manager)

    assert result.status == STATUS_ACCEPTED


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
