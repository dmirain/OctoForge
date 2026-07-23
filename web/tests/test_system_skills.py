"""Tests for the system-skill registry sync at application startup."""

import json
import logging
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from octoforge_core.config import EmbeddingBackend
from octoforge_core.instructions.api import InstructionType

from octoforge_web.config import Settings
from octoforge_web.main import create_app
from octoforge_web.system_skills import WEB_SYSTEM_SKILLS

TEST_BASE_URL = "http://test-llm/v1"
DEAD_EMBEDDINGS_URL = "http://127.0.0.1:9"
EMBEDDING_API_KEY = "test-embeddings-key"
HEALTH_STATUS_OK = "ok"
SYNC_WARNING = "System skill registry sync failed"


def test_web_pack_declares_the_weather_endpoint_and_scenarios() -> None:
    by_title = {entry.title: entry for entry in WEB_SYSTEM_SKILLS}
    assert set(by_title) == {"wttr_in_weather", "get_current_weather", "compare_weather_two_cities"}
    endpoint = by_title["wttr_in_weather"]
    assert endpoint.kind is InstructionType.ENDPOINT
    spec = json.loads(endpoint.content)
    assert spec["url_template"].startswith("https://wttr.in/")
    for skill_title in ("get_current_weather", "compare_weather_two_cities"):
        record = by_title[skill_title]
        assert record.kind is InstructionType.SKILL
        assert "wttr_in_weather" in record.content


@pytest.fixture()
def app_settings(tmp_path: Path) -> Settings:
    """Settings with a configured but unreachable embeddings endpoint.

    The backend is pinned to the HTTP one explicitly so a developer's local
    `.env` (e.g. OF_EMBEDDING_BACKEND=local) cannot leak into the test.
    """
    database_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=database_url,
        embedding_backend=EmbeddingBackend.OPENAI,
        embedding_base_url=DEAD_EMBEDDINGS_URL,
        embedding_api_key=EMBEDDING_API_KEY,
    )


def test_app_starts_when_embeddings_endpoint_is_unreachable(
    app_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registry-sync failure degrades to a warning instead of killing the startup."""
    app = create_app(app_settings)
    with (
        caplog.at_level(logging.WARNING, logger="octoforge_web.main"),
        TestClient(app) as client,
    ):
        response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": HEALTH_STATUS_OK}
    assert any(SYNC_WARNING in record.message for record in caplog.records)
