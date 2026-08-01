"""Tests for the Telegram-backed ImageResolver (httpx-level, mocked transport)."""

import json

import httpx
import pytest
from octoforge_core.vision.api import ImageData, VisionUnavailableError
from octoforge_telegram.client import USER_ID_PREFIX, TelegramBotClient
from octoforge_telegram.images import (
    LEGACY_REF_PREFIX,
    REF_PREFIX,
    TelegramImageResolver,
)

BOT_TOKEN = "123:secret-token"
FILE_ID = "abc123"
FILE_PATH = "photos/file_1.jpg"
PNG_FILE_PATH = "documents/file_2.png"
WEBP_FILE_PATH = "documents/file_3.webp"
IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def json_response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload).encode())


def _resolver(handler: httpx.MockTransport) -> tuple[TelegramImageResolver, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=handler)
    client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
    return TelegramImageResolver(client), http


async def test_fetch_returns_image_data_for_a_telegram_ref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            assert json.loads(request.content) == {"file_id": FILE_ID}
            return json_response({"ok": True, "result": {"file_path": FILE_PATH}})
        assert request.url.path == f"/file/bot{BOT_TOKEN}/{FILE_PATH}"
        return httpx.Response(200, content=IMAGE_BYTES)

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        result = await resolver.fetch(f"tg:{FILE_ID}")

    assert result == ImageData(content=IMAGE_BYTES, media_type="image/jpeg")


async def test_fetch_infers_media_type_from_extension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            return json_response({"ok": True, "result": {"file_path": PNG_FILE_PATH}})
        return httpx.Response(200, content=IMAGE_BYTES)

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        result = await resolver.fetch(f"tg:{FILE_ID}")

    assert result.media_type == "image/png"


async def test_fetch_infers_webp_media_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            return json_response({"ok": True, "result": {"file_path": WEBP_FILE_PATH}})
        return httpx.Response(200, content=IMAGE_BYTES)

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        result = await resolver.fetch(f"tg:{FILE_ID}")

    assert result.media_type == "image/webp"


async def test_fetch_rejects_a_foreign_ref_without_calling_telegram() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({"ok": True, "result": {"file_path": FILE_PATH}})

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        with pytest.raises(VisionUnavailableError, match="not a Telegram image ref"):
            await resolver.fetch("other:xyz")

    assert calls == 0


async def test_fetch_rejects_a_malformed_ref_with_no_prefix() -> None:
    resolver, http = _resolver(httpx.MockTransport(lambda request: json_response({"ok": True})))
    async with http:
        with pytest.raises(VisionUnavailableError):
            await resolver.fetch(FILE_ID)


async def test_fetch_wraps_a_telegram_failure_as_vision_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"ok": False, "description": "file not found"})

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        with pytest.raises(VisionUnavailableError, match="could not fetch image"):
            await resolver.fetch(f"tg:{FILE_ID}")


async def test_fetch_wraps_a_download_failure_as_vision_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            return json_response({"ok": True, "result": {"file_path": FILE_PATH}})
        return httpx.Response(404, content=b"Not Found")

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        with pytest.raises(VisionUnavailableError, match="could not fetch image"):
            await resolver.fetch(f"tg:{FILE_ID}")


async def test_a_ref_written_before_the_rename_still_resolves() -> None:
    """Attachments live in the message history forever, so the old `tg:` prefix
    has to keep working — a picture sent last month must still resolve."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            return json_response({"ok": True, "result": {"file_path": FILE_PATH}})
        return httpx.Response(200, content=IMAGE_BYTES)

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        result = await resolver.fetch(f"{LEGACY_REF_PREFIX}{FILE_ID}")

    assert result == ImageData(content=IMAGE_BYTES, media_type="image/jpeg")


async def test_a_ref_written_today_carries_the_new_prefix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/bot{BOT_TOKEN}/getFile":
            assert json.loads(request.content) == {"file_id": FILE_ID}
            return json_response({"ok": True, "result": {"file_path": FILE_PATH}})
        return httpx.Response(200, content=IMAGE_BYTES)

    resolver, http = _resolver(httpx.MockTransport(handler))
    async with http:
        result = await resolver.fetch(f"{REF_PREFIX}{FILE_ID}")

    assert result == ImageData(content=IMAGE_BYTES, media_type="image/jpeg")


def test_an_attachment_ref_no_longer_shares_a_namespace_with_a_user_id() -> None:
    """`tg:` was both a Telegram user id and an attachment ref. Two kinds of
    identifier in one namespace only need one prefix check to be confused."""
    assert not REF_PREFIX.startswith(USER_ID_PREFIX)
    assert not USER_ID_PREFIX.startswith(REF_PREFIX)
