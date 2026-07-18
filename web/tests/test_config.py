"""Tests for the web settings (env parsing and core config conversion)."""

import pytest

from octoforge_web.config import (
    DEFAULT_CRON_LEASE_TTL_SECONDS,
    DEFAULT_CRON_POLL_INTERVAL_SECONDS,
    DEFAULT_CRON_REPLAY_LIMIT,
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
WHITELIST_JSON = (
    '[{"base_url_prefix": "https://internal.example.com/", '
    '"header_name": "X-Api-Key", "header_value": "s3cret"}]'
)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "OF_EMBEDDING_BASE_URL",
        "OF_EMBEDDING_API_KEY",
        "OF_EMBEDDING_MODEL",
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
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings()

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
    assert settings.to_embedding_config().model == DEFAULT_EMBEDDING_MODEL
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

    settings = Settings()

    assert settings.self_base_url == CUSTOM_SELF_BASE_URL
    assert settings.cron_poll_interval_seconds == CUSTOM_CRON_POLL_INTERVAL
    assert settings.cron_lease_ttl_seconds == CUSTOM_CRON_LEASE_TTL
    assert settings.cron_replay_limit == CUSTOM_CRON_REPLAY_LIMIT


def test_auth_whitelist_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EXTERNAL_CALL_AUTH_WHITELIST", WHITELIST_JSON)

    settings = Settings()
    whitelist = settings.to_external_call_auth_whitelist()

    assert len(whitelist) == 1
    entry = whitelist[0]
    assert entry.base_url_prefix == "https://internal.example.com/"
    assert entry.header_name == "X-Api-Key"
    assert entry.header_value == "s3cret"
