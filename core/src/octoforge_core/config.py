"""Configuration objects for the core library."""

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_BATCH_SIZE = 16
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 1.0
DEFAULT_RETRY_MAX_SECONDS = 30.0
DEFAULT_RERANK_MAX_LENGTH = 512
DEFAULT_RERANK_BATCH_SIZE = 32
DEFAULT_RERANK_API_URL = "https://api.siliconflow.cn/v1/rerank"


class EmbeddingBackend(StrEnum):
    """Where embeddings are computed: remote HTTP endpoint or in-process."""

    OPENAI = "openai"
    LOCAL = "local"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Connection settings for an OpenAI-compatible LLM endpoint."""

    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Settings for embeddings: an OpenAI-compatible endpoint or a local model.

    `api_key`/`timeout_seconds` apply to the OPENAI backend only; `batch_size`
    applies to the LOCAL backend (sentence-transformers encode batches).
    """

    api_key: str
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    backend: EmbeddingBackend = EmbeddingBackend.OPENAI
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    """Settings of the local cross-encoder reranker."""

    model: str
    max_length: int = DEFAULT_RERANK_MAX_LENGTH
    batch_size: int = DEFAULT_RERANK_BATCH_SIZE


@dataclass(frozen=True, slots=True)
class HttpRerankerConfig:
    """Connection settings of an HTTP reranker backend."""

    model: str
    api_key: str
    api_url: str = DEFAULT_RERANK_API_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
