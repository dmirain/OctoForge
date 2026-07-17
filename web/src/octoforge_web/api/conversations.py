"""Conversation endpoints with SSE event streaming."""

import asyncio
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from octoforge_core import ConversationManager

from octoforge_web.api.schemas import AckResponse, CreateConversationResponse, PostMessageRequest
from octoforge_web.api.sse import encode_frame, encode_heartbeat, event_to_payload
from octoforge_web.deps import get_conversation_manager

router = APIRouter(prefix="/api/conversations")

HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_MEDIA_TYPE = "text/event-stream"
STATUS_ACCEPTED = "accepted"

ManagerDep = Annotated[ConversationManager, Depends(get_conversation_manager)]


@router.post("", status_code=HTTPStatus.CREATED)
async def create_conversation(manager: ManagerDep) -> CreateConversationResponse:
    """Create a new conversation and return its id."""
    return CreateConversationResponse(id=manager.create_conversation())


@router.post("/{conversation_id}/messages", status_code=HTTPStatus.ACCEPTED)
async def post_message(
    conversation_id: str,
    request: PostMessageRequest,
    manager: ManagerDep,
) -> AckResponse:
    """Submit a user message; injected mid-run or starts a new run."""
    await manager.get(conversation_id).submit(request.content)
    return AckResponse(status=STATUS_ACCEPTED)


@router.post("/{conversation_id}/cancel", status_code=HTTPStatus.ACCEPTED)
async def cancel(conversation_id: str, manager: ManagerDep) -> AckResponse:
    """Cancel the current run of the conversation."""
    await manager.get(conversation_id).cancel()
    return AckResponse(status=STATUS_ACCEPTED)


@router.get("/{conversation_id}/events")
async def events(conversation_id: str, manager: ManagerDep) -> StreamingResponse:
    """Subscribe to conversation events over SSE."""
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except TimeoutError:
                    yield encode_heartbeat()
                    continue
                yield encode_frame(event_to_payload(event))
        finally:
            runner.unsubscribe(queue)

    return StreamingResponse(stream(), media_type=SSE_MEDIA_TYPE)
