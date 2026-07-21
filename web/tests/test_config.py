"""Tests for the web settings (env parsing and core config conversion)."""

from pathlib import Path

import pytest
from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, SYSTEM_PROMPT_NAME
from octoforge_core.config import EmbeddingBackend

from octoforge_web.config import (
    DEFAULT_CRON_LEASE_TTL_SECONDS,
    DEFAULT_CRON_POLL_INTERVAL_SECONDS,
    DEFAULT_CRON_REPLAY_LIMIT,
    DEFAULT_CRON_RETRY_BACKOFF_SECONDS,
    DEFAULT_CRON_RETRY_LIMIT,
    DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT,
    DEFAULT_DATASETS_QUERY_MAX_LIMIT,
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_INSTRUCTIONS_TOP_K,
    DEFAULT_MAX_PROCESSES,
    DEFAULT_MEMORY_SEARCH_DEFAULT_LIMIT,
    DEFAULT_MEMORY_SEARCH_MAX_LIMIT,
    DEFAULT_ROUTER_TIMEOUT_SECONDS,
    DEFAULT_SELF_BASE_URL,
    DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS,
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    Settings,
)

CUSTOM_TOP_K = 7
CUSTOM_DATASETS_DEFAULT_LIMIT = 25
CUSTOM_DATASETS_MAX_LIMIT = 500
CUSTOM_MEMORY_DEFAULT_LIMIT = 15
CUSTOM_MEMORY_MAX_LIMIT = 80
CUSTOM_MAX_PROCESSES = 9
CUSTOM_ROUTER_TIMEOUT = 2.5
CUSTOM_SELF_BASE_URL = "http://10.0.0.5:9000"
CUSTOM_CRON_POLL_INTERVAL = 2.5
CUSTOM_CRON_LEASE_TTL = 120.0
CUSTOM_CRON_REPLAY_LIMIT = 3
CUSTOM_CRON_RETRY_LIMIT = 5
CUSTOM_CRON_RETRY_BACKOFF = 30.0
CUSTOM_BATCH_SIZE = 32
CUSTOM_RERANK_CANDIDATES = 15
CUSTOM_TELEGRAM_TOKEN = "123:abc"
CUSTOM_TELEGRAM_POLL_TIMEOUT = 45.0
CUSTOM_TELEGRAM_EDIT_THROTTLE = 2.0
LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"
LOCAL_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
WHITELIST_JSON = (
    '[{"base_url_prefix": "https://internal.example.com/", '
    '"header_name": "X-Api-Key", "header_value": "s3cret"}]'
)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "OF_EMBEDDING_BASE_URL",
        "OF_EMBEDDING_API_KEY",
        "OF_EMBEDDING_MODEL",
        "OF_EMBEDDING_BACKEND",
        "OF_EMBEDDING_BATCH_SIZE",
        "OF_RERANKER_MODEL",
        "OF_RERANKER_CANDIDATES",
        "OF_INSTRUCTIONS_TOP_K",
        "OF_EXTERNAL_CALL_AUTH_WHITELIST",
        "OF_DATASETS_QUERY_DEFAULT_LIMIT",
        "OF_DATASETS_QUERY_MAX_LIMIT",
        "OF_MEMORY_SEARCH_DEFAULT_LIMIT",
        "OF_MEMORY_SEARCH_MAX_LIMIT",
        "OF_MAX_PROCESSES",
        "OF_ROUTER_TIMEOUT_SECONDS",
        "OF_SELF_BASE_URL",
        "OF_CRON_POLL_INTERVAL_SECONDS",
        "OF_CRON_LEASE_TTL_SECONDS",
        "OF_CRON_REPLAY_LIMIT",
        "OF_CRON_RETRY_LIMIT",
        "OF_CRON_RETRY_BACKOFF_SECONDS",
        "OF_TELEGRAM_BOT_TOKEN",
        "OF_TELEGRAM_POLL_TIMEOUT_SECONDS",
        "OF_TELEGRAM_EDIT_THROTTLE_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)  # defaults must not read the developer's .env

    assert settings.embedding_base_url == DEFAULT_EMBEDDING_BASE_URL
    assert settings.embedding_api_key == ""
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert settings.instructions_top_k == DEFAULT_INSTRUCTIONS_TOP_K
    assert settings.external_call_auth_whitelist == []
    assert settings.datasets_query_default_limit == DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT
    assert settings.datasets_query_max_limit == DEFAULT_DATASETS_QUERY_MAX_LIMIT
    assert settings.memory_search_default_limit == DEFAULT_MEMORY_SEARCH_DEFAULT_LIMIT
    assert settings.memory_search_max_limit == DEFAULT_MEMORY_SEARCH_MAX_LIMIT
    assert settings.max_processes == DEFAULT_MAX_PROCESSES
    assert settings.router_timeout_seconds == DEFAULT_ROUTER_TIMEOUT_SECONDS
    assert settings.self_base_url == DEFAULT_SELF_BASE_URL
    assert settings.cron_poll_interval_seconds == DEFAULT_CRON_POLL_INTERVAL_SECONDS
    assert settings.cron_lease_ttl_seconds == DEFAULT_CRON_LEASE_TTL_SECONDS
    assert settings.cron_replay_limit == DEFAULT_CRON_REPLAY_LIMIT
    assert settings.cron_retry_limit == DEFAULT_CRON_RETRY_LIMIT
    assert settings.cron_retry_backoff_seconds == DEFAULT_CRON_RETRY_BACKOFF_SECONDS
    assert settings.telegram_bot_token == ""
    assert settings.telegram_poll_timeout_seconds == DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
    assert settings.telegram_edit_throttle_seconds == DEFAULT_TELEGRAM_EDIT_THROTTLE_SECONDS
    assert settings.to_embedding_config().model == DEFAULT_EMBEDDING_MODEL
    assert settings.to_embedding_config().backend == EmbeddingBackend.OPENAI
    assert settings.reranker_model == ""
    assert not settings.embeddings_configured()
    assert settings.to_external_call_auth_whitelist() == ()


