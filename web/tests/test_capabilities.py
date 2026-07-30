"""Tests for the startup capability report."""

import logging

import pytest
from octoforge_core.db.search_extensions import PG_TEXTSEARCH, VECTOR

from octoforge_web.capabilities import (
    CRITICAL,
    describe_capabilities,
    log_capabilities,
)
from octoforge_web.config import Settings

LLM_KEY = "sk-llm"
LLM_BASE_URL = "https://gateway.example.com/v1"
EMBEDDINGS = "embeddings"
OPERATOR = "operator credential"
TELEGRAM = "telegram"
DATABASE = "database"


def states(settings: Settings) -> dict[str, bool]:
    """Map capability name to whether the report calls it on."""
    return {cap.name: cap.enabled for cap in describe_capabilities(settings)}


def detail(settings: Settings, name: str) -> str:
    """Detail line of one capability."""
    return next(cap.detail for cap in describe_capabilities(settings) if cap.name == name)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every case from an unconfigured installation."""
    for variable in (
        "OF_LLM_API_KEY",
        "OF_EMBEDDING_API_KEY",
        "OF_EMBEDDING_BASE_URL",
        "OF_EMBEDDING_BACKEND",
        "OF_RERANKER_MODEL",
        "OF_RERANKER_API_KEY",
        "OF_SERPER_TOKEN",
        "OF_SECRETS_KEY",
        "OF_ADMIN_PASSWORD_HASH",
        "OF_TELEGRAM_BOT_TOKEN",
        "OF_TELEGRAM_ADMIN_IDS",
        "OF_STT_BASE_URL",
        "OF_STT_MODEL",
        "OF_VISION_MODEL",
        "OF_VISION_DEEP_MODEL",
        "OF_DATABASE_URL",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_bare_installation_reports_everything_off() -> None:
    report = states(Settings())

    assert not report["llm"]
    assert not report[EMBEDDINGS]
    assert not report[OPERATOR]
    assert not report[TELEGRAM]
    assert not report["web search"]
    assert not report["secret store"]
    # the database is always there — SQLite by default
    assert report[DATABASE]


def test_one_llm_key_also_lights_up_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("OF_LLM_BASE_URL", LLM_BASE_URL)
    settings = Settings()

    assert states(settings)[EMBEDDINGS]
    assert "inherited from OF_LLM_*" in detail(settings, EMBEDDINGS)
    assert "gateway.example.com" in detail(settings, EMBEDDINGS)


def test_own_embeddings_endpoint_is_reported_without_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("OF_EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("OF_EMBEDDING_BASE_URL", "https://embeddings.internal/v1")
    settings = Settings()

    assert states(settings)[EMBEDDINGS]
    assert "inherited" not in detail(settings, EMBEDDINGS)
    assert "embeddings.internal" in detail(settings, EMBEDDINGS)


def test_local_backend_is_reported_as_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_EMBEDDING_BACKEND", "local")
    settings = Settings()

    assert states(settings)[EMBEDDINGS]
    assert "local sentence-transformers" in detail(settings, EMBEDDINGS)


def test_secrets_never_appear_in_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("OF_SERPER_TOKEN", "serper-secret")
    monkeypatch.setenv("OF_SECRETS_KEY", "fernet-secret")
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", "123:bot-secret")

    rendered = "\n".join(cap.line() for cap in describe_capabilities(Settings()))

    assert LLM_KEY not in rendered
    assert "serper-secret" not in rendered
    assert "fernet-secret" not in rendered
    assert "bot-secret" not in rendered


def test_telegram_without_admins_is_reported_as_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_TELEGRAM_BOT_TOKEN", "123:abc")
    settings = Settings()

    assert states(settings)[TELEGRAM]
    assert "OPEN TO EVERYONE" in detail(settings, TELEGRAM)

    monkeypatch.setenv("OF_TELEGRAM_ADMIN_IDS", "1,2")

    assert "invite gate, 2 admin(s)" in detail(Settings(), TELEGRAM)


def test_sqlite_default_mentions_the_single_writer() -> None:
    assert "single writer" in detail(Settings(), DATABASE)


def test_postgres_url_is_reported_by_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OF_DATABASE_URL", "postgresql+asyncpg://u:p@host/db")

    assert detail(Settings(), DATABASE) == "postgresql"


def test_missing_essentials_are_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.capabilities")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_capabilities(Settings(), logger)

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]

    assert len(warnings) == len(CRITICAL)
    assert any(EMBEDDINGS in message for message in warnings)
    assert any(OPERATOR in message for message in warnings)


def test_configured_installation_warns_about_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("OF_ADMIN_PASSWORD_HASH", "pbkdf2_sha256:1:c2FsdA==:ZGlnZXN0")
    logger = logging.getLogger("test.capabilities.ok")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_capabilities(Settings(), logger)

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_search_extensions_are_reported_from_the_database_not_from_settings() -> None:
    """No OF_ variable can say whether the server has pgvector; only a probe can."""
    absent = {cap.name: cap for cap in describe_capabilities(Settings())}
    present = {
        cap.name: cap
        for cap in describe_capabilities(Settings(), frozenset({VECTOR, PG_TEXTSEARCH}))
    }

    assert absent["vector search"].enabled is False
    assert absent["lexical search"].enabled is False
    assert present["vector search"].enabled is True
    assert present["lexical search"].enabled is True


def test_a_database_without_the_extensions_is_not_a_critical_gap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Managed Postgres and SQLite simply cannot have these; that is a supported
    configuration, so it must be stated in the report and never warned about."""
    monkeypatch.setenv("OF_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("OF_ADMIN_PASSWORD_HASH", "pbkdf2_sha256:1:c2FsdA==:ZGlnZXN0")
    logger = logging.getLogger("test.capabilities.search")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_capabilities(Settings(), logger, frozenset())

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert "no pgvector" in caplog.text
