"""Tests for the Telegram Bot API client (httpx-level, mocked transport)."""

import json

import httpx
import pytest

from octoforge_web.telegram import client as client_module
from octoforge_web.telegram.client import (
    MAX_DOWNLOADED_FILE_BYTES,
    TelegramApiError,
    TelegramBotClient,
)

BOT_TOKEN = "123:secret-token"
CHAT_ID = 42
MESSAGE_ID = 7
UPDATE_ID = 10
EXPECTED_REQUEST_COUNT = 2
THREE_REQUESTS = 3
RETRY_AFTER_SECONDS = 0.01
FILE_PATH = "photos/file_1.jpg"


def json_response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload).encode())


async def test_send_message_returns_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        assert await client.send_message(CHAT_ID, "hi") == MESSAGE_ID


async def test_send_message_passes_parse_mode_in_the_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["parse_mode"] == "HTML"
        assert payload["text"] == "<b>hi</b>"
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        assert await client.send_message(CHAT_ID, "<b>hi</b>", parse_mode="HTML") == MESSAGE_ID


async def test_send_message_retries_without_parse_mode_on_entity_errors() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "parse_mode" in payload:
            return json_response(
                {"ok": False, "description": "Bad Request: can't parse entities"},
                status_code=400,
            )
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        result = await client.send_message(CHAT_ID, "<b>oops</b>", parse_mode="HTML")

    assert result == MESSAGE_ID
    assert len(requests) == EXPECTED_REQUEST_COUNT
    assert requests[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in requests[1]


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


async def test_edit_message_rich_posts_the_rich_message_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{BOT_TOKEN}/editMessageText"
        payload = json.loads(request.content)
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID
        assert payload["rich_message"] == {"markdown": "| a |\n| --- |"}
        assert "text" not in payload
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        await client.edit_message_rich(CHAT_ID, MESSAGE_ID, "| a |\n| --- |")


async def test_edit_message_rich_swallows_not_modified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"ok": False, "description": "Bad Request: message is not modified"},
            status_code=400,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        await client.edit_message_rich(CHAT_ID, MESSAGE_ID, "same")


async def test_edit_message_rich_propagates_other_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {"ok": False, "description": "Bad Request: can't parse rich message"},
            status_code=400,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="can't parse rich message"):
            await client.edit_message_rich(CHAT_ID, MESSAGE_ID, "boom")


# --- 429 / transient retry ----------------------------------------------------


async def test_send_message_retries_after_429_honoring_retry_after() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return json_response(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 0",
                    "parameters": {"retry_after": RETRY_AFTER_SECONDS},
                },
                status_code=429,
            )
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        result = await client.send_message(CHAT_ID, "hi")

    assert result == MESSAGE_ID
    assert requests == EXPECTED_REQUEST_COUNT


async def test_retry_after_above_the_cap_gives_up_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "MAX_TOTAL_RETRY_WAIT_SECONDS", 10.0)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return json_response(
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 999",
                "parameters": {"retry_after": 999},
            },
            status_code=429,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="retry after 999"):
            await client.send_message(CHAT_ID, "hi")

    assert requests == 1  # not worth waiting for, fails on the first attempt


async def test_transient_5xx_retries_with_fixed_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "TRANSIENT_RETRY_DELAY_SECONDS", 0.0)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, content=b"Service Unavailable")
        return json_response({"ok": True, "result": {"message_id": MESSAGE_ID}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        result = await client.send_message(CHAT_ID, "hi")

    assert result == MESSAGE_ID
    assert requests == EXPECTED_REQUEST_COUNT


async def test_retry_gives_up_after_the_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "TRANSIENT_RETRY_DELAY_SECONDS", 0.0)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, content=b"Service Unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="HTTP 503"):
            await client.send_message(CHAT_ID, "hi")

    assert requests == THREE_REQUESTS  # MAX_CALL_ATTEMPTS, then gives up


# --- files (get_file / download_file) -----------------------------------------


async def test_get_file_returns_the_file_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/bot{BOT_TOKEN}/getFile"
        assert json.loads(request.content) == {"file_id": "abc"}
        return json_response({"ok": True, "result": {"file_path": FILE_PATH}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        assert await client.get_file("abc") == FILE_PATH


async def test_get_file_rejects_an_unexpected_result_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"ok": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="unexpected result shape"):
            await client.get_file("abc")


async def test_download_file_returns_the_bytes() -> None:
    content = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/file/bot{BOT_TOKEN}/{FILE_PATH}"
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        assert await client.download_file(FILE_PATH) == content


async def test_download_file_refuses_a_file_over_the_size_cap() -> None:
    oversized = b"x" * (MAX_DOWNLOADED_FILE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="too large"):
            await client.download_file(FILE_PATH)


async def test_download_file_propagates_a_hard_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError, match="HTTP 404"):
            await client.download_file(FILE_PATH)


async def test_download_file_retries_a_transient_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "TRANSIENT_RETRY_DELAY_SECONDS", 0.0)
    requests = 0
    content = b"ok-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, content=b"Service Unavailable")
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        result = await client.download_file(FILE_PATH)

    assert result == content
    assert requests == EXPECTED_REQUEST_COUNT


async def test_send_chat_action_does_not_retry() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return json_response(
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 0",
                "parameters": {"retry_after": RETRY_AFTER_SECONDS},
            },
            status_code=429,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramBotClient(http_client=http, token=BOT_TOKEN)
        with pytest.raises(TelegramApiError):
            await client.send_chat_action(CHAT_ID, "typing")

    assert requests == 1  # fire-and-forget: fails fast, no retry queue
