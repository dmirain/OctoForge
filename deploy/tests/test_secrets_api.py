"""Tests for the self-service secrets surface: token gate, form API, openness."""

import sqlite3
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from octoforge_core.secrets.api import SecretFormPrefill, SecretPlacement, SecretTransform
from octoforge_server.config import Settings
from octoforge_server.secret_links import LinkSubject, SecretLinkService

from octoforge_deploy.main import create_app

TEST_BASE_URL = "http://test-llm/v1"
# Deliberately ASYMMETRIC: the link names the Telegram account, the store is
# keyed by the person. Making these the same string is exactly what let the
# form write into a namespace the agent never reads — put and get agreed with
# each other and with nothing else.
ACCOUNT = "100500"
CODE = "mail_token"
VALUE = "tok-123"
HOST = "api.mail.example.com"
DESCRIPTION = "test mailbox token"


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


SUBJECT = LinkSubject(surface="telegram", external_id=ACCOUNT)


def link_service(client: TestClient) -> SecretLinkService:
    return client.app.state.secret_links  # type: ignore[attr-defined,no-any-return]


def issue_token(client: TestClient) -> str:
    return link_service(client).issue(SUBJECT)


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
        json={
            "token": token,
            "code": CODE,
            "value": VALUE,
            "allowed_host": HOST,
            "description": DESCRIPTION,
        },
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


def test_the_form_writes_where_the_agent_reads(client: TestClient, tmp_path: Path) -> None:
    """The regression that made every new secret silently unusable.

    The link names a Telegram account; the agent resolves secrets by the
    person, because that is what a tool context carries. While the form took
    the account id at face value the two never met: `put` and `get` agreed
    with each other and with nothing else, so a saved secret came back to the
    agent as SECRET_MISSING with no error on the way in.

    Asserted against the database rather than through the API, because the
    API would resolve the token the same way twice and agree with itself.
    """
    client.post(
        "/api/secrets/set",
        json={
            "token": issue_token(client),
            "code": CODE,
            "value": VALUE,
            "allowed_host": HOST,
            "description": DESCRIPTION,
        },
    )

    with sqlite3.connect(tmp_path / "secrets-test.db") as db:
        owner = db.execute("select user_id from secrets where code = ?", (CODE,)).fetchone()
        person = db.execute(
            "select user_id from user_identities where surface = ? and external_id = ?",
            (SUBJECT.surface, SUBJECT.external_id),
        ).fetchone()

    assert owner is not None, "the form stored nothing"
    assert person is not None, "the account was never resolved to a person"
    assert owner[0] == person[0]
    assert owner[0] != f"tg:{ACCOUNT}"  # the shape the old code stored under


def test_expired_or_foreign_token_is_rejected(client: TestClient) -> None:
    links = link_service(client)
    links.ttl_seconds = -1.0  # everything issued is instantly expired
    token = links.issue(SUBJECT)

    response = client.post(
        "/api/secrets/set",
        json={
            "token": token,
            "code": CODE,
            "value": VALUE,
            "allowed_host": HOST,
            "description": DESCRIPTION,
        },
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_invalid_secret_is_a_400(client: TestClient) -> None:
    token = issue_token(client)

    response = client.post(
        "/api/secrets/set",
        json={
            "token": token,
            "code": "Bad Code!",
            "value": VALUE,
            "allowed_host": HOST,
            "description": DESCRIPTION,
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_delete_of_a_missing_secret_is_a_404(client: TestClient) -> None:
    token = issue_token(client)

    response = client.post("/api/secrets/delete", json={"token": token, "code": "absent"})

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_disabled_secrets_answer_503(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, with_key=False))) as client:
        token = link_service(client).issue(SUBJECT)

        response = client.post("/api/secrets/session", json={"token": token})

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_tokens_name_one_account_and_expire() -> None:
    service = SecretLinkService(ttl_seconds=600)
    other = LinkSubject(surface="telegram", external_id="2")

    assert service.redeem(service.issue(SUBJECT)).subject == SUBJECT
    assert service.redeem(service.issue(other)).subject == other
    assert service.redeem("garbage") is None
    service.ttl_seconds = -1.0
    assert service.redeem(service.issue(SUBJECT)) is None


def test_a_token_is_redeemed_by_a_pod_that_did_not_issue_it() -> None:
    """The form carries no X-User-Id, so no balancer can route it back.

    Two instances of one installation stand for two pods. A token that only
    the issuing process can redeem makes the secrets form fail on whichever
    pod the browser happens to reach — with a 403 the user cannot act on.
    """
    key = Fernet.generate_key().decode()

    issued_on = SecretLinkService(key)
    redeemed_on = SecretLinkService(key)

    assert redeemed_on.redeem(issued_on.issue(SUBJECT)).subject == SUBJECT


def test_another_installations_token_is_worthless_here() -> None:
    """The key is what binds a token to an installation, not a shared table."""
    ours = SecretLinkService(Fernet.generate_key().decode())
    theirs = SecretLinkService(Fernet.generate_key().decode())

    assert ours.redeem(theirs.issue(SUBJECT)) is None


def test_an_installation_without_a_key_mints_nothing_another_can_redeem() -> None:
    """No key means the surface is off; tokens must not be forgeable meanwhile.

    Deriving from the empty string would make the link key a public constant,
    so every keyless installation would share it.
    """
    one = SecretLinkService()
    two = SecretLinkService()

    assert two.redeem(one.issue(SUBJECT)) is None


def test_person_token_carries_prefill_and_writes_under_the_person(client: TestClient) -> None:
    """The agent's secret_link tool: token names the person, form is pre-filled."""
    links = link_service(client)
    prefill = SecretFormPrefill(
        code=CODE,
        allowed_host=HOST,
        description=DESCRIPTION,
        placements=frozenset({SecretPlacement.HEADER, SecretPlacement.URL}),
        transform=SecretTransform.BASE64,
    )
    token = links.issue_for_person("person-1", prefill)

    session = client.post("/api/secrets/session", json={"token": token}).json()
    stored = client.post(
        "/api/secrets/set",
        json={
            "token": token,
            "code": CODE,
            "value": VALUE,
            "allowed_host": HOST,
            "description": DESCRIPTION,
            "placements": ["header", "url"],
            "transform": "base64",
        },
    )
    listed = client.post("/api/secrets/session", json={"token": token}).json()

    assert session["prefill"] == {
        "code": CODE,
        "allowed_host": HOST,
        "description": DESCRIPTION,
        "placements": ["header", "url"],
        "transform": "base64",
    }
    assert stored.status_code == HTTPStatus.OK
    assert listed["secrets"][0]["description"] == DESCRIPTION
    assert listed["secrets"][0]["placements"] == ["header", "url"]
    assert listed["secrets"][0]["transform"] == "base64"
    assert VALUE not in str(listed)


def test_set_without_description_is_rejected(client: TestClient) -> None:
    token = issue_token(client)

    response = client.post(
        "/api/secrets/set",
        json={
            "token": token,
            "code": CODE,
            "value": VALUE,
            "allowed_host": HOST,
            "description": "   ",
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
