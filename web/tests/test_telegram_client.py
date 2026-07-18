"""Tests for the Telegram Bot API client (httpx-level, mocked transport)."""

import json

import httpx
import pytest

from octoforge_web.telegram.client import TelegramApiError, TelegramBotClient

BOT_TOKEN = "123:secret-token"
CHAT_ID = 42
MESSAGE_ID = 7
UPDATE_ID = 10


def json_response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload).encode())


async def test_send_message_returns_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        assert await client.send_message(CHAT_ID, "hi") == MESSAGE_ID


async def test_get_updates_parses_models() -> None:
    updates = [
        {
            "update_id": UPDATE_ID,
            "message": {
                "message_id": MESSAGE_ID,
                "from": {"id": 99},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "hello",
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"ok": True, "result": updates})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        result = await client.get_updates(None, 0)
    assert len(result) == 1
    assert result[0].update_id == UPDATE_ID
    assert result[0].message is not None
    assert result[0].message.text == "hello"


async def test_api_error_carries_description() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"ok": False, "description": "chat not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="chat not found"):
            await client.send_message(CHAT_ID, "hi")


async def test_http_error_hides_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="HTTP 404") as exc_info:
            await client.send_message(CHAT_ID, "hi")
    assert BOT_TOKEN not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
