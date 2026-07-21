"""Retrying decorator over the LLMClient port.

Retries only transient error kinds (rate limit, provider-internal,
transport) with exponential backoff and full jitter; a provider-supplied
`Retry-After` acts as the floor of the delay. Streaming calls are retried
only when the failure happened before the first stream event — once deltas
went downstream, a retry would duplicate partial output.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from octoforge_core.domain import ChatMessage
from octoforge_core.llm.errors import LLMError, RateLimitError
from octoforge_core.llm.events import RetryScheduled, StreamEvent
from octoforge_core.ports import LLMClient
from octoforge_core.skills.base import SkillSpec

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


class RetryingLLMClient:
    """LLMClient wrapper that retries transient failures with backoff."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_retries: int,
        base_seconds: float,
        max_seconds: float,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._base_seconds = base_seconds
        self._max_seconds = max_seconds
        self._sleeper = sleeper

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        """Call complete(), retrying transient failures silently (log only)."""
        attempt = 0
        while True:
            try:
                return await self._inner.complete(messages, tools)
            except LLMError as exc:
                if not exc.transient or attempt >= self._max_retries:
                    raise
                attempt += 1
                delay = self._delay(attempt, exc)
                logger.warning(
                    "LLM complete() failed (%s), retry %d/%d in %.1fs",
                    exc.kind.value,
                    attempt,
                    self._max_retries,
                    delay,
                )
                await self._sleeper(delay)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Call stream(), retrying failures that happened before the first event."""
        attempt = 0
        while True:
            emitted = False
            try:
                async for event in self._inner.stream(messages, tools):
                    emitted = True
                    yield event
                return
            except LLMError as exc:
                if emitted or not exc.transient or attempt >= self._max_retries:
                    raise
                attempt += 1
                delay = self._delay(attempt, exc)
                logger.warning(
                    "LLM stream() failed (%s), retry %d/%d in %.1fs",
                    exc.kind.value,
                    attempt,
                    self._max_retries,
                    delay,
                )
                yield RetryScheduled(attempt=attempt, delay_seconds=delay, reason=exc.kind.value)
                await self._sleeper(delay)

    def _delay(self, attempt: int, exc: LLMError) -> float:
        """Exponential backoff with full jitter, floored at Retry-After."""
        ceiling = min(self._max_seconds, self._base_seconds * (2 ** (attempt - 1)))
        delay = random.uniform(0, ceiling)
        if isinstance(exc, RateLimitError) and exc.retry_after is not None:
            delay = max(delay, exc.retry_after)
        return delay
