"""Configuration objects for the core library."""

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 1.0
DEFAULT_RETRY_MAX_SECONDS = 30.0


class EmbeddingBackend(StrEnum):
    """Where embeddings are computed: remote HTTP endpoint or in-process."""

    OPENAI = "openai"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Connection settings for an OpenAI-compatible LLM endpoint."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Settings for embeddings: an OpenAI-compatible endpoint or a local model.

    `base_url`/`api_key` apply to the OPENAI backend only; `batch_size`
    applies to the LOCAL backend (sentence-transformers encode batches).
    """

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    backend: EmbeddingBackend = EmbeddingBackend.OPENAI
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
