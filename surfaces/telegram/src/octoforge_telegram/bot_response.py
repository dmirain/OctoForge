"""Parse Telegram Bot API responses and classify retryable failures."""

from typing import Any, NoReturn

import httpx
from pydantic import ValidationError

from octoforge_telegram.client_types import TelegramApiError
from octoforge_telegram.models import TelegramUpdate

TOO_MANY_REQUESTS_STATUS = 429
RETRYABLE_HTTP_STATUS_CODES = frozenset({500, 502, 503, 504})
TRANSIENT_RETRY_DELAY_SECONDS = 1.0


class RetryableCallError(Exception):
    def __init__(self, wait_seconds: float, message: str) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


def result_or_raise(
    method: str,
    response: httpx.Response,
    body: dict[str, Any],
) -> dict[str, Any] | list[Any]:
    if not body.get("ok"):
        description = body.get("description", "unknown error")
        retry_after = retry_after_seconds(body)
        rate_limited = (
            response.status_code == TOO_MANY_REQUESTS_STATUS
            or body.get("error_code") == TOO_MANY_REQUESTS_STATUS
        )
        if rate_limited and retry_after is not None:
            raise RetryableCallError(retry_after, f"{method}: {description}")
        raise TelegramApiError(f"{method}: {description}")
    result = body.get("result")
    return result if isinstance(result, (dict, list)) else {}


def raise_for_non_json_response(method: str, response: httpx.Response) -> NoReturn:
    if response.status_code in RETRYABLE_HTTP_STATUS_CODES:
        raise RetryableCallError(
            TRANSIENT_RETRY_DELAY_SECONDS,
            f"{method}: HTTP {response.status_code}",
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TelegramApiError(f"{method}: HTTP {exc.response.status_code}") from None
    raise TelegramApiError(f"{method}: unexpected response shape")


def parse_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def parse_updates(items: list[Any]) -> list[TelegramUpdate]:
    updates: list[TelegramUpdate] = []
    for item in items:
        try:
            updates.append(TelegramUpdate.model_validate(item))
            continue
        except ValidationError:
            pass
        if isinstance(item, dict) and isinstance(item.get("update_id"), int):
            updates.append(TelegramUpdate(update_id=item["update_id"]))
    return updates


def retry_after_seconds(body: dict[str, Any]) -> float | None:
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    return float(retry_after) if isinstance(retry_after, int | float) else None
