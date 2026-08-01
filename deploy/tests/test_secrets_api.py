"""Tests for the self-service secrets surface: token gate, form API, openness."""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from octoforge_server.config import Settings
from octoforge_server.secret_links import SecretLinkService

from octoforge_deploy.main import create_app

TEST_BASE_URL = "http://test-llm/v1"
USER = "tg:100500"
CODE = "mail_token"
VALUE = "tok-123"
HOST = "api.mail.example.com"


def make_settings(tmp_path: Path, *, with_key: bool = True) -> Settings:
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/secrets-test.db",
        secrets_key=Fernet.generate_key().decode() if with_key else "",
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(tmp_path))) as test_client:
        yield test_client


def issue_token(client: TestClient) -> str:
    links: SecretLinkService = client.app.state.secret_links  # type: ignore[attr-defined]
    return links.issue(USER)


def test_form_and_api_are_reachable_without_the_operator_credential(
    client: TestClient,
) -> None:
    """Dialog users have no Basic credential; the token is the auth."""
    assert client.get("/secrets.html").status_code == HTTPStatus.OK
    response = client.post("/api/secrets/session", json={"token": "nope"})
    assert response.status_code == HTTPStatus.FORBIDDEN  # not 401 Basic


def test_full_form_flow(client: TestClient) -> None:
    token = issue_token(client)

    stored = client.post(
        "/api/secrets/set",
        json={"token": token, "code": CODE, "value": VALUE, "allowed_host": HOST},
    )
    listed = client.post("/api/secrets/session", json={"token": token}).json()
    deleted = client.post("/api/secrets/delete", json={"token": token, "code": CODE})
    empty = client.post("/api/secrets/session", json={"token": token}).json()

    assert stored.status_code == HTTPStatus.OK
    assert [item["code"] for item in listed["secrets"]] == [CODE]
    assert listed["secrets"][0]["allowed_host"] == HOST
    assert VALUE not in str(listed)  # values never travel back
    assert deleted.status_code == HTTPStatus.OK
    assert empty["secrets"] == []


def test_expired_or_foreign_token_is_rejected(client: TestClient) -> None:
    links: SecretLinkService = client.app.state.secret_links  # type: ignore[attr-defined]
    links.ttl_seconds = -1.0  # everything issued is instantly expired
    token = links.issue(USER)

    response = client.post(
        "/api/secrets/set",
        json={"token": token, "code": CODE, "value": VALUE, "allowed_host": HOST},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_invalid_secret_is_a_400(client: TestClient) -> None:
    token = issue_token(client)

    response = client.post(
        "/api/secrets/set",
        json={"token": token, "code": "Bad Code!", "value": VALUE, "allowed_host": HOST},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_delete_of_a_missing_secret_is_a_404(client: TestClient) -> None:
    token = issue_token(client)

    response = client.post("/api/secrets/delete", json={"token": token, "code": "absent"})

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_disabled_secrets_answer_503(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, with_key=False))) as client:
        links: SecretLinkService = client.app.state.secret_links  # type: ignore[attr-defined]
        token = links.issue(USER)

        response = client.post("/api/secrets/session", json={"token": token})

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_tokens_are_user_bound_and_expire() -> None:
    service = SecretLinkService(ttl_seconds=600)

    token = service.issue(USER)
    other = service.issue("tg:2")

    assert service.redeem(token) == USER
    assert service.redeem(other) == "tg:2"
    assert service.redeem("garbage") is None
    service.ttl_seconds = -1.0
    expired = service.issue(USER)
    assert service.redeem(expired) is None
