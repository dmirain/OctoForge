"""Events produced by streaming LLM completions."""

from dataclasses import dataclass

from octoforge_core.domain import ChatMessage


@dataclass(frozen=True, slots=True)
class TextDelta:
    """An incremental piece of the assistant message."""

    text: str


@dataclass(frozen=True, slots=True)
class StreamFinished:
    """Terminal event carrying the complete assistant message."""

    message: ChatMessage


StreamEvent = TextDelta | StreamFinished
