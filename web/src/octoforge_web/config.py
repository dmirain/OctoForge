"""Application settings."""

from pathlib import Path

from octoforge_core import EmbeddingConfig, LLMConfig
from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, SYSTEM_PROMPT_NAME
from octoforge_core.config import DEFAULT_EMBEDDING_BATCH_SIZE, EmbeddingBackend
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES
from octoforge_core.net.external import ExternalCallAuth
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "OF_"
ENV_FILE = ".env"
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_INSTRUCTIONS_TOP_K = 5
DEFAULT_AGENT_MAX_ITERATIONS = 10
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./octoforge.db"
DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT = 50
DEFAULT_DATASETS_QUERY_MAX_LIMIT = 200
DEFAULT_MEMORY_SEARCH_DEFAULT_LIMIT = 10
DEFAULT_MEMORY_SEARCH_MAX_LIMIT = 50
DEFAULT_HISTORY_SEARCH_DEFAULT_LIMIT = 20
DEFAULT_HISTORY_SEARCH_MAX_LIMIT = 100
DEFAULT_CONTEXT_HOT_MAX_CHARS = 12000
DEFAULT_CONTEXT_COMPACT_TARGET_CHARS = 6000
DEFAULT_MAX_PROCESSES = 5
DEFAULT_ROUTER_TIMEOUT_SECONDS = 10.0
DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS = 120.0
DEFAULT_SELF_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CRON_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_CRON_LEASE_TTL_SECONDS = 60.0
DEFAULT_CRON_REPLAY_LIMIT = 5
DEFAULT_CRON_RETRY_LIMIT = 3
DEFAULT_CRON_RETRY_BACKOFF_SECONDS = 60.0
DEFAULT_RERANKER_MODEL = ""
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 30.0
DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS = 1.5
FILE_SCHEME_PREFIX = "file:"


class ExternalCallAuthSettings(BaseModel):
    """One whitelist entry for internal authorization on external calls."""

    base_url_prefix: str
    header_name: str
    header_value: str


class Settings(BaseSettings):
    """Environment-driven application settings."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_file=ENV_FILE, extra="ignore")

    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    embedding_api_key: str = ""
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_backend: EmbeddingBackend = EmbeddingBackend.OPENAI
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    reranker_model: str = DEFAULT_RERANKER_MODEL
    reranker_candidates: int = DEFAULT_RERANK_CANDIDATES
    instructions_top_k: int = DEFAULT_INSTRUCTIONS_TOP_K
    external_call_auth_whitelist: list[ExternalCallAuthSettings] = Field(default_factory=list)
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS
    database_url: str = DEFAULT_DATABASE_URL
    datasets_query_default_limit: int = DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT
    datasets_query_max_limit: int = DEFAULT_DATASETS_QUERY_MAX_LIMIT
    memory_search_default_limit: int = DEFAULT_MEMORY_SEARCH_DEFAULT_LIMIT
    memory_search_max_limit: int = DEFAULT_MEMORY_SEARCH_MAX_LIMIT
    history_search_default_limit: int = DEFAULT_HISTORY_SEARCH_DEFAULT_LIMIT
    history_search_max_limit: int = DEFAULT_HISTORY_SEARCH_MAX_LIMIT
    context_hot_max_chars: int = DEFAULT_CONTEXT_HOT_MAX_CHARS
    context_compact_target_chars: int = DEFAULT_CONTEXT_COMPACT_TARGET_CHARS
    max_processes: int = DEFAULT_MAX_PROCESSES
    router_timeout_seconds: float = DEFAULT_ROUTER_TIMEOUT_SECONDS
    llm_stream_idle_timeout_seconds: float = DEFAULT_LLM_STREAM_IDLE_TIMEOUT_SECONDS
    self_base_url: str = DEFAULT_SELF_BASE_URL
    cron_poll_interval_seconds: float = DEFAULT_CRON_POLL_INTERVAL_SECONDS
    cron_lease_ttl_seconds: float = DEFAULT_CRON_LEASE_TTL_SECONDS
    cron_replay_limit: int = DEFAULT_CRON_REPLAY_LIMIT
    cron_retry_limit: int = DEFAULT_CRON_RETRY_LIMIT
    cron_retry_backoff_seconds: float = DEFAULT_CRON_RETRY_BACKOFF_SECONDS
    telegram_bot_token: str = ""
    telegram_poll_timeout_seconds: float = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    telegram_edit_throttle_seconds: float = DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS
    serper_token: str = ""
    system_prompt_source: str = ""
    router_prompt_source: str = ""

    def to_llm_config(self) -> LLMConfig:
        """Build the core LLM configuration."""
        return LLMConfig(
            base_url=self.llm_base_url,
            api_key=self.llm_api_key,
            model=self.llm_model,
        )

    def to_embedding_config(self) -> EmbeddingConfig:
        """Build the core embeddings configuration."""
        return EmbeddingConfig(
            base_url=self.embedding_base_url,
            api_key=self.embedding_api_key,
            model=self.embedding_model,
            backend=self.embedding_backend,
            batch_size=self.embedding_batch_size,
        )

    def embeddings_configured(self) -> bool:
        """Whether an embeddings backend is usable (drives seeding at startup).

        The LOCAL backend needs no credentials; the OPENAI backend is usable
        only with an API key.
        """
        return self.embedding_backend == EmbeddingBackend.LOCAL or bool(self.embedding_api_key)

    def to_external_call_auth_whitelist(self) -> tuple[ExternalCallAuth, ...]:
        """Build the executor's auth whitelist from the settings entries."""
        return tuple(
            ExternalCallAuth(
                base_url_prefix=entry.base_url_prefix,
                header_name=entry.header_name,
                header_value=entry.header_value,
            )
            for entry in self.external_call_auth_whitelist
        )

    def to_prompt_files(self) -> dict[str, Path]:
        """Map prompt names to their override files (`file:` scheme, empty = default).

        Raises ValueError on an unsupported scheme: a mistyped source must
        fail at startup, not silently serve the built-in prompt.
        """
        files: dict[str, Path] = {}
        sources = (
            (SYSTEM_PROMPT_NAME, self.system_prompt_source),
            (ROUTER_PROMPT_NAME, self.router_prompt_source),
        )
        for name, source in sources:
            if source:
                files[name] = _parse_prompt_source(source)
        return files


def _parse_prompt_source(source: str) -> Path:
    if not source.startswith(FILE_SCHEME_PREFIX):
        raise ValueError(
            f"unsupported prompt source {source!r}: only '{FILE_SCHEME_PREFIX}' is supported"
        )
    return Path(source.removeprefix(FILE_SCHEME_PREFIX))
