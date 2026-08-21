"""Tests for the web settings (env parsing and core config conversion)."""

from pathlib import Path

import httpx
import pytest
from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, SYSTEM_PROMPT_NAME
from octoforge_core.config import EmbeddingBackend
from octoforge_core.llm.http_reranker import HttpRerankerClient
from octoforge_core.llm.reranker import CrossEncoderReranker
from octoforge_server.config import (
    DEFAULT_CRON_LEASE_TTL_SECONDS,
    DEFAULT_CRON_POLL_INTERVAL_SECONDS,
    DEFAULT_CRON_REPLAY_LIMIT,
    DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT,
    DEFAULT_DATASETS_QUERY_MAX_LIMIT,
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_INSTRUCTIONS_TOP_K,
    DEFAULT_MAX_PROCESSES,
    DEFAULT_ROUTER_TIMEOUT_SECONDS,
    DEFAULT_SELF_BASE_URL,
    DEFAULT_VISION_DEEP_MODEL,
    Settings,
)
from octoforge_telegram.config import (
    DEFAULT_EDIT_THROTTLE_SECONDS,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    TelegramSettings,
)

from octoforge_deploy.main import _build_reranker

CUSTOM_TOP_K = 7
CUSTOM_DATASETS_DEFAULT_LIMIT = 25
CUSTOM_DATASETS_MAX_LIMIT = 500
CUSTOM_MAX_PROCESSES = 9
CUSTOM_ROUTER_TIMEOUT = 2.5
CUSTOM_SELF_BASE_URL = "http://10.0.0.5:9000"
CUSTOM_CRON_POLL_INTERVAL = 2.5
CUSTOM_CRON_LEASE_TTL = 120.0
CUSTOM_CRON_REPLAY_LIMIT = 3
CUSTOM_BATCH_SIZE = 32
CUSTOM_RERANK_CANDIDATES = 15
CUSTOM_TELEGRAM_TOKEN = "123:abc"
CUSTOM_TELEGRAM_POLL_TIMEOUT = 45.0
CUSTOM_TELEGRAM_EDIT_THROTTLE = 2.0
CUSTOM_LLM_BASE_URL = "https://gateway.example.com/v1"
CUSTOM_LLM_KEY = "sk-llm-key"
LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"
LOCAL_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
WHITELIST_JSON = (
    '[{"base_url_prefix": "https://internal.example.com/", '
    '"header_name": "X-Api-Key", "header_value": "s3cret"}]'
)
DEFAULT_ENV_VARS = (
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
    "OF_MAX_PROCESSES",
    "OF_ROUTER_TIMEOUT_SECONDS",
    "OF_SELF_BASE_URL",
    "OF_CRON_POLL_INTERVAL_SECONDS",
    "OF_CRON_LEASE_TTL_SECONDS",
    "OF_CRON_REPLAY_LIMIT",
    "OF_TELEGRAM_BOT_TOKEN",
    "OF_TELEGRAM_POLL_TIMEOUT_SECONDS",
    "OF_TELEGRAM_EDIT_THROTTLE_SECONDS",
)


def _clear_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in DEFAULT_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)


def _assert_default_settings(settings: Settings) -> None:
    assert settings.embedding_base_url == DEFAULT_EMBEDDING_BASE_URL
    assert settings.embedding_api_key == ""
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert settings.instructions_top_k == DEFAULT_INSTRUCTIONS_TOP_K
    assert settings.external_call_auth_whitelist == []
    assert settings.datasets_query_default_limit == DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT
    assert settings.datasets_query_max_limit == DEFAULT_DATASETS_QUERY_MAX_LIMIT
    assert settings.max_processes == DEFAULT_MAX_PROCESSES
    assert settings.router_timeout_seconds == DEFAULT_ROUTER_TIMEOUT_SECONDS
    assert settings.self_base_url == DEFAULT_SELF_BASE_URL
    assert settings.cron_poll_interval_seconds == DEFAULT_CRON_POLL_INTERVAL_SECONDS
    assert settings.cron_lease_ttl_seconds == DEFAULT_CRON_LEASE_TTL_SECONDS
    assert settings.cron_replay_limit == DEFAULT_CRON_REPLAY_LIMIT
    assert settings.reranker_model == ""
    assert not settings.embeddings_configured()
    assert settings.to_external_call_auth_whitelist() == ()


