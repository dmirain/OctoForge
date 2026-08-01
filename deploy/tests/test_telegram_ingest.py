"""Ingestion over HTTP: what the node sends, and who is allowed to read.

The node exists because of one Bot API fact — a token may be long-polled by
exactly one process — so the rules worth asserting are about the boundary it
draws: what crosses it, and that nobody polls twice.
"""

import json

import httpx
import pytest
from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_telegram.gateway import ApiGatewayRegistry, basic_auth_header

CHAT_ID = 424242
HANDLE = f"tg:{CHAT_ID}"


def recording_client(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"status": "accepted"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://balancer",
        headers=basic_auth_header("ingest", "secret"),
    )


async def test_the_node_sends_the_account_not_a_person() -> None:
    """Which person an account belongs to is the service's answer. Asking here
    would put a database this process does not otherwise need behind every
    message."""
    seen: list[httpx.Request] = []
    async with recording_client(seen) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)
        await gateway.handle_text("привет")

    assert seen[0].headers["X-User-Id"] == str(CHAT_ID)
    assert seen[0].headers["X-Channel"] == "telegram"


async def test_forwarded_material_and_files_survive_the_boundary() -> None:
    """An out-of-process surface must be able to say everything an in-process
    one could, or a forward becomes an ordinary question."""
    seen: list[httpx.Request] = []
    async with recording_client(seen) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)
        await gateway.handle_text(
            "смотри",
            kind=MessageKind.MATERIAL,
            origin="Иван",
            attachments=(Attachment(kind=AttachmentKind.IMAGE, ref="tgfile:abc"),),
        )

    body = json.loads(seen[0].content)
    assert body["kind"] == MessageKind.MATERIAL.value
    assert body["origin"] == "Иван"
    assert body["attachments"] == [{"kind": "image", "ref": "tgfile:abc"}]


async def test_the_node_carries_a_credential() -> None:
    """It reaches an internal service; unauthenticated it would simply be refused."""
    seen: list[httpx.Request] = []
    async with recording_client(seen) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)
        await gateway.cancel()

    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_a_refusal_is_not_swallowed() -> None:
    """A message the service rejected must not look delivered — Telegram will
    resend the update, which is the retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)

        with pytest.raises(httpx.HTTPStatusError):
            await gateway.handle_text("привет")
