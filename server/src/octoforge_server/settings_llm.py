"""LLM, embeddings and reranking settings and core config conversion."""

from octoforge_core import EmbeddingConfig, LLMConfig
from octoforge_core.config import DEFAULT_EMBEDDING_BATCH_SIZE, EmbeddingBackend
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES
from pydantic import BaseModel

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BASE_SECONDS = 1.0
DEFAULT_LLM_RETRY_MAX_SECONDS = 30.0
DEFAULT_RERANKER_MODEL = ""
DEFAULT_RERANKER_API_URL = "https://api.siliconflow.cn/v1/rerank"
DEFAULT_RERANKER_TIMEOUT_SECONDS = 30.0


class LlmSettings(BaseModel):
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    llm_max_retries: int = DEFAULT_LLM_MAX_RETRIES
    llm_retry_base_seconds: float = DEFAULT_LLM_RETRY_BASE_SECONDS
    llm_retry_max_seconds: float = DEFAULT_LLM_RETRY_MAX_SECONDS
    llm_stream_idle_timeout_seconds: float = 120.0

    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    embedding_api_key: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_backend: EmbeddingBackend = EmbeddingBackend.OPENAI
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE

    reranker_model: str = DEFAULT_RERANKER_MODEL
    reranker_candidates: int = DEFAULT_RERANK_CANDIDATES
    reranker_api_key: str = ""
    reranker_api_url: str = DEFAULT_RERANKER_API_URL
    reranker_timeout_seconds: float = DEFAULT_RERANKER_TIMEOUT_SECONDS

    def to_llm_config(self) -> LLMConfig:
        return LLMConfig(
            api_key=self.llm_api_key,
            model=self.llm_model,
            max_retries=self.llm_max_retries,
            retry_base_seconds=self.llm_retry_base_seconds,
            retry_max_seconds=self.llm_retry_max_seconds,
        )

    def embeddings_inherit_llm(self) -> bool:
        return (
            self.embedding_backend == EmbeddingBackend.OPENAI
            and not self.embedding_api_key
            and self.embedding_base_url == DEFAULT_EMBEDDING_BASE_URL
            and bool(self.llm_api_key)
        )

    def resolved_embedding_base_url(self) -> str:
        return self.llm_base_url if self.embeddings_inherit_llm() else self.embedding_base_url

    def to_embedding_config(self) -> EmbeddingConfig:
        return EmbeddingConfig(
            api_key=self.llm_api_key if self.embeddings_inherit_llm() else self.embedding_api_key,
            model=self.embedding_model,
            backend=self.embedding_backend,
            batch_size=self.embedding_batch_size,
        )

    def embeddings_configured(self) -> bool:
        return (
            self.embedding_backend == EmbeddingBackend.LOCAL
            or bool(self.embedding_api_key)
            or self.embeddings_inherit_llm()
        )
