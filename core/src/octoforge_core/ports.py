"""Ports (protocols) used by the core services."""

from collections.abc import AsyncIterator
from typing import Protocol

from octoforge_core.domain import ChatMessage
from octoforge_core.llm.events import StreamEvent
from octoforge_core.llm.usage import Completion
from octoforge_core.tools.base import ToolSpec


class LLMClient(Protocol):
    """Async chat-completion client port."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        """Return the assistant reply and its usage for the given conversation."""
        ...

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the assistant reply as events."""
        ...
