"""Tests for the web settings (env parsing and core config conversion)."""

import pytest

from octoforge_web.config import (
    DEFAULT_DATASETS_QUERY_DEFAULT_LIMIT,
    DEFAULT_DATASETS_QUERY_MAX_LIMIT,
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_INSTRUCTIONS_TOP_K,
    Settings,
)

CUSTOM_TOP_K = 7
CUSTOM_DATASETS_DEFAULT_LIMIT = 25
CUSTOM_DATASETS_MAX_LIMIT = 500
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


def test_auth_whitelist_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EXTERNAL_CALL_AUTH_WHITELIST", WHITELIST_JSON)

    settings = Settings()
    whitelist = settings.to_external_call_auth_whitelist()

    assert len(whitelist) == 1
    entry = whitelist[0]
    assert entry.base_url_prefix == "https://internal.example.com/"
    assert entry.header_name == "X-Api-Key"
    assert entry.header_value == "s3cret"
