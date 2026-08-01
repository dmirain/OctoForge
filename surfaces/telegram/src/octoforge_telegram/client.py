"""Minimal Telegram Bot API client over plain httpx."""

import asyncio
import logging
from typing import Any, NoReturn, Protocol

import httpx
from pydantic import ValidationError

from octoforge_telegram.models import TelegramUpdate

TELEGRAM_CHANNEL = "telegram"
USER_ID_PREFIX = "tg:"
# the plain-text `sendMessage` budget, which bounds the poller's notices —
# answers do not travel this way and are bounded by the Rich Message limit
MAX_MESSAGE_LENGTH = 4096
# 32,768 UTF-8 characters, alongside 500 blocks, 16 nesting levels, 50 media
# attachments and 20 table columns (Bot API 10.1, "Rich Message Limits")
MAX_RICH_MESSAGE_LENGTH = 32768
API_BASE_URL = "https://api.telegram.org"
CHAT_ACTION_TYPING = "typing"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
LONG_POLL_TIMEOUT_MARGIN_SECONDS = 10.0
NOT_MODIFIED_MARKER = "message is not modified"
CANT_PARSE_ENTITIES_MARKER = "can't parse entities"
ALLOWED_UPDATES = ("message",)
TOO_MANY_REQUESTS_STATUS = 429
RETRYABLE_HTTP_STATUS_CODES = frozenset({500, 502, 503, 504})
# 1 initial attempt + up to 2 retries; a bounded, honest retry, not a rate limiter.
MAX_CALL_ATTEMPTS = 3
MAX_TOTAL_RETRY_WAIT_SECONDS = 10.0
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
# a bot must not become a memory hog over a single incoming picture
MAX_DOWNLOADED_FILE_BYTES = 12 * 1024 * 1024

logger = logging.getLogger(__name__)


