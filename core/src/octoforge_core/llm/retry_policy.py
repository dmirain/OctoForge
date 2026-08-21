"""Exponential full-jitter retry policy for primary LLM calls."""

import asyncio
import random
from dataclasses import dataclass

from octoforge_core.llm.error_types import LLMError
from octoforge_core.llm.short_retry import RETRY_AFTER_DELAY_CAP_SECONDS, Sleeper


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int
    base_seconds: float
    max_seconds: float
    sleeper: Sleeper = asyncio.sleep

    def delay(self, attempt: int, error: LLMError) -> float:
        """Full jitter, with Retry-After as a floor and a hard total cap."""
        ceiling = min(self.max_seconds, self.base_seconds * (2 ** (attempt - 1)))
        if error.retry_after is not None:
            delay = error.retry_after + random.uniform(0, ceiling)
            return min(delay, RETRY_AFTER_DELAY_CAP_SECONDS)
        return random.uniform(0, ceiling)
