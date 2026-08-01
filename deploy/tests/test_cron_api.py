"""Tests for the cron job API."""

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from octoforge_server.auth import hash_password
from octoforge_server.config import Settings

from octoforge_deploy.main import create_app

TEST_BASE_URL = "http://test-llm/v1"
ADMIN_USER = "operator"
ADMIN_PASSWORD = "console-secret"
ADMIN_ITERATIONS = 1_000
USER_A = "alice"
USER_B = "bob"
USER_ID_HEADER = "X-User-Id"
JOBS_URL = "/api/cron/jobs"
DAILY_9AM = "0 9 * * *"
WEB_CHANNEL = "web"
MISSING_JOB_ID = "no-such-job"
EXPECTED_TWO_JOBS = 2


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Basic credentials as a header: this TestClient takes no `auth=` argument."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/octoforge-test.db"
    settings = Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=database_url,
        admin_username=ADMIN_USER,
        admin_password_hash=hash_password(ADMIN_PASSWORD, iterations=ADMIN_ITERATIONS),
    )
    # every endpoint but the health probes sits behind the operator credential
    with TestClient(
        create_app(settings), headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD)
    ) as test_client:
        yield test_client


def create_job(
    client: TestClient,
    user_id: str = USER_A,
    **overrides: str | None,
) -> httpx.Response:
    params = {
        "title": "morning report",
        "schedule": DAILY_9AM,
        "prompt": "prepare the report",
        "timezone": "UTC",
    }
    for key, value in overrides.items():
        if value is None:
            params.pop(key)
        else:
            params[key] = value
    return client.post(JOBS_URL, params=params, headers={USER_ID_HEADER: user_id})


def create_job_id(client: TestClient, user_id: str = USER_A) -> str:
    response = create_job(client, user_id)
    return str(response.json()["id"])


def test_create_job_returns_the_created_job(client: TestClient) -> None:
    response = create_job(client, timezone="Europe/Moscow")

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["id"]
    # the person behind the header, not the header itself
    assert body["user_id"] != USER_A
    assert body["channel"] == WEB_CHANNEL
    assert body["title"] == "morning report"
    assert body["schedule"] == DAILY_9AM
    assert body["timezone"] == "Europe/Moscow"
    assert body["prompt"] == "prepare the report"
    assert body["enabled"] is True
    assert body["last_fire_at"] is None
    assert body["one_shot"] is False
    assert body["last_status"] is None
    assert body["last_error"] is None
    assert body["retry_count"] == 0
    assert datetime.fromisoformat(body["next_fire_at"]) > datetime.now(UTC)


def test_create_job_with_one_shot_flag(client: TestClient) -> None:
    response = create_job(client, one_shot="true")

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["one_shot"] is True


def test_create_job_defaults_the_timezone_to_utc(client: TestClient) -> None:
    response = create_job(client, timezone=None)

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["timezone"] == "UTC"


def test_create_job_rejects_an_invalid_schedule(client: TestClient) -> None:
    response = create_job(client, schedule="not a cron")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "invalid cron expression" in response.json()["detail"]


def test_create_job_rejects_an_unknown_timezone(client: TestClient) -> None:
    response = create_job(client, timezone="Mars/Olympus_Mons")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "unknown IANA timezone" in response.json()["detail"]


def test_create_job_requires_all_mandatory_params(client: TestClient) -> None:
    response = create_job(client, prompt=None)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_list_jobs_is_scoped_to_the_user(client: TestClient) -> None:
    create_job(client, USER_A, title="first")
    create_job(client, USER_A, title="second")
    create_job(client, USER_B, title="bob's job")

    jobs_a = client.get(JOBS_URL, headers={USER_ID_HEADER: USER_A})
    jobs_b = client.get(JOBS_URL, headers={USER_ID_HEADER: USER_B})

    assert jobs_a.status_code == HTTPStatus.OK
    assert [job["title"] for job in jobs_a.json()] == ["first", "second"]
    assert [job["title"] for job in jobs_b.json()] == ["bob's job"]


def test_delete_job_removes_only_the_owned_job(client: TestClient) -> None:
    job_id = create_job_id(client)

    deleted = client.delete(f"{JOBS_URL}/{job_id}", headers={USER_ID_HEADER: USER_A})

    assert deleted.status_code == HTTPStatus.NO_CONTENT
    assert client.get(JOBS_URL, headers={USER_ID_HEADER: USER_A}).json() == []

    job_id = create_job_id(client)
    foreign = client.delete(f"{JOBS_URL}/{job_id}", headers={USER_ID_HEADER: USER_B})
    missing = client.delete(f"{JOBS_URL}/{MISSING_JOB_ID}", headers={USER_ID_HEADER: USER_A})

    assert foreign.status_code == HTTPStatus.NOT_FOUND
    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert len(client.get(JOBS_URL, headers={USER_ID_HEADER: USER_A}).json()) == 1


def test_pause_and_resume_toggle_the_job(client: TestClient) -> None:
    job_id = create_job_id(client)

    paused = client.post(f"{JOBS_URL}/{job_id}/pause", headers={USER_ID_HEADER: USER_A})

    assert paused.status_code == HTTPStatus.OK
    assert paused.json()["enabled"] is False

    resumed = client.post(f"{JOBS_URL}/{job_id}/resume", headers={USER_ID_HEADER: USER_A})

    assert resumed.status_code == HTTPStatus.OK
    assert resumed.json()["enabled"] is True
    # resume recomputes the next fire from now, so there is no instant catch-up
    assert datetime.fromisoformat(resumed.json()["next_fire_at"]) > datetime.now(UTC)


def test_pause_and_resume_reject_foreign_jobs(client: TestClient) -> None:
    job_id = create_job_id(client, USER_A)

    paused = client.post(f"{JOBS_URL}/{job_id}/pause", headers={USER_ID_HEADER: USER_B})
    resumed = client.post(f"{JOBS_URL}/{job_id}/resume", headers={USER_ID_HEADER: USER_B})

    assert paused.status_code == HTTPStatus.NOT_FOUND
    assert resumed.status_code == HTTPStatus.NOT_FOUND


def test_missing_user_id_header_is_rejected(client: TestClient) -> None:
    create = client.post(JOBS_URL, params={"title": "t", "schedule": DAILY_9AM, "prompt": "p"})
    listing = client.get(JOBS_URL)
    delete = client.delete(f"{JOBS_URL}/{MISSING_JOB_ID}")
    pause = client.post(f"{JOBS_URL}/{MISSING_JOB_ID}/pause")
    resume = client.post(f"{JOBS_URL}/{MISSING_JOB_ID}/resume")

    assert create.status_code == HTTPStatus.BAD_REQUEST
    assert listing.status_code == HTTPStatus.BAD_REQUEST
    assert delete.status_code == HTTPStatus.BAD_REQUEST
    assert pause.status_code == HTTPStatus.BAD_REQUEST
    assert resume.status_code == HTTPStatus.BAD_REQUEST
