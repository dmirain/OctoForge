"""Retrying decorator over the LLMClient port, plus a short-retry helper.

Retries only transient error kinds (rate limit, provider-internal,
transport) with exponential backoff and full jitter; a provider-supplied
`Retry-After` acts as the floor of the delay. Streaming calls are retried
only when the failure happened before the first stream event — once deltas
went downstream, a retry would duplicate partial output.

`retry_transient` is the lighter variant for the one-shot HTTP backends
(embeddings, reranker): a single extra attempt with a minimal fixed delay.
"""

import logging
from collections.abc import AsyncIterator

from octoforge_core.domain import ChatMessage
from octoforge_core.llm.error_types import LLMError
from octoforge_core.llm.events import RetryScheduled, StreamEvent
from octoforge_core.llm.retry_policy import RetryPolicy
from octoforge_core.llm.short_retry import (
    RETRY_AFTER_DELAY_CAP_SECONDS,
    SHORT_RETRY_DELAY_SECONDS,
    SHORT_RETRY_MAX_RETRIES,
    ShortRetryPolicy,
    Sleeper,
    retry_transient,
)
from octoforge_core.llm.usage import Completion
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolSpec

logger = logging.getLogger(__name__)

__all__ = [
    "RETRY_AFTER_DELAY_CAP_SECONDS",
    "SHORT_RETRY_DELAY_SECONDS",
    "SHORT_RETRY_MAX_RETRIES",
    "RetryPolicy",
    "RetryingLLMClient",
    "ShortRetryPolicy",
    "Sleeper",
    "retry_transient",
]


class RetryingLLMClient:
    """LLMClient wrapper that retries transient failures with backoff."""

    def __init__(
        self,
        inner: LLMClient,
        policy: RetryPolicy,
    ) -> None:
        self._inner = inner
        self._policy = policy

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        """Call complete(), retrying transient failures silently (log only)."""
        attempt = 0
        while True:
            try:
                return await self._inner.complete(messages, tools)
            except LLMError as exc:
                if not exc.transient or attempt >= self._policy.max_retries:
                    raise
                attempt += 1
                delay = self._policy.delay(attempt, exc)
                logger.warning(
                    "LLM complete() failed (%s), retry %d/%d in %.1fs",
                    exc.kind.value,
                    attempt,
                    self._policy.max_retries,
                    delay,
                )
                await self._policy.sleeper(delay)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
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
                if emitted or not exc.transient or attempt >= self._policy.max_retries:
                    raise
                attempt += 1
                delay = self._policy.delay(attempt, exc)
                logger.warning(
                    "LLM stream() failed (%s), retry %d/%d in %.1fs",
                    exc.kind.value,
                    attempt,
                    self._policy.max_retries,
                    delay,
                )
                yield RetryScheduled(attempt=attempt, delay_seconds=delay, reason=exc.kind.value)
                await self._policy.sleeper(delay)
