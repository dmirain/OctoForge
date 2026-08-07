"""The media endpoints: where an out-of-process surface reaches the model.

This is the boundary that closes the hole these endpoints exist for. The
ingestion node cannot resolve a person, so before this it called vision and
speech itself — unchecked and unledgered. Here the service resolves, admits,
checks the plan and meters, and the node only asks.
"""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from octoforge_core.media.api import MediaOutcome
from octoforge_server.auth import hash_password
from octoforge_server.config import Settings
from octoforge_telegram.gateway import basic_auth_header

from octoforge_deploy.main import create_app

ADMIN_USER = "operator"
ADMIN_PASSWORD = "operator-password"
SERVICE_USER = "telegram-ingest"
SERVICE_PASSWORD = "a-long-generated-secret"
TEST_ITERATIONS = 1
TEST_BASE_URL = "http://llm.invalid"
IMAGE_REF = "tgfile:img-1"
AUDIO_REF = "tgfile:voice-1"
ACCOUNT = "424242"
DESCRIBE = "/api/media/describe"
TRANSCRIBE = "/api/media/transcribe"
CAPABILITIES = "/api/media/capabilities"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/media-test.db",
        admin_username=ADMIN_USER,
        admin_password_hash=hash_password(ADMIN_PASSWORD, iterations=TEST_ITERATIONS),
        service_username=SERVICE_USER,
        service_password_hash=hash_password(SERVICE_PASSWORD, iterations=TEST_ITERATIONS),
    )


@pytest.fixture
def ingest(tmp_path: Path) -> Iterator[TestClient]:
    """A client carrying what the ingestion node carries: its service credential."""
    with TestClient(
        create_app(make_settings(tmp_path)),
        # no X-Channel: this assembly installs no Telegram surface, so the
        # deployment's own channel is the honest default here
        headers=basic_auth_header(SERVICE_USER, SERVICE_PASSWORD) | {"X-User-Id": ACCOUNT},
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as test_client:
        yield test_client


def test_the_service_credential_opens_these_endpoints(ingest: TestClient) -> None:
    """The node holds no operator credential; without this it could not ask."""
    response = ingest.post(DESCRIBE, json={"refs": [IMAGE_REF]})

    assert response.status_code == HTTPStatus.OK


def test_an_unauthenticated_caller_is_refused(anonymous: TestClient) -> None:
    """These endpoints spend money on a model; they are not open."""
    response = anonymous.post(DESCRIBE, json={"refs": [IMAGE_REF]})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_reference_per_result_comes_back_in_order(ingest: TestClient) -> None:
    """The album contract: one outcome per picture, so a failed third page
    keeps its slot instead of shifting the other four."""
    response = ingest.post(DESCRIBE, json={"refs": [IMAGE_REF, "tgfile:img-2"]})

    results = response.json()["results"]
    assert len(results) == len([IMAGE_REF, "tgfile:img-2"])
    # this deployment configures no vision model, so every one is unavailable
    assert {item["outcome"] for item in results} == {MediaOutcome.UNAVAILABLE}


def test_an_unconfigured_installation_answers_unavailable_not_an_error(
    ingest: TestClient,
) -> None:
    """A feature that is off is not a failure: the surface falls back to text."""
    response = ingest.post(
        TRANSCRIBE, json={"ref": AUDIO_REF, "seconds": 12, "min_seconds": 1, "max_seconds": 600}
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["outcome"] == MediaOutcome.UNAVAILABLE


def test_capabilities_tell_the_node_what_it_will_get(ingest: TestClient) -> None:
    """What the ingestion node logs at startup, in place of reading settings
    that no longer govern anything on its side."""
    body = ingest.get(CAPABILITIES).json()

    assert body == {"describes_images": False, "transcribes_audio": False}


def test_an_empty_batch_is_refused_by_the_schema(ingest: TestClient) -> None:
    response = ingest.post(DESCRIBE, json={"refs": []})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_banned_account_cannot_spend_a_model_call(ingest: TestClient) -> None:
    """Admission runs here too, because `get_user_id` is what resolves the
    person: without it a banned account would still burn vision at ingestion,
    where nothing else checks anything."""
    ingest.post(DESCRIBE, json={"refs": [IMAGE_REF]})  # mints the person
    people = ingest.get(
        "/api/admin/users", headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD)
    ).json()["items"]
    (person,) = [item for item in people if item["user_id"]]

    banned = ingest.post(
        f"/api/admin/users/{person['user_id']}/status?status=banned",
        headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD),
    )
    refused = ingest.post(DESCRIBE, json={"refs": [IMAGE_REF]})

    assert banned.status_code == HTTPStatus.OK
    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert refused.headers["X-Access-Status"] == "banned"
