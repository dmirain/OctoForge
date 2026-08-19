"""The profile endpoint: how an out-of-process surface names its people.

The ingestion node knows what Telegram calls a sender and cannot write it
anywhere that matters — names key on people, and it holds no core database.
Before this endpoint existed there was nowhere to send them, and everyone
admitted through a split deployment stayed a bare id in the operator console.
"""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
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
ACCOUNT = "424242"
PROFILE = "/api/identity/profile"
USERS = "/api/admin/users"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_base_url=TEST_BASE_URL,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/identity-test.db",
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


def _people(client: TestClient) -> list[dict[str, object]]:
    response = client.get(USERS, headers=basic_auth_header(ADMIN_USER, ADMIN_PASSWORD))
    return list(response.json()["items"])


def test_a_profile_names_the_person_on_first_contact(ingest: TestClient) -> None:
    """The profile arrives before the first message has: the endpoint mints
    the person rather than updating into the void, or every newcomer would
    stay nameless until their second message."""
    response = ingest.put(PROFILE, json={"name": "Alice Smith", "username": "alice"})

    assert response.status_code == HTTPStatus.NO_CONTENT
    (person,) = _people(ingest)
    assert person["name"] == "Alice Smith"
    (identity,) = person["identities"]  # type: ignore[misc]
    assert (identity["external_id"], identity["name"], identity["username"]) == (
        ACCOUNT,
        "Alice Smith",
        "alice",
    )


def test_a_waiting_person_is_still_named(ingest: TestClient) -> None:
    """Deliberately no status gate: the console's waiting queue is where
    activation is decided, and a queue of bare ids gives the operator nothing
    to decide by."""
    response = ingest.put(PROFILE, json={"name": "Boris", "username": None})

    assert response.status_code == HTTPStatus.NO_CONTENT
    (person,) = _people(ingest)
    # minted here, never admitted: everyone is born waiting
    assert (person["status"], person["name"]) == ("waiting", "Boris")


def test_the_mirror_follows_a_rename_without_touching_the_christening(
    ingest: TestClient,
) -> None:
    """The identity's name is a living mirror; the person's canonical name is
    seeded once and is thereafter their own."""
    ingest.put(PROFILE, json={"name": "Alice Smith", "username": "alice"})
    ingest.put(PROFILE, json={"name": "Alice Cooper", "username": "acooper"})

    (person,) = _people(ingest)
    assert person["name"] == "Alice Smith"
    (identity,) = person["identities"]  # type: ignore[misc]
    assert (identity["name"], identity["username"]) == ("Alice Cooper", "acooper")


def test_an_unauthenticated_caller_is_refused(anonymous: TestClient) -> None:
    """The mirror writes into the identity store; it is not open."""
    response = anonymous.put(
        PROFILE, json={"name": "Mallory", "username": None}, headers={"X-User-Id": ACCOUNT}
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_profile_without_an_account_is_refused(ingest: TestClient) -> None:
    response = ingest.put(
        PROFILE, json={"name": "Nobody", "username": None}, headers={"X-User-Id": ""}
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
