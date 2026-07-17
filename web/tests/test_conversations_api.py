"""Tests for the conversations API."""

import asyncio
import json
from collections.abc import AsyncIterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from octoforge_core import (
    AgentLoop,
    ChatMessage,
    ConversationManager,
    MessageRole,
    SkillRegistry,
    SkillSpec,
)
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta

from octoforge_web.api.conversations import SSE_MEDIA_TYPE
from octoforge_web.api.conversations import events as events_endpoint
from octoforge_web.config import Settings
from octoforge_web.deps import get_conversation_manager
from octoforge_web.main import create_app

REPLY_CONTENT = "final answer"
SSE_DATA_PREFIX = "data:"
SYSTEM_PROMPT = "test prompt"
MAX_ITERATIONS = 3
MAX_FRAMES = 50
UNKNOWN_ID = "missing-conversation"
TEST_BASE_URL = "http://test-llm/v1"
EVENTS_TIMEOUT_SECONDS = 5.0
FINISHED_TYPE = "finished"


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        self.requests.append(list(messages))
        return self._replies.pop(0)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


def reply(content: str = REPLY_CONTENT) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def make_manager(replies: list[ChatMessage]) -> ConversationManager:
    loop = AgentLoop(
        llm_client=ScriptedLLM(replies),
        registry=SkillRegistry(),
        max_iterations=MAX_ITERATIONS,
    )
    return ConversationManager(loop=loop, system_prompt=SYSTEM_PROMPT)


@pytest.fixture
def client() -> TestClient:
    app = create_app(Settings(llm_base_url=TEST_BASE_URL))
    manager = make_manager([reply()])
    app.dependency_overrides[get_conversation_manager] = lambda: manager
    return TestClient(app)


def create_conversation(client: TestClient) -> str:
    response = client.post("/api/conversations")
    assert response.status_code == HTTPStatus.CREATED
    return str(response.json()["id"])


def test_create_conversation(client: TestClient) -> None:
    conversation_id = create_conversation(client)

    assert conversation_id


def test_post_message_accepted(client: TestClient) -> None:
    conversation_id = create_conversation(client)

    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "hi"},
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json() == {"status": "accepted"}


async def test_events_endpoint_streams_frames() -> None:
    manager = make_manager([reply()])
    conversation_id = manager.create_conversation()

    response = await events_endpoint(conversation_id, manager)

    assert response.media_type == SSE_MEDIA_TYPE

    async def collect_frames() -> list[dict[str, object]]:
        await manager.get(conversation_id).submit("hi")
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
    seqs = [payload["seq"] for payload in payloads]
    assert seqs == sorted(seqs)


def test_cancel_accepted(client: TestClient) -> None:
    conversation_id = create_conversation(client)

    response = client.post(f"/api/conversations/{conversation_id}/cancel")

    assert response.status_code == HTTPStatus.ACCEPTED


def test_unknown_conversation_returns_404(client: TestClient) -> None:
    message = client.post(f"/api/conversations/{UNKNOWN_ID}/messages", json={"content": "hi"})
    cancel = client.post(f"/api/conversations/{UNKNOWN_ID}/cancel")
    events = client.get(f"/api/conversations/{UNKNOWN_ID}/events")

    assert message.status_code == HTTPStatus.NOT_FOUND
    assert cancel.status_code == HTTPStatus.NOT_FOUND
    assert events.status_code == HTTPStatus.NOT_FOUND


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


def test_index_page_is_served(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == HTTPStatus.OK
    assert "text/html" in response.headers["content-type"]
