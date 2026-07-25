"""Tests for the operator console: the credential gate and the entity endpoints."""

import base64
import logging
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from octoforge_web.auth import hash_password, verify_password
from octoforge_web.config import Settings
from octoforge_web.main import create_app

TEST_BASE_URL = "http://test-llm/v1"
ADMIN_USER = "operator"
ADMIN_PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "correct horse battery stapl"
# a low cost keeps the suite fast; the stored hash carries its own iterations
TEST_ITERATIONS = 1_000
USER_ID_HEADER = "X-User-Id"
LISTING_PATHS = (
    "/api/admin/totals",
    "/api/admin/dialogs",
    "/api/admin/tasks",
    "/api/admin/cron",
    "/api/admin/instructions",
    "/api/admin/datasets",
    "/api/admin/memories",
    "/api/admin/summaries",
)
EMPTY_TOTALS = 0


def basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Basic credentials as a header: this TestClient takes no `auth=` argument."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def make_settings(tmp_path: Path, *, with_admin: bool = True) -> Settings:
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/console-test.db",
        admin_username=ADMIN_USER,
        admin_password_hash=(
            hash_password(ADMIN_PASSWORD, iterations=TEST_ITERATIONS) if with_admin else ""
        ),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(make_settings(tmp_path)),
        headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD),
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as test_client:
        yield test_client


def test_password_hash_round_trip() -> None:
    encoded = hash_password(ADMIN_PASSWORD, iterations=TEST_ITERATIONS)

    assert verify_password(ADMIN_PASSWORD, encoded)
    assert not verify_password(WRONG_PASSWORD, encoded)


def test_malformed_hash_never_verifies() -> None:
    assert not verify_password(ADMIN_PASSWORD, "not-a-hash")
    assert not verify_password(ADMIN_PASSWORD, "bcrypt:12:salt:digest")


def test_health_probes_stay_open(anonymous: TestClient) -> None:
    """The container healthcheck and any uptime monitor must not need a secret."""
    assert anonymous.get("/health").status_code == HTTPStatus.OK
    assert anonymous.get("/health/ready").status_code == HTTPStatus.OK


@pytest.mark.parametrize(
    "path", ["/api/admin/totals", "/api/dialog/events", "/admin.html", "/docs"]
)
def test_everything_else_demands_a_credential(anonymous: TestClient, path: str) -> None:
    response = anonymous.get(path)

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_is_rejected(anonymous: TestClient) -> None:
    response = anonymous.get(
        "/api/admin/totals", headers=basic_auth_header(ADMIN_USER, WRONG_PASSWORD)
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_wrong_user_is_rejected(anonymous: TestClient) -> None:
    response = anonymous.get(
        "/api/admin/totals", headers=basic_auth_header("someone", ADMIN_PASSWORD)
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_unconfigured_credentials_fail_closed(tmp_path: Path) -> None:
    """An empty password must never mean "open": the host is publicly reachable."""
    with TestClient(create_app(make_settings(tmp_path, with_admin=False))) as client:
        response = client.get(
            "/api/admin/totals", headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD)
        )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


@pytest.mark.parametrize("path", LISTING_PATHS)
def test_listings_answer_with_a_page(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    if path.endswith("totals"):
        assert payload["dialogs"] == EMPTY_TOTALS
    else:
        assert payload["items"] == []
        assert payload["total"] == EMPTY_TOTALS
        assert payload["limit"] > 0


def test_console_page_is_served(client: TestClient) -> None:
    response = client.get("/admin.html")

    assert response.status_code == HTTPStatus.OK
    assert "OctoForge" in response.text


def test_dialog_and_its_messages_are_visible(client: TestClient) -> None:
    client.post("/api/dialog/messages", json={"content": "hi"}, headers={USER_ID_HEADER: "alice"})

    dialogs = client.get("/api/admin/dialogs").json()
    dialog_id = dialogs["items"][0]["id"]
    messages = client.get(f"/api/admin/dialogs/{dialog_id}/messages").json()

    assert dialogs["total"] == 1
    assert dialogs["items"][0]["user_id"] == "alice"
    assert [item["content"] for item in messages["items"]] == ["hi"]


def test_paging_parameters_are_validated(client: TestClient) -> None:
    assert client.get("/api/admin/dialogs?limit=0").status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get("/api/admin/dialogs?offset=-1").status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_missing_entities_answer_404(client: TestClient) -> None:
    assert client.delete("/api/admin/tasks/nope").status_code == HTTPStatus.NOT_FOUND
    assert client.delete("/api/admin/cron/nope").status_code == HTTPStatus.NOT_FOUND
    assert client.post("/api/admin/instructions/nope/publish").status_code == HTTPStatus.NOT_FOUND
    assert (
        client.delete("/api/admin/memories/nope?user_id=alice").status_code == HTTPStatus.NOT_FOUND
    )


def test_httpx_logging_never_carries_the_bot_token(tmp_path: Path) -> None:
    """The app process also runs the bot: httpx INFO would log the token in the URL."""
    logging.getLogger("httpx").setLevel(logging.INFO)

    create_app(make_settings(tmp_path))

    assert logging.getLogger("httpx").level == logging.WARNING
