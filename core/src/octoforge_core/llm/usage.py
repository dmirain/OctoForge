"""Token-usage DTOs captured from LLM responses."""

from dataclasses import dataclass

from octoforge_core.domain import ChatMessage


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting of one LLM call, as reported by the provider."""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """Result of a non-streaming completion: the reply plus its usage."""

    message: ChatMessage
    usage: Usage | None = None


def parse_usage(raw: object) -> Usage | None:
    """Parse an OpenAI-style usage object; None when absent or malformed."""
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached if isinstance(cached, int) else None,
    )
