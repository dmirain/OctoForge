"""One short retry for secondary LLM HTTP backends."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from octoforge_core.llm.error_types import LLMError

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]
T = TypeVar("T")

RETRY_AFTER_DELAY_CAP_SECONDS = 300.0
SHORT_RETRY_MAX_RETRIES = 1
SHORT_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class ShortRetryPolicy:
    max_retries: int = SHORT_RETRY_MAX_RETRIES
    delay_seconds: float = SHORT_RETRY_DELAY_SECONDS
    sleeper: Sleeper = asyncio.sleep


DEFAULT_SHORT_RETRY_POLICY = ShortRetryPolicy()


async def retry_transient(
    call: Callable[[], Awaitable[T]],
    policy: ShortRetryPolicy = DEFAULT_SHORT_RETRY_POLICY,
) -> T:
    """Retry transient failures with a short fixed delay and provider floor."""
    attempt = 0
    while True:
        try:
            return await call()
        except LLMError as exc:
            if not exc.transient or attempt >= policy.max_retries:
                raise
            attempt += 1
            delay = min(exc.retry_after or policy.delay_seconds, RETRY_AFTER_DELAY_CAP_SECONDS)
            logger.warning(
                "request failed (%s), retry %d/%d in %.1fs",
                exc.kind.value,
                attempt,
                policy.max_retries,
                delay,
            )
            await policy.sleeper(delay)
