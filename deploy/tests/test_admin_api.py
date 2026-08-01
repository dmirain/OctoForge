"""Tests for the operator console: the credential gate and the entity endpoints."""

import base64
import logging
from collections.abc import Iterator
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from octoforge_core.dialogs.api import DialogRepository, ExchangeRepository, ExchangeStatus
from octoforge_core.instructions.api import (
    InstructionDraft,
    InstructionService,
    InstructionType,
)
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.time import utc_now
from octoforge_server.auth import MAX_FAILED_ATTEMPTS, hash_password, verify_password
from octoforge_server.config import Settings
from octoforge_telegram.invites.api import Invite, InviteStatus, MemberProfile

from octoforge_deploy.main import create_app

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
    "/api/admin/exchanges",
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


def test_operator_actions_leave_an_audit_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Every mutation names who did what to which id — and no content."""
    with caplog.at_level(logging.INFO, logger="octoforge.audit"):
        client.delete("/api/admin/tasks/unknown-task")  # 404 leaves no line
        client.post("/api/admin/instructions/unknown-id/publish")

    lines = [record.getMessage() for record in caplog.records]
    assert any("action=instruction.publish" in line for line in lines)
    assert any("outcome=not_found" in line for line in lines)
    assert all(f"actor={ADMIN_USER}@" in line for line in lines)


def test_cross_site_mutation_is_refused(client: TestClient) -> None:
    """A browser posting from another site carries the credential; refuse it."""
    response = client.post(
        "/api/admin/instructions/whatever/publish",
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_same_origin_mutation_is_allowed_through(client: TestClient) -> None:
    """The console's own requests pass the CSRF check (404 = it reached the handler)."""
    response = client.post(
        "/api/admin/instructions/unknown-id/publish",
        headers={"Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_repeated_bad_credentials_are_refused_without_hashing(anonymous: TestClient) -> None:
    """The failure budget answers 429 instead of burning PBKDF2 per attempt."""
    for _ in range(MAX_FAILED_ATTEMPTS):
        anonymous.get("/api/admin/totals", headers=basic_auth_header(ADMIN_USER, WRONG_PASSWORD))

    refused = anonymous.get(
        "/api/admin/totals", headers=basic_auth_header(ADMIN_USER, WRONG_PASSWORD)
    )

    assert refused.status_code == HTTPStatus.TOO_MANY_REQUESTS


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
    # a person, not the handle the surface used: `alice` is what the caller
    # calls them, and the console shows who they are
    assert dialogs["items"][0]["user_id"] != "alice"
    assert [item["content"] for item in messages["items"]] == ["hi"]


def app_exchanges(client: TestClient) -> ExchangeRepository:
    return cast(ExchangeRepository, client.app.state.exchanges)  # type: ignore[attr-defined]


def test_exchanges_carry_dialog_identity_and_filter_by_status(client: TestClient) -> None:
    # seed through the repositories, not the dialog API: a posted message
    # would race this test by creating its own exchange via routing
    dialogs = cast(DialogRepository, client.app.state.dialogs)  # type: ignore[attr-defined]
    exchanges = app_exchanges(client)
    assert client.portal is not None
    dialog = client.portal.call(dialogs.get_or_create, "alice", "web")
    opened = client.portal.call(exchanges.create, dialog.id, "a question")
    client.portal.call(
        partial(
            exchanges.set_status,
            opened.id,
            ExchangeStatus.AWAITING_USER,
            pending_question="which one?",
        )
    )

    everything = client.get("/api/admin/exchanges").json()
    for_alice = client.get("/api/admin/exchanges?user_id=alice").json()
    for_someone_else = client.get("/api/admin/exchanges?user_id=bob").json()
    awaiting = client.get("/api/admin/exchanges?status=awaiting_user").json()

    assert everything["total"] == 1
    # this dialog was built through the repository, so its user id is whatever
    # the test wrote — the resolution below the API is not in play here
    assert everything["items"][0]["user_id"] == "alice"
    assert everything["items"][0]["channel"] == "web"
    assert for_alice["total"] == 1
    assert for_someone_else["total"] == 0
    assert awaiting["total"] == 1
    assert awaiting["items"][0]["pending_question"] == "which one?"
    assert awaiting["items"][0]["status"] == "awaiting_user"


def test_dialog_deletion_cascades_and_survives_recreation(client: TestClient) -> None:
    client.post("/api/dialog/messages", json={"content": "hi"}, headers={USER_ID_HEADER: "alice"})
    dialog_id = client.get("/api/admin/dialogs").json()["items"][0]["id"]

    deleted = client.delete(f"/api/admin/dialogs/{dialog_id}")
    listing = client.get("/api/admin/dialogs").json()
    messages = client.get(f"/api/admin/dialogs/{dialog_id}/messages").json()

    assert deleted.status_code == HTTPStatus.OK
    assert listing["total"] == 0
    assert messages["total"] == 0
    # the user is not locked out: the next contact starts a fresh dialog
    client.post("/api/dialog/messages", json={"content": "back"}, headers={USER_ID_HEADER: "alice"})
    dialogs = client.get("/api/admin/dialogs").json()
    assert dialogs["total"] == 1
    assert dialogs["items"][0]["id"] != dialog_id


def test_deleting_an_unknown_dialog_answers_404(client: TestClient) -> None:
    assert client.delete("/api/admin/dialogs/nope").status_code == HTTPStatus.NOT_FOUND


def test_paging_parameters_are_validated(client: TestClient) -> None:
    assert client.get("/api/admin/dialogs?limit=0").status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert client.get("/api/admin/dialogs?offset=-1").status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_missing_entities_answer_404(client: TestClient) -> None:
    assert client.delete("/api/admin/tasks/nope").status_code == HTTPStatus.NOT_FOUND
    assert client.delete("/api/admin/cron/nope").status_code == HTTPStatus.NOT_FOUND
    assert client.post("/api/admin/instructions/nope/publish").status_code == HTTPStatus.NOT_FOUND
    assert client.delete("/api/admin/instructions/nope").status_code == HTTPStatus.NOT_FOUND
    assert (
        client.delete("/api/admin/memories/nope?user_id=alice").status_code == HTTPStatus.NOT_FOUND
    )


def app_instructions(client: TestClient) -> InstructionService:
    return cast(InstructionService, client.app.state.instructions)  # type: ignore[attr-defined]


def test_public_instruction_is_deletable_without_owner(client: TestClient) -> None:
    """The console can drop a public record: owner_id NULL used to be unreachable."""
    service = app_instructions(client)
    assert client.portal is not None
    saved = client.portal.call(service.save, "alice", InstructionType.SKILL, "temp skill", "steps")
    published = client.portal.call(service.publish, saved.id)

    deleted = client.delete(f"/api/admin/instructions/{published.id}")
    listing = client.get("/api/admin/instructions").json()

    assert deleted.status_code == HTTPStatus.OK
    assert listing["total"] == 0


def test_private_instruction_needs_its_owner(client: TestClient) -> None:
    service = app_instructions(client)
    assert client.portal is not None
    saved = client.portal.call(service.save, "alice", InstructionType.SKILL, "own skill", "steps")

    without_owner = client.delete(f"/api/admin/instructions/{saved.id}")
    with_owner = client.delete(f"/api/admin/instructions/{saved.id}?owner_id=alice")

    assert without_owner.status_code == HTTPStatus.NOT_FOUND
    assert with_owner.status_code == HTTPStatus.OK
    assert client.get("/api/admin/instructions").json()["total"] == 0


def test_system_instruction_refuses_deletion(client: TestClient) -> None:
    # seed through the store: save_system embeds strictly and the test app
    # has no embedding backend (the lenient agent-facing save defers instead)
    store = SqlAlchemyInstructionStore(client.app.state.session_factory)  # type: ignore[attr-defined]
    draft = InstructionDraft(
        kind=InstructionType.SKILL,
        title="sys skill",
        content="steps",
        tags=(),
        embedding=(),
        system=True,
        owner_id=None,
    )
    assert client.portal is not None
    saved = client.portal.call(store.upsert, draft)

    response = client.delete(f"/api/admin/instructions/{saved.id}")

    assert response.status_code == HTTPStatus.CONFLICT
    assert client.get("/api/admin/instructions").json()["total"] == 1


def test_httpx_logging_never_carries_the_bot_token(tmp_path: Path) -> None:
    """The app process also runs the bot: httpx INFO would log the token in the URL."""
    logging.getLogger("httpx").setLevel(logging.INFO)

    create_app(make_settings(tmp_path))

    assert logging.getLogger("httpx").level == logging.WARNING


def test_telegram_users_empty_without_the_bot(client: TestClient) -> None:
    listing = client.get("/api/admin/telegram/users").json()
    assert listing == {"items": [], "total": 0}


class _StaticDirectory:
    """MemberDirectory fake serving one fixed profile."""

    def __init__(self, profile: MemberProfile) -> None:
        self._profile = profile

    async def record(
        self, user_id: str, first_name: str, last_name: str, username: str | None
    ) -> None:
        raise AssertionError("read-only endpoint must not write")

    async def get(self, user_id: str) -> MemberProfile | None:
        return self._profile

    async def list_all(self) -> list[MemberProfile]:
        return [self._profile]


def test_telegram_users_join_profile_with_invite(client: TestClient) -> None:
    profile = MemberProfile(
        user_id="tg:42",
        first_name="Alice",
        last_name="Smith",
        username="alice",
        first_seen_at=utc_now(),
        last_seen_at=utc_now(),
    )
    invite = Invite(
        id="inv-1",
        code="SECRETCODE",
        status=InviteStatus.CLAIMED,
        note="for Alice",
        created_at=utc_now(),
        claimed_at=utc_now(),
        revoked_at=None,
        claimed_by="tg:42",
        disabled_cron_job_ids=(),
    )

    class _StaticInvites:
        async def list_all(self) -> list[Invite]:
            return [invite]

    client.app.state.telegram_members = _StaticDirectory(profile)  # type: ignore[attr-defined]
    client.app.state.telegram_invites = _StaticInvites()  # type: ignore[attr-defined]
    listing = client.get("/api/admin/telegram/users").json()
    assert listing["total"] == 1
    (item,) = listing["items"]
    assert item["display_name"] == "Alice Smith (@alice)"
    assert item["invite_code"] == "SECRETCODE"
    assert item["invite_note"] == "for Alice"
    assert item["invite_status"] == "claimed"


def test_the_console_shows_people_and_the_accounts_they_answer_on(
    client: TestClient,
) -> None:
    """A person is the unit, not a handle: the same human may arrive from
    Telegram and from a browser, and everything they own is filed under them."""
    client.post("/api/dialog/messages", json={"content": "hi"}, headers={USER_ID_HEADER: "alice"})

    body = client.get("/api/admin/users").json()

    assert body["total"] == 1
    person = body["items"][0]
    assert person["user_id"] != "alice"  # the person, not what the surface calls them
    assert person["identities"] == [{"surface": "web", "external_id": "alice", "active": True}]
