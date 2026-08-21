"""Bounded retrying HTTP transport for Telegram Bot API methods and files."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from octoforge_telegram.bot_response import (
    RETRYABLE_HTTP_STATUS_CODES,
    TRANSIENT_RETRY_DELAY_SECONDS,
    RetryableCallError,
    parse_body,
    raise_for_non_json_response,
    result_or_raise,
)
from octoforge_telegram.client_types import TelegramApiError

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_CALL_ATTEMPTS = 3
MAX_TOTAL_RETRY_WAIT_SECONDS = 10.0
MAX_DOWNLOADED_FILE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BotCall:
    method: str
    payload: dict[str, Any]
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    retry: bool = True


class BotTransport:
    def __init__(self, http: httpx.AsyncClient, base_url: str, file_base_url: str) -> None:
        self._http = http
        self._base_url = base_url
        self._file_base_url = file_base_url

    async def call(self, request: BotCall) -> dict[str, Any] | list[Any]:
        total_wait = 0.0
        attempts = MAX_CALL_ATTEMPTS if request.retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._call_once(request)
            except RetryableCallError as exc:
                exhausted = attempt == attempts
                over_cap = total_wait + exc.wait_seconds > MAX_TOTAL_RETRY_WAIT_SECONDS
                if exhausted or over_cap:
                    raise TelegramApiError(str(exc)) from None
                await asyncio.sleep(exc.wait_seconds)
                total_wait += exc.wait_seconds
        raise AssertionError("unreachable: bounded call loop returns or raises")

    async def download(self, file_path: str) -> bytes:
        url = f"{self._file_base_url}/{file_path}"
        total_wait = 0.0
        for attempt in range(1, MAX_CALL_ATTEMPTS + 1):
            try:
                return await self._download_once(url)
            except RetryableCallError as exc:
                exhausted = attempt == MAX_CALL_ATTEMPTS
                over_cap = total_wait + exc.wait_seconds > MAX_TOTAL_RETRY_WAIT_SECONDS
                if exhausted or over_cap:
                    raise TelegramApiError(str(exc)) from None
                await asyncio.sleep(exc.wait_seconds)
                total_wait += exc.wait_seconds
        raise AssertionError("unreachable: bounded download loop returns or raises")

    async def _call_once(self, request: BotCall) -> dict[str, Any] | list[Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/{request.method}",
                json=request.payload,
                timeout=request.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableCallError(
                TRANSIENT_RETRY_DELAY_SECONDS,
                f"{request.method}: {exc}",
            ) from exc
        body = parse_body(response)
        if body is None:
            raise_for_non_json_response(request.method, response)
        return result_or_raise(request.method, response, body)

    async def _download_once(self, url: str) -> bytes:
        try:
            async with self._http.stream(
                "GET",
                url,
                timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code in RETRYABLE_HTTP_STATUS_CODES:
                    raise RetryableCallError(
                        TRANSIENT_RETRY_DELAY_SECONDS,
                        f"downloadFile: HTTP {response.status_code}",
                    )
                if response.status_code != httpx.codes.OK:
                    raise TelegramApiError(f"downloadFile: HTTP {response.status_code}")
                return await _read_capped(response, MAX_DOWNLOADED_FILE_BYTES)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableCallError(TRANSIENT_RETRY_DELAY_SECONDS, f"downloadFile: {exc}") from exc


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TelegramApiError("downloadFile: file too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise TelegramApiError("downloadFile: file too large")
        chunks.append(chunk)
    return b"".join(chunks)