class TelegramApiError(Exception):
    """The Bot API answered `ok=false` for a method call."""


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read a streaming response, refusing as soon as it crosses `limit` bytes."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        logger.warning("downloadFile: declared %s bytes over the %d cap; refusing", declared, limit)
        raise TelegramApiError("downloadFile: file too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            logger.warning("downloadFile: exceeded the %d byte cap mid-download; refusing", limit)
            raise TelegramApiError("downloadFile: file too large")
        chunks.append(chunk)
    return b"".join(chunks)


class _RetryableCallError(Exception):
    """Internal signal: the call failed but is worth retrying after `wait_seconds`."""

    def __init__(self, wait_seconds: float, message: str) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


class TelegramClient(Protocol):
    """Port of the Telegram Bot API used by the adapter."""

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        """Fetch updates after `offset`, long-polling up to `timeout_seconds`."""
        ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a text message, optionally as a reply; return its message id."""
        ...

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        """Replace the text of an existing message."""
        ...

    async def send_rich_message(
        self, chat_id: int, markdown: str, reply_to_message_id: int | None = None
    ) -> int:
        """Send a Rich Message from raw Markdown, optionally as a reply."""
        ...

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        """Replace an existing message's content with a Rich Message from raw Markdown."""
        ...

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        """Send a chat action (e.g. typing)."""
        ...

    async def get_file(self, file_id: str) -> str:
        """Resolve a file id to its Bot API file path, for `download_file`."""
        ...

    async def download_file(self, file_path: str) -> bytes:
        """Download file bytes; raises `TelegramApiError` over the size cap or on failure."""
        ...


class TelegramBotClient:
    """Bot API client: token in the method path, JSON payloads, long-poll timeouts."""

    def __init__(self, http_client: httpx.AsyncClient, token: str) -> None:
        self._http = http_client
        self._base_url = f"{API_BASE_URL}/bot{token}"
        self._file_base_url = f"{API_BASE_URL}/file/bot{token}"

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        payload: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call(
            "getUpdates",
            payload,
            timeout=timeout_seconds + LONG_POLL_TIMEOUT_MARGIN_SECONDS,
        )
        if not isinstance(result, list):
            raise TelegramApiError("getUpdates: unexpected result shape")
        return _parse_updates(result)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            # allow_sending_without_reply: a deleted question must not fail the answer
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = await self._call_with_parse_fallback("sendMessage", payload)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramApiError("sendMessage: unexpected result shape")
        return int(result["message_id"])

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        try:
            await self._call_with_parse_fallback("editMessageText", payload)
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        # fire-and-forget UX hint (a typing indicator): worth failing fast on
        # rather than queuing behind a rate-limit retry.
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, retry=False)

    async def get_file(self, file_id: str) -> str:
        result = await self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or "file_path" not in result:
            raise TelegramApiError("getFile: unexpected result shape")
        return str(result["file_path"])

    async def download_file(self, file_path: str) -> bytes:
        """GET the file's bytes; not a JSON call, so it gets its own small retry loop."""
        url = f"{self._file_base_url}/{file_path}"
        total_wait = 0.0
        for attempt in range(1, MAX_CALL_ATTEMPTS + 1):
            try:
                return await self._download_once(url)
            except _RetryableCallError as exc:
                exhausted = attempt == MAX_CALL_ATTEMPTS
                over_cap = total_wait + exc.wait_seconds > MAX_TOTAL_RETRY_WAIT_SECONDS
                if exhausted or over_cap:
                    raise TelegramApiError(str(exc)) from None
                logger.warning(
                    "downloadFile: %s; retrying in %.1fs (attempt %d/%d)",
                    exc,
                    exc.wait_seconds,
                    attempt,
                    MAX_CALL_ATTEMPTS,
                )
                await asyncio.sleep(exc.wait_seconds)
                total_wait += exc.wait_seconds
        raise AssertionError("unreachable: the loop above always returns or raises")

    async def _download_once(self, url: str) -> bytes:
        """Make one download attempt; raises `_RetryableCallError` for a transient failure.

        Streamed and stopped at the cap rather than buffered and measured: the
        declared size is the sender's word, and a file that ignores the cap must
        not be held in memory before being refused.
        """
        try:
            async with self._http.stream(
                "GET", url, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS
            ) as response:
                if response.status_code in RETRYABLE_HTTP_STATUS_CODES:
                    raise _RetryableCallError(
                        TRANSIENT_RETRY_DELAY_SECONDS,
                        f"downloadFile: HTTP {response.status_code}",
                    )
                if response.status_code != httpx.codes.OK:
                    raise TelegramApiError(f"downloadFile: HTTP {response.status_code}")
                return await _read_capped(response, MAX_DOWNLOADED_FILE_BYTES)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _RetryableCallError(
                TRANSIENT_RETRY_DELAY_SECONDS, f"downloadFile: {exc}"
            ) from exc

    async def send_rich_message(
        self, chat_id: int, markdown: str, reply_to_message_id: int | None = None
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if reply_to_message_id is not None:
            # allow_sending_without_reply: a deleted question must not fail the answer
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = await self._call("sendRichMessage", payload)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramApiError("sendRichMessage: unexpected result shape")
        return int(result["message_id"])

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown},
        }
        try:
            await self._call("editMessageText", payload)
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def _call_with_parse_fallback(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        """Call `method`, retrying without parse_mode when the API rejects the markup."""
        try:
            return await self._call(method, payload)
        except TelegramApiError as exc:
            if "parse_mode" not in payload or CANT_PARSE_ENTITIES_MARKER not in str(exc):
                raise
            logger.warning("%s rejected the markup; retrying as plain text", method)
            plain = {key: value for key, value in payload.items() if key != "parse_mode"}
            return await self._call(method, plain)

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        *,
        retry: bool = True,
    ) -> dict[str, Any] | list[Any]:
        """POST `method`, retrying a bounded number of times on 429/transient failures.

        A 429 honors the Bot API's `parameters.retry_after`; transient
        network errors and 5xx responses use a fixed short backoff instead
        (Telegram gives no `retry_after` for those). Total sleep across all
        retries is capped at `MAX_TOTAL_RETRY_WAIT_SECONDS` — a `retry_after`
        above that cap is not worth waiting for and fails immediately, same
        as exhausting the attempt budget. `retry=False` skips all of this
        for fire-and-forget calls that should fail fast instead of queuing
        behind a rate limit.
        """
        total_wait = 0.0
        attempts = MAX_CALL_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._call_once(method, payload, timeout)
            except _RetryableCallError as exc:
                exhausted = attempt == attempts
                over_cap = total_wait + exc.wait_seconds > MAX_TOTAL_RETRY_WAIT_SECONDS
                if exhausted or over_cap:
                    raise TelegramApiError(str(exc)) from None
                logger.warning(
                    "%s: %s; retrying in %.1fs (attempt %d/%d)",
                    method,
                    exc,
                    exc.wait_seconds,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(exc.wait_seconds)
                total_wait += exc.wait_seconds
        raise AssertionError("unreachable: the loop above always returns or raises")

    async def _call_once(
        self, method: str, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any] | list[Any]:
        """Make one HTTP attempt; raises `_RetryableCallError` for a worth-retrying failure."""
        try:
            response = await self._http.post(
                f"{self._base_url}/{method}", json=payload, timeout=timeout
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _RetryableCallError(TRANSIENT_RETRY_DELAY_SECONDS, f"{method}: {exc}") from exc
        body = _parse_body(response)
        if body is None:
            _raise_for_non_json_response(method, response)
        return _result_or_raise(method, response, body)


def _raise_for_non_json_response(method: str, response: httpx.Response) -> NoReturn:
    if response.status_code in RETRYABLE_HTTP_STATUS_CODES:
        raise _RetryableCallError(
            TRANSIENT_RETRY_DELAY_SECONDS, f"{method}: HTTP {response.status_code}"
        )
    try:
        # The request URL carries the bot token; keep it out of logs.
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TelegramApiError(f"{method}: HTTP {exc.response.status_code}") from None
    raise TelegramApiError(f"{method}: unexpected response shape")


def _result_or_raise(
    method: str, response: httpx.Response, body: dict[str, Any]
) -> dict[str, Any] | list[Any]:
    # Error statuses still carry a JSON body with the failure description.
    if not body.get("ok"):
        description = body.get("description", "unknown error")
        retry_after = _retry_after_seconds(body)
        is_too_many_requests = (
            response.status_code == TOO_MANY_REQUESTS_STATUS
            or body.get("error_code") == TOO_MANY_REQUESTS_STATUS
        )
        if is_too_many_requests and retry_after is not None:
            raise _RetryableCallError(retry_after, f"{method}: {description}")
        raise TelegramApiError(f"{method}: {description}")
    result = body.get("result")
    return result if isinstance(result, (dict, list)) else {}


def _retry_after_seconds(body: dict[str, Any]) -> float | None:
    """Extract `parameters.retry_after` from an error body, if present."""
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    return float(retry_after) if isinstance(retry_after, int | float) else None


def _parse_body(response: httpx.Response) -> dict[str, Any] | None:
    """Parse the response JSON object, or None when the body is not one."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _parse_updates(items: list[Any]) -> list[TelegramUpdate]:
    """Parse raw update dicts, skipping entries that do not match the schema.

    A poison entry is reduced to its bare update id (message=None) so the
    poller still advances the offset past it instead of re-fetching forever.
    """
    updates: list[TelegramUpdate] = []
    for item in items:
        try:
            updates.append(TelegramUpdate.model_validate(item))
            continue
        except ValidationError:
            pass
        if isinstance(item, dict) and isinstance(item.get("update_id"), int):
            logger.warning("Dropping an unparsable Telegram update %s", item["update_id"])
            updates.append(TelegramUpdate(update_id=item["update_id"]))
        else:
            logger.warning("Dropping a Telegram update without an update id")
    return updates