def _assert_default_embedding_config(settings: Settings) -> None:
    config = settings.to_embedding_config()

    assert config.model == DEFAULT_EMBEDDING_MODEL
    assert config.backend == EmbeddingBackend.OPENAI


def _assert_default_telegram_settings() -> None:
    telegram = TelegramSettings()

    assert telegram.telegram_bot_token == ""
    assert telegram.telegram_poll_timeout_seconds == DEFAULT_POLL_TIMEOUT_SECONDS
    assert telegram.telegram_edit_throttle_seconds == DEFAULT_EDIT_THROTTLE_SECONDS


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_default_env(monkeypatch)

    settings = Settings(_env_file=None)  # defaults must not read the developer's .env

    _assert_default_settings(settings)
    _assert_default_embedding_config(settings)
    _assert_default_telegram_settings()


def test_embedding_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "ollama")
    monkeypatch.setenv("OF_EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setenv("OF_INSTRUCTIONS_TOP_K", str(CUSTOM_TOP_K))

    settings = Settings()
    config = settings.to_embedding_config()

    assert settings.embedding_base_url == "http://localhost:11434/v1"
    assert config.api_key == "ollama"
    assert config.model == "nomic-embed-text"
    assert settings.instructions_top_k == CUSTOM_TOP_K


def test_datasets_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_DATASETS_QUERY_DEFAULT_LIMIT", str(CUSTOM_DATASETS_DEFAULT_LIMIT))
    monkeypatch.setenv("OF_DATASETS_QUERY_MAX_LIMIT", str(CUSTOM_DATASETS_MAX_LIMIT))

    settings = Settings()

    assert settings.datasets_query_default_limit == CUSTOM_DATASETS_DEFAULT_LIMIT
    assert settings.datasets_query_max_limit == CUSTOM_DATASETS_MAX_LIMIT


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

    settings = Settings()

    assert settings.self_base_url == CUSTOM_SELF_BASE_URL
    assert settings.cron_poll_interval_seconds == CUSTOM_CRON_POLL_INTERVAL
    assert settings.cron_lease_ttl_seconds == CUSTOM_CRON_LEASE_TTL
    assert settings.cron_replay_limit == CUSTOM_CRON_REPLAY_LIMIT


def test_telegram_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", CUSTOM_TELEGRAM_TOKEN)
    monkeypatch.setenv("OF_TELEGRAM_POLL_TIMEOUT_SECONDS", str(CUSTOM_TELEGRAM_POLL_TIMEOUT))
    monkeypatch.setenv("OF_TELEGRAM_EDIT_THROTTLE_SECONDS", str(CUSTOM_TELEGRAM_EDIT_THROTTLE))

    assert TelegramSettings().telegram_bot_token == CUSTOM_TELEGRAM_TOKEN
    assert TelegramSettings().telegram_poll_timeout_seconds == CUSTOM_TELEGRAM_POLL_TIMEOUT
    assert TelegramSettings().telegram_edit_throttle_seconds == CUSTOM_TELEGRAM_EDIT_THROTTLE


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
    # with no LLM key either there is nothing to inherit from
    monkeypatch.delenv("OF_LLM_API_KEY", raising=False)

    assert not Settings().embeddings_configured()

    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "sk-test")

    assert Settings().embeddings_configured()


def test_embeddings_inherit_the_llm_endpoint_when_nothing_else_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_LLM_BASE_URL", CUSTOM_LLM_BASE_URL)
    monkeypatch.setenv("OF_LLM_API_KEY", CUSTOM_LLM_KEY)
    monkeypatch.delenv("OF_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OF_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OF_EMBEDDING_BACKEND", raising=False)

    settings = Settings()

    assert settings.embeddings_inherit_llm()
    assert settings.resolved_embedding_base_url() == CUSTOM_LLM_BASE_URL
    assert settings.to_embedding_config().api_key == CUSTOM_LLM_KEY
    assert settings.embeddings_configured()


def test_own_embedding_key_keeps_the_default_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_BASE_URL", CUSTOM_LLM_BASE_URL)
    monkeypatch.setenv("OF_LLM_API_KEY", CUSTOM_LLM_KEY)
    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "sk-embeddings")
    monkeypatch.delenv("OF_EMBEDDING_BASE_URL", raising=False)

    settings = Settings()

    assert not settings.embeddings_inherit_llm()
    assert settings.resolved_embedding_base_url() == DEFAULT_EMBEDDING_BASE_URL
    assert settings.to_embedding_config().api_key == "sk-embeddings"


