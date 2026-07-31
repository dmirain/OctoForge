"""Dialog endpoints with SSE event streaming."""

import asyncio
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from octoforge_core import ConversationManager
from octoforge_core.agent.runner import STREAM_CLOSED

from octoforge_web.api.schemas import AckResponse, PostMessageRequest
from octoforge_web.api.sse import encode_frame, encode_heartbeat, event_to_payload
from octoforge_web.deps import get_channel, get_conversation_manager, get_user_id

router = APIRouter(prefix="/api/dialog")

HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_MEDIA_TYPE = "text/event-stream"
STATUS_ACCEPTED = "accepted"

ManagerDep = Annotated[ConversationManager, Depends(get_conversation_manager)]
UserIdDep = Annotated[str, Depends(get_user_id)]
ChannelDep = Annotated[str, Depends(get_channel)]


@router.post("/messages", status_code=HTTPStatus.ACCEPTED)
async def post_message(
    request: PostMessageRequest,
    user_id: UserIdDep,
    channel: ChannelDep,
    manager: ManagerDep,
) -> AckResponse:
    """Submit a user message; injected mid-run or starts a new run.

    `client_message_id` is an idempotency key: a retry with an
    already-recorded key is accepted but skipped (no double run).
    `reply_to_exchange_id`, if the client already knows it, skips the LLM
    router and joins the message to that exchange outright.
    """
    runner = await manager.get_or_create_runner(user_id, channel)
    await runner.submit(
        request.content,
        client_message_id=request.client_message_id,
        reply_to_exchange_id=request.reply_to_exchange_id,
    )
    return AckResponse(status=STATUS_ACCEPTED)


@router.post("/cancel", status_code=HTTPStatus.ACCEPTED)
async def cancel(user_id: UserIdDep, channel: ChannelDep, manager: ManagerDep) -> AckResponse:
    """Cancel the current run of the dialog."""
    runner = await manager.get_or_create_runner(user_id, channel)
    await runner.cancel()
    return AckResponse(status=STATUS_ACCEPTED)


@router.get("/events")
async def events(
    user_id: UserIdDep,
    channel: ChannelDep,
    manager: ManagerDep,
) -> StreamingResponse:
    """Subscribe to dialog events over SSE; the dialog is created on first contact.

    The stream ends when the runner stands down — another process owns this
    dialog now. Clients reconnect on a closed SSE stream by themselves, and
    reconnecting is what routes them to the new owner; holding the connection
    open would leave the user watching a process that will never speak again.
    """
    runner = await manager.get_or_create_runner(user_id, channel)
    queue = runner.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except TimeoutError:
                    yield encode_heartbeat()
                    continue
                if event is STREAM_CLOSED:
                    return
                yield encode_frame(event_to_payload(event))
        finally:
            runner.unsubscribe(queue)

    return StreamingResponse(stream(), media_type=SSE_MEDIA_TYPE)
