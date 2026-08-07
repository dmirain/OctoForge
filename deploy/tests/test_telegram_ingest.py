"""Ingestion over HTTP: what the node sends, and who is allowed to read.

The node exists because of one Bot API fact — a token may be long-polled by
exactly one process — so the rules worth asserting are about the boundary it
draws: what crosses it, and that nobody polls twice.
"""

import json
from contextlib import AsyncExitStack

import httpx
import pytest
from fastapi import HTTPException, Request
from octoforge_core.domain import Attachment, AttachmentKind, MessageKind
from octoforge_server.auth import AuthGate, hash_password
from octoforge_server.config import Settings
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.gateway import (
    AccessRefusedError,
    ApiGatewayRegistry,
    basic_auth_header,
)
from octoforge_telegram.ingest import __main__ as ingest_main
from octoforge_telegram.ingest.__main__ import _build, run_ingest, service_headers
from octoforge_telegram.media_client import ApiMediaUnderstanding

CHAT_ID = 424242
HANDLE = f"tg:{CHAT_ID}"
SERVICE_USER = "telegram-ingest"
SERVICE_PASSWORD = "a-long-generated-secret"
DIALOG_PATH = "/api/dialog/messages"
#: how many probe attempts the boot-race test makes the service refuse first
BOOT_ATTEMPTS = 3


@pytest.fixture(autouse=True)
def instant_media_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real waiting: `_build` probes the service, which no test here runs.

    Left alone, every test that assembles the node would sit through the
    startup probe's full retry budget — thirty seconds each of waiting for a
    balancer that was never there.
    """
    monkeypatch.setattr(ingest_main, "MEDIA_PROBE_DELAY_SECONDS", 0.0)


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
        await gateway.handle_text("привет")

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


async def test_a_status_refusal_arrives_as_a_verdict_the_surface_can_speak() -> None:
    """403 from the status gate is not a transport failure to log and forget.

    This process cannot resolve a person, so the service decides admission —
    and if its verdict did not survive the boundary, a queued newcomer would
    get silence instead of being told there is a queue.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"detail": "registration is pending"}, headers={"X-Access-Status": "waiting"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)

        with pytest.raises(AccessRefusedError) as refusal:
            await gateway.handle_text("привет")

    assert refusal.value.status == "waiting"


async def test_a_403_without_the_header_stays_an_ordinary_failure() -> None:
    """Only the admission gate speaks that header; anything else 403 is a bug
    to surface, not a message to relay to the user as a queue notice."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        gateway = await ApiGatewayRegistry(client).gateway_for(HANDLE, CHAT_ID)

        with pytest.raises(httpx.HTTPStatusError):
            await gateway.handle_text("привет")


async def test_the_node_hands_out_referral_links() -> None:
    """The referral store lives in the surface's own database, which this node
    has — so /invite must work here exactly as it does in-process. Its first
    deployment shipped without it and the command answered "not set up"."""
    settings = Settings(service_username=SERVICE_USER, service_password=SERVICE_PASSWORD)
    telegram = TelegramSettings(
        telegram_bot_token="123:abc",
        telegram_service_url="http://balancer",
        telegram_database_url="sqlite+aiosqlite:///:memory:",
        telegram_bot_username="@octoforge_test_bot",
    )
    async with AsyncExitStack() as stack:
        poller = await _build(stack, settings, telegram)
        assert poller._referrals is not None
        assert poller._bot_username == "octoforge_test_bot"


async def test_the_node_owns_no_model_client_of_its_own() -> None:
    """The whole point of the move: this process cannot resolve a person, so
    a model call made here could be neither checked against a plan nor
    ledgered. It asks the service, whatever its own settings happen to say."""
    settings = Settings(
        service_username=SERVICE_USER,
        service_password=SERVICE_PASSWORD,
        vision_model="test-vision",
        stt_model="test-whisper",
        stt_base_url="https://stt.example",
        llm_base_url="https://llm.example",
    )
    telegram = TelegramSettings(
        telegram_bot_token="123:abc",
        telegram_service_url="http://balancer",
        telegram_database_url="sqlite+aiosqlite:///:memory:",
    )
    async with AsyncExitStack() as stack:
        poller = await _build(stack, settings, telegram)

    assert isinstance(poller._media, ApiMediaUnderstanding)


async def test_the_node_says_what_media_it_will_actually_get() -> None:
    """The descendant of a real incident: shipped without model clients, the
    poller degraded silently to text-only and images and voice "stopped
    working" for a day, while every capability report that mentioned them
    belonged to a different process. This one reports what it will rely on."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"describes_images": True, "transcribes_audio": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        reported = await ApiMediaUnderstanding(client).capabilities()

    assert seen == ["/api/media/capabilities"]
    assert reported == {"describes_images": True, "transcribes_audio": False}


async def test_an_unreachable_service_is_reported_rather_than_assumed() -> None:
    """Silence is what the incident looked like; None makes the node say so."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        assert await ApiMediaUnderstanding(client).capabilities() is None


async def test_the_startup_probe_outlasts_the_services_boot() -> None:
    """A rollout restarts both at once, so the first answer is often a 503 from
    a service that is fine seconds later. The first deploy of this probe logged
    'the service did not answer' against exactly that, and a one-shot report
    that is wrong for the rest of the process's life is worse than none."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < BOOT_ATTEMPTS:
            return httpx.Response(503, json={"detail": "still booting"})
        return httpx.Response(200, json={"describes_images": True, "transcribes_audio": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://balancer"
    ) as client:
        await ingest_main._report_media(ApiMediaUnderstanding(client))

    assert len(attempts) == BOOT_ATTEMPTS  # it kept asking until the service was up
