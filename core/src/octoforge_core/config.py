"""Configuration objects for the core library."""

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_EMBEDDING_BATCH_SIZE = 16
# vision calls are slower than text ones (a measured 4-8s for the cheap
# model, 15-30s for the strong one), so they get their own budget
VISION_TIMEOUT_SECONDS = 90.0
# a photographed page of text (a menu, a document) is the common case, and
# 900 tokens truncated it mid-sentence — the ingestion description has to
# fit a full quote of what is written on the picture
VISION_MAX_TOKENS = 1600
# the strong tier is asked precisely when the cheap description fell short,
# usually about that same wall of text, and it may be shown several pages
VISION_DEEP_MAX_TOKENS = 2400
# transcription is an upload plus a decode: a long voice note needs room
SPEECH_TIMEOUT_SECONDS = 120.0
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
class VisionConfig:
    """Settings of the model that looks at images (separate from the main LLM).

    The main model is text-only, so vision is its own endpoint/model pair;
    base URL and key default to the main LLM's in the composition root. An
    empty `model` means the feature is off and images keep their placeholder.
    """

    api_key: str
    model: str
    timeout_seconds: float = VISION_TIMEOUT_SECONDS
    max_tokens: int = VISION_MAX_TOKENS


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    """Settings of the model that transcribes voice messages.

    Its own endpoint/model pair, and — unlike vision — the base URL cannot
    fall back to the main LLM's: transcription is a different endpoint kind,
    and a chat-only gateway answers 404 for it (measured against this
    deployment's provider). An empty `model` means the feature is off.
    """

    api_key: str
    model: str
    timeout_seconds: float = SPEECH_TIMEOUT_SECONDS
    # a hint for the recognizer; empty leaves autodetection on
    language: str = ""


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