def test_embedding_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "ollama")
    monkeypatch.setenv("OF_EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setenv("OF_INSTRUCTIONS_TOP_K", str(CUSTOM_TOP_K))

    settings = Settings()
    config = settings.to_embedding_config()

    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key == "ollama"
    assert config.model == "nomic-embed-text"
    assert settings.instructions_top_k == CUSTOM_TOP_K


def test_datasets_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_DATASETS_QUERY_DEFAULT_LIMIT", str(CUSTOM_DATASETS_DEFAULT_LIMIT))
    monkeypatch.setenv("OF_DATASETS_QUERY_MAX_LIMIT", str(CUSTOM_DATASETS_MAX_LIMIT))

    settings = Settings()

    assert settings.datasets_query_default_limit == CUSTOM_DATASETS_DEFAULT_LIMIT
    assert settings.datasets_query_max_limit == CUSTOM_DATASETS_MAX_LIMIT


def test_memory_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_MEMORY_SEARCH_DEFAULT_LIMIT", str(CUSTOM_MEMORY_DEFAULT_LIMIT))
    monkeypatch.setenv("OF_MEMORY_SEARCH_MAX_LIMIT", str(CUSTOM_MEMORY_MAX_LIMIT))

    settings = Settings()

    assert settings.memory_search_default_limit == CUSTOM_MEMORY_DEFAULT_LIMIT
    assert settings.memory_search_max_limit == CUSTOM_MEMORY_MAX_LIMIT


def test_process_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_MAX_PROCESSES", str(CUSTOM_MAX_PROCESSES))
    monkeypatch.setenv("OF_ROUTER_TIMEOUT_SECONDS", str(CUSTOM_ROUTER_TIMEOUT))

    settings = Settings()

    assert settings.max_processes == CUSTOM_MAX_PROCESSES
    assert settings.router_timeout_seconds == CUSTOM_ROUTER_TIMEOUT


def test_cron_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_SELF_BASE_URL", CUSTOM_SELF_BASE_URL)
    monkeypatch.setenv("OF_CRON_POLL_INTERVAL_SECONDS", str(CUSTOM_CRON_POLL_INTERVAL))
    monkeypatch.setenv("OF_CRON_LEASE_TTL_SECONDS", str(CUSTOM_CRON_LEASE_TTL))
    monkeypatch.setenv("OF_CRON_REPLAY_LIMIT", str(CUSTOM_CRON_REPLAY_LIMIT))
    monkeypatch.setenv("OF_CRON_RETRY_LIMIT", str(CUSTOM_CRON_RETRY_LIMIT))
    monkeypatch.setenv("OF_CRON_RETRY_BACKOFF_SECONDS", str(CUSTOM_CRON_RETRY_BACKOFF))

    settings = Settings()

    assert settings.self_base_url == CUSTOM_SELF_BASE_URL
    assert settings.cron_poll_interval_seconds == CUSTOM_CRON_POLL_INTERVAL
    assert settings.cron_lease_ttl_seconds == CUSTOM_CRON_LEASE_TTL
    assert settings.cron_replay_limit == CUSTOM_CRON_REPLAY_LIMIT
    assert settings.cron_retry_limit == CUSTOM_CRON_RETRY_LIMIT
    assert settings.cron_retry_backoff_seconds == CUSTOM_CRON_RETRY_BACKOFF


def test_telegram_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", CUSTOM_TELEGRAM_TOKEN)
    monkeypatch.setenv("OF_TELEGRAM_POLL_TIMEOUT_SECONDS", str(CUSTOM_TELEGRAM_POLL_TIMEOUT))
    monkeypatch.setenv("OF_TELEGRAM_EDIT_THROTTLE_SECONDS", str(CUSTOM_TELEGRAM_EDIT_THROTTLE))

    settings = Settings()

    assert settings.telegram_bot_token == CUSTOM_TELEGRAM_TOKEN
    assert settings.telegram_poll_timeout_seconds == CUSTOM_TELEGRAM_POLL_TIMEOUT
    assert settings.telegram_edit_throttle_seconds == CUSTOM_TELEGRAM_EDIT_THROTTLE


def test_auth_whitelist_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EXTERNAL_CALL_AUTH_WHITELIST", WHITELIST_JSON)

    settings = Settings()
    whitelist = settings.to_external_call_auth_whitelist()

    assert len(whitelist) == 1
    entry = whitelist[0]
    assert entry.base_url_prefix == "https://internal.example.com/"
    assert entry.header_name == "X-Api-Key"
    assert entry.header_value == "s3cret"


def test_local_embeddings_backend_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EMBEDDING_BACKEND", "local")
    monkeypatch.setenv("OF_EMBEDDING_MODEL", LOCAL_EMBEDDING_MODEL)
    monkeypatch.setenv("OF_EMBEDDING_BATCH_SIZE", str(CUSTOM_BATCH_SIZE))
    monkeypatch.setenv("OF_RERANKER_MODEL", LOCAL_RERANKER_MODEL)
    monkeypatch.setenv("OF_RERANKER_CANDIDATES", str(CUSTOM_RERANK_CANDIDATES))

    settings = Settings()
    config = settings.to_embedding_config()

    assert config.backend == EmbeddingBackend.LOCAL
    assert config.model == LOCAL_EMBEDDING_MODEL
    assert config.batch_size == CUSTOM_BATCH_SIZE
    assert settings.reranker_model == LOCAL_RERANKER_MODEL
    assert settings.reranker_candidates == CUSTOM_RERANK_CANDIDATES
    assert settings.embeddings_configured()


def test_openai_backend_requires_api_key_to_count_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_EMBEDDING_BACKEND", "openai")
    monkeypatch.delenv("OF_EMBEDDING_API_KEY", raising=False)

    assert not Settings().embeddings_configured()

    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "sk-test")

    assert Settings().embeddings_configured()


def test_prompt_sources_default_to_no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OF_SYSTEM_PROMPT_SOURCE", raising=False)
    monkeypatch.delenv("OF_ROUTER_PROMPT_SOURCE", raising=False)

    assert Settings().to_prompt_files() == {}


def test_prompt_sources_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_SYSTEM_PROMPT_SOURCE", "file:/etc/octoforge/system.txt")
    monkeypatch.setenv("OF_ROUTER_PROMPT_SOURCE", "file:/etc/octoforge/router.txt")

    files = Settings().to_prompt_files()

    assert files == {
        SYSTEM_PROMPT_NAME: Path("/etc/octoforge/system.txt"),
        ROUTER_PROMPT_NAME: Path("/etc/octoforge/router.txt"),
    }


def test_prompt_source_with_unsupported_scheme_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_SYSTEM_PROMPT_SOURCE", "https://example.com/prompt.txt")

    with pytest.raises(ValueError, match="unsupported prompt source"):
        Settings().to_prompt_files()