def test_own_embedding_base_url_disables_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_BASE_URL", CUSTOM_LLM_BASE_URL)
    monkeypatch.setenv("OF_LLM_API_KEY", CUSTOM_LLM_KEY)
    monkeypatch.setenv("OF_EMBEDDING_BASE_URL", "http://embeddings.internal/v1")
    monkeypatch.delenv("OF_EMBEDDING_API_KEY", raising=False)

    settings = Settings()

    assert not settings.embeddings_inherit_llm()
    assert settings.resolved_embedding_base_url() == "http://embeddings.internal/v1"
    assert not settings.embeddings_configured()


def test_local_backend_never_inherits_the_llm_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_BASE_URL", CUSTOM_LLM_BASE_URL)
    monkeypatch.setenv("OF_LLM_API_KEY", CUSTOM_LLM_KEY)
    monkeypatch.setenv("OF_EMBEDDING_BACKEND", "local")
    monkeypatch.delenv("OF_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OF_EMBEDDING_BASE_URL", raising=False)

    settings = Settings()

    assert not settings.embeddings_inherit_llm()
    assert settings.to_embedding_config().api_key == ""


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


RERANKER_API_URL = "https://rerank.example/v1/rerank"


def test_reranker_api_settings_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_RERANKER_API_KEY", "sf-test-key")
    monkeypatch.setenv("OF_RERANKER_API_URL", RERANKER_API_URL)

    settings = Settings()

    assert settings.reranker_api_key == "sf-test-key"
    assert settings.reranker_api_url == RERANKER_API_URL


def test_build_reranker_selects_http_backend_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_RERANKER_MODEL", LOCAL_RERANKER_MODEL)
    monkeypatch.setenv("OF_RERANKER_API_KEY", "sf-test-key")

    reranker = _build_reranker(Settings(), httpx.AsyncClient())

    assert isinstance(reranker, HttpRerankerClient)


def test_build_reranker_falls_back_to_local_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_RERANKER_MODEL", LOCAL_RERANKER_MODEL)
    monkeypatch.delenv("OF_RERANKER_API_KEY", raising=False)

    reranker = _build_reranker(Settings(), httpx.AsyncClient())

    assert isinstance(reranker, CrossEncoderReranker)


def test_build_reranker_disabled_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OF_RERANKER_MODEL", raising=False)

    assert _build_reranker(Settings(), httpx.AsyncClient()) is None


def test_telegram_admin_ids_parsed_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TELEGRAM_ADMIN_IDS", "123456, 789012")

    assert TelegramSettings().telegram_admin_ids == [123456, 789012]


def test_telegram_bot_username_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    written = (
        "octoforge_bot",
        "@octoforge_bot",
        "https://t.me/octoforge_bot",
        " t.me/octoforge_bot/ ",
    )
    for raw in written:
        monkeypatch.setenv("OF_TELEGRAM_BOT_USERNAME", raw)

        assert TelegramSettings().resolved_bot_username() == "octoforge_bot"


def test_telegram_bot_username_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OF_TELEGRAM_BOT_USERNAME", raising=False)

    assert TelegramSettings().resolved_bot_username() == ""


def test_telegram_admin_ids_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OF_TELEGRAM_ADMIN_IDS", raising=False)

    assert TelegramSettings(_env_file=None).telegram_admin_ids == []  # type: ignore[call-arg]


# --- deep vision (image_look tool) ---------------------------------------------


def test_deep_vision_model_defaults_and_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OF_VISION_DEEP_MODEL", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.vision_deep_model == DEFAULT_VISION_DEEP_MODEL
    assert settings.deep_vision_configured()


def test_empty_deep_vision_model_turns_the_tool_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_VISION_DEEP_MODEL", "")

    settings = Settings()

    assert not settings.deep_vision_configured()


def test_deep_vision_config_mirrors_the_cheap_tier_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", "llm-key")
    monkeypatch.delenv("OF_VISION_API_KEY", raising=False)
    monkeypatch.setenv("OF_VISION_DEEP_MODEL", "qwen3.5:397b")

    config = Settings().to_deep_vision_config()

    assert config.model == "qwen3.5:397b"
    assert config.api_key == "llm-key"  # falls back to the main LLM's key, like to_vision_config()


def test_deep_vision_config_prefers_its_own_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", "llm-key")
    monkeypatch.setenv("OF_VISION_API_KEY", "vision-key")

    config = Settings().to_deep_vision_config()

    assert config.api_key == "vision-key"
