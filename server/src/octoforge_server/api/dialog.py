"""Dialog endpoints with SSE event streaming."""

import asyncio
from collections.abc import AsyncIterator
from http import HTTPStatus

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from octoforge_core.agent.runner import STREAM_CLOSED, DialogSubmission

from octoforge_server.api.dialog_deps import DialogActorDep
from octoforge_server.api.schemas import AckResponse, PostMessageRequest
from octoforge_server.api.sse import encode_frame, encode_heartbeat, event_to_payload

router = APIRouter(prefix="/api/dialog")

HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_MEDIA_TYPE = "text/event-stream"
STATUS_ACCEPTED = "accepted"


@router.post("/messages", status_code=HTTPStatus.ACCEPTED)
async def post_message(
    request: PostMessageRequest,
    actor: DialogActorDep,
) -> AckResponse:
    """Submit a user message; injected mid-run or starts a new run.

    `client_message_id` is an idempotency key: a retry with an
    already-recorded key is accepted but skipped (no double run).
    `reply_to_exchange_id`, if the client already knows it, skips the LLM
    router and joins the message to that exchange outright. `kind`, `origin`
    and `attachments` carry what only the surface knows: whether this is the
    user's own words or forwarded material, whom it came from, and which
    files arrived with it.
    """
    runner = await actor.manager.get_or_create_runner(actor.user_id, actor.channel)
    await runner.submit(
        DialogSubmission(
            request.content,
            client_message_id=request.client_message_id,
            reply_to_exchange_id=request.reply_to_exchange_id,
            source=request.to_source(),
        )
    )
    return AckResponse(status=STATUS_ACCEPTED)


@router.post("/cancel", status_code=HTTPStatus.ACCEPTED)
async def cancel(actor: DialogActorDep) -> AckResponse:
    """Cancel the current run of the dialog."""
    runner = await actor.manager.get_or_create_runner(actor.user_id, actor.channel)
    await runner.cancel()
    return AckResponse(status=STATUS_ACCEPTED)


@router.get("/events")
async def events(
    actor: DialogActorDep,
) -> StreamingResponse:
    """Subscribe to dialog events over SSE; the dialog is created on first contact.

    The stream ends when the runner stands down — another process owns this
    dialog now. Clients reconnect on a closed SSE stream by themselves, and
    reconnecting is what routes them to the new owner; holding the connection
    open would leave the user watching a process that will never speak again.
    """
    runner = await actor.manager.get_or_create_runner(actor.user_id, actor.channel)
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
