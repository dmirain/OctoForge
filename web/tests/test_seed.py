"""Tests for application startup resilience around instruction seeding."""

import logging
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octoforge_web.config import Settings
from octoforge_web.main import create_app

TEST_BASE_URL = "http://test-llm/v1"
DEAD_EMBEDDINGS_URL = "http://127.0.0.1:9"
EMBEDDING_API_KEY = "test-embeddings-key"
HEALTH_STATUS_OK = "ok"
SEEDING_WARNING = "Instruction seeding failed"


@pytest.fixture()
def app_settings(tmp_path: Path) -> Settings:
    """Settings with a configured but unreachable embeddings endpoint."""
    database_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=database_url,
        embedding_base_url=DEAD_EMBEDDINGS_URL,
        embedding_api_key=EMBEDDING_API_KEY,
    )


def test_app_starts_when_embeddings_endpoint_is_unreachable(
    app_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A seeding failure degrades to a warning instead of killing the startup."""
    app = create_app(app_settings)
    with (
        caplog.at_level(logging.WARNING, logger="octoforge_web.main"),
        TestClient(app) as client,
    ):
        response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": HEALTH_STATUS_OK}
    assert any(SEEDING_WARNING in record.message for record in caplog.records)
