"""Configuration objects for the core library."""

from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Connection settings for an OpenAI-compatible LLM endpoint."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Connection settings for an OpenAI-compatible embeddings endpoint."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
