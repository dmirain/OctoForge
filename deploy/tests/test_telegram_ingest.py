"""Ingestion over HTTP: what the node sends, and who is allowed to read.

The node exists because of one Bot API fact — a token may be long-polled by
exactly one process — so the rules worth asserting are about the boundary it
draws: what crosses it, and that nobody polls twice.
"""

import json

import httpx
import pytest
from fastapi import HTTPException, Request
from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_server.auth import AuthGate, hash_password
from octoforge_server.config import Settings
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.gateway import ApiGatewayRegistry, basic_auth_header
from octoforge_telegram.ingest.__main__ import run_ingest, service_headers

CHAT_ID = 424242
HANDLE = f"tg:{CHAT_ID}"
SERVICE_USER = "telegram-ingest"
SERVICE_PASSWORD = "a-long-generated-secret"
DIALOG_PATH = "/api/dialog/messages"


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


def _request(headers: dict[str, str], path: str = DIALOG_PATH) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
            "query_string": b"",
            "client": ("10.0.0.7", 51234),
            "scheme": "http",
            "server": ("service", 8000),
        }
    )


async def test_the_credential_the_node_presents_is_one_the_service_accepts() -> None:
    """The two halves of this credential live in two processes, and only their
    fit makes the node work at all. Nothing else asserts it: the gate's own
    tests build the header themselves, so the node could present anything.

    It did. It sent the *hash* — which the gate hashes again and refuses — and
    every message was dropped into a 401 nobody was looking at.
    """
    node = Settings(service_username=SERVICE_USER, service_password=SERVICE_PASSWORD)
    gate = AuthGate(
        username="admin",
        password_hash=hash_password("operator-secret"),
        service_username=SERVICE_USER,
        service_password_hash=hash_password(SERVICE_PASSWORD),
    )

    await gate.authenticate(_request(service_headers(node)), service_allowed=True)


async def test_the_nodes_credential_is_still_only_a_relays() -> None:
    """Fitting the gate must not have handed it operator power."""
    node = Settings(service_username=SERVICE_USER, service_password=SERVICE_PASSWORD)
    gate = AuthGate(
        username="admin",
        password_hash=hash_password("operator-secret"),
        service_username=SERVICE_USER,
        service_password_hash=hash_password(SERVICE_PASSWORD),
    )

    with pytest.raises(HTTPException):
        await gate.authenticate(
            _request(service_headers(node), path="/api/admin/dialogs"), service_allowed=False
        )


async def test_a_node_with_no_credential_refuses_to_start() -> None:
    """Starting anyway is the worst of both: it polls, so no other process may,
    and every update it takes is dropped on a 401. Better to not start."""
    telegram = TelegramSettings(
        telegram_bot_token="123:abc", telegram_service_url="http://balancer"
    )

    with pytest.raises(SystemExit):
        await run_ingest(Settings(service_username=SERVICE_USER, service_password=""), telegram)


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
