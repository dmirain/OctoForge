"""Tests for the admin_manage tool of the Telegram surface."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.api import DialogRepository, MessageRepository
from octoforge_core.dialogs.store import SqlAlchemyDialogRepository, SqlAlchemyMessageRepository
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.instructions.api import InstructionService, InstructionType
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext
from octoforge_telegram.admin import (
    ACTION_GENERATE_INVITE,
    ACTION_LIST_USERS,
    ACTION_PUBLISH_INSTRUCTION,
    ACTION_RESTORE_INVITE,
    ACTION_REVOKE_INVITE,
    ACTION_SEARCH_INSTRUCTIONS,
    NO_BOT_USERNAME_HINT,
    NOT_AUTHORIZED_MESSAGE,
    PUBLISH_NOT_FOUND_MESSAGE,
    AdminAccess,
    AdminManageTool,
    AdminStores,
)
from octoforge_telegram.client import TELEGRAM_CHANNEL
from octoforge_telegram.invites.api import InviteStatus, MemberProfile
from octoforge_telegram.invites.store import SqlAlchemyInviteStore
from octoforge_telegram.schema import TelegramSurfaceBase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
ADMIN_TELEGRAM_ID = 999
ADMIN_USER_ID = "tg:999"
USER_ID = "tg:111"
DIALOG_ID = "dlg-1"
NOTE = "for Alice"
BOT_USERNAME = "octoforge_demo_bot"
CRON_SCHEDULE = "0 9 * * *"
NEXT_FIRE = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)

ADMIN_CONTEXT = ToolContext(user_id=ADMIN_USER_ID, channel=TELEGRAM_CHANNEL, dialog_id=DIALOG_ID)
USER_CONTEXT = ToolContext(user_id=USER_ID, channel=TELEGRAM_CHANNEL, dialog_id=DIALOG_ID)
WEB_ADMIN_CONTEXT = ToolContext(user_id=ADMIN_USER_ID, channel="web", dialog_id=DIALOG_ID)

StoresTuple = tuple[
    SqlAlchemyInviteStore,
    SqlAlchemyCronStore,
    MessageRepository,
    DialogRepository,
    InstructionService,
]


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


@pytest.fixture
async def stores() -> AsyncIterator[StoresTuple]:
    core_engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(core_engine)
    core_sessions: async_sessionmaker[AsyncSession] = create_session_factory(core_engine)
    telegram_engine = create_engine(MEMORY_DATABASE_URL)
    async with telegram_engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    telegram_sessions: async_sessionmaker[AsyncSession] = create_session_factory(telegram_engine)
    yield (
        SqlAlchemyInviteStore(telegram_sessions),
        SqlAlchemyCronStore(core_sessions),
        SqlAlchemyMessageRepository(core_sessions),
        SqlAlchemyDialogRepository(core_sessions),
        LocalInstructionService(SqlAlchemyInstructionStore(core_sessions), LenientEmbedder()),
    )
    await core_engine.dispose()
    await telegram_engine.dispose()


def make_tool(
    stores: StoresTuple,
    bot_username: str = "",
) -> AdminManageTool:
    invites, cron_store, messages, dialogs, instructions = stores
    access = AdminAccess(admin_ids=frozenset({ADMIN_TELEGRAM_ID}), bot_username=bot_username)
    backends = AdminStores(
        invites=invites,
        cron_store=cron_store,
        messages=messages,
        dialogs=dialogs,
        instructions=instructions,
    )
    return AdminManageTool(backends, access)


def make_cron_job(job_id: str, user_id: str, enabled: bool = True) -> CronJob:
    return CronJob(
        id=job_id,
        user_id=user_id,
        channel=TELEGRAM_CHANNEL,
        title=f"job {job_id}",
        schedule=CRON_SCHEDULE,
        timezone="UTC",
        prompt="prompt",
        enabled=enabled,
        next_fire_at=NEXT_FIRE,
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )


async def claim_invite(
    stores: StoresTuple,
    user_id: str = USER_ID,
) -> str:
    invites = stores[0]
    invite = await invites.create(NOTE)
    claimed = await invites.claim(invite.code, user_id)
    return claimed.id


async def test_non_admin_gets_not_authorized(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_LIST_USERS}, USER_CONTEXT)

    assert result == NOT_AUTHORIZED_MESSAGE


async def test_admin_on_web_channel_gets_not_authorized(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_LIST_USERS}, WEB_ADMIN_CONTEXT)

    assert result == NOT_AUTHORIZED_MESSAGE


async def test_visible_to_only_for_telegram_admins(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    assert tool.visible_to(ADMIN_CONTEXT) is True
    assert tool.visible_to(USER_CONTEXT) is False
    assert tool.visible_to(WEB_ADMIN_CONTEXT) is False


async def test_generate_invite_returns_the_code(
    stores: StoresTuple,
) -> None:
    invites = stores[0]
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_GENERATE_INVITE, "note": NOTE}, ADMIN_CONTEXT)

    pending = await invites.list_all()
    assert len(pending) == 1
    assert pending[0].status is InviteStatus.PENDING
    assert pending[0].note == NOTE
    assert pending[0].code in result
    # no handle configured: the bare code plus the hint that fixes it
    assert NO_BOT_USERNAME_HINT in result
    assert "t.me" not in result


async def test_generate_invite_returns_a_markdown_link_with_a_bot_handle(
    stores: StoresTuple,
) -> None:
    invites = stores[0]
    tool = make_tool(stores, bot_username=BOT_USERNAME)

    result = await tool.execute({"action": ACTION_GENERATE_INVITE}, ADMIN_CONTEXT)

    code = (await invites.list_all())[0].code
    assert f"[@{BOT_USERNAME}](https://t.me/{BOT_USERNAME}?start={code})" in result
    assert NO_BOT_USERNAME_HINT not in result


async def test_pending_invites_are_listed_as_links_when_the_handle_is_known(
    stores: StoresTuple,
) -> None:
    invites = stores[0]
    tool = make_tool(stores, bot_username=BOT_USERNAME)
    invite = await invites.create(NOTE)

    result = await tool.execute({"action": ACTION_LIST_USERS}, ADMIN_CONTEXT)

    assert f"https://t.me/{BOT_USERNAME}?start={invite.code}" in result
    assert NOTE in result


async def test_list_users_reports_access_stats_and_cron(
    stores: StoresTuple,
) -> None:
    _, cron_store, messages, dialogs, _ = stores
    await claim_invite(stores)
    dialog = await dialogs.get_or_create(USER_ID, TELEGRAM_CHANNEL)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hello"))
    await cron_store.create(make_cron_job("job-1", USER_ID))
    await cron_store.create(replace(make_cron_job("job-2", USER_ID), enabled=False))
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_LIST_USERS}, ADMIN_CONTEXT)

    assert USER_ID in result
    assert "access=claimed" in result
    assert "wrote 1 messages (5 chars)" in result
    assert "agent replied 0 (0 chars)" in result
    assert "cron=1/2 enabled" in result


async def test_revoke_disables_cron_jobs_and_restore_reenables_exactly_them(
    stores: StoresTuple,
) -> None:
    invites, cron_store = stores[0], stores[1]
    invite_id = await claim_invite(stores)
    await cron_store.create(make_cron_job("job-1", USER_ID))
    await cron_store.create(make_cron_job("job-2", USER_ID))
    # the user's own pause, predating the revoke: restore must not re-enable it
    await cron_store.create(replace(make_cron_job("job-3", USER_ID), enabled=False))
    tool = make_tool(stores)

    revoked = await tool.execute(
        {"action": ACTION_REVOKE_INVITE, "user_id": USER_ID}, ADMIN_CONTEXT
    )

    assert "disabled cron jobs: 2" in revoked
    jobs = {job.id: job for job in await cron_store.list_for_user(USER_ID)}
    assert jobs["job-1"].enabled is False
    assert jobs["job-2"].enabled is False
    assert jobs["job-3"].enabled is False
    invite = await invites.get_by_id(invite_id)
    assert invite is not None and invite.status is InviteStatus.REVOKED

    restored = await tool.execute(
        {"action": ACTION_RESTORE_INVITE, "invite_id": invite_id}, ADMIN_CONTEXT
    )

    assert "re-enabled cron jobs: 2" in restored
    jobs = {job.id: job for job in await cron_store.list_for_user(USER_ID)}
    assert jobs["job-1"].enabled is True
    assert jobs["job-2"].enabled is True
    assert jobs["job-3"].enabled is False  # the user's own pause survived
    invite = await invites.get_by_id(invite_id)
    assert invite is not None and invite.status is InviteStatus.CLAIMED
    assert invite.disabled_cron_job_ids == ()


async def test_revoke_pending_invite_by_id(
    stores: StoresTuple,
) -> None:
    invites = stores[0]
    invite = await invites.create(NOTE)
    tool = make_tool(stores)

    result = await tool.execute(
        {"action": ACTION_REVOKE_INVITE, "invite_id": invite.id}, ADMIN_CONTEXT
    )

    assert "disabled cron jobs: 0" in result
    fetched = await invites.get_by_id(invite.id)
    assert fetched is not None and fetched.status is InviteStatus.REVOKED


async def test_revoke_without_target_is_an_error(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_REVOKE_INVITE}, ADMIN_CONTEXT)

    assert result.startswith("error:")


async def test_search_instructions_finds_records_of_everyone(
    stores: StoresTuple,
) -> None:
    _, _, _, _, instructions = stores
    await instructions.save(USER_ID, InstructionType.SKILL, "weather scenario", "call wttr.in")
    tool = make_tool(stores)

    result = await tool.execute(
        {"action": ACTION_SEARCH_INSTRUCTIONS, "query": "weather"}, ADMIN_CONTEXT
    )

    assert "[skill] weather scenario" in result
    assert f"owner: {USER_ID}" in result
    assert "id: " in result


async def test_search_instructions_requires_a_query(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_SEARCH_INSTRUCTIONS}, ADMIN_CONTEXT)

    assert result.startswith("error:")


async def test_publish_instruction_makes_a_private_record_public(
    stores: StoresTuple,
) -> None:
    _, _, _, _, instructions = stores
    saved = await instructions.save(USER_ID, InstructionType.SKILL, "weather scenario", "steps")
    tool = make_tool(stores)

    result = await tool.execute(
        {"action": ACTION_PUBLISH_INSTRUCTION, "id": saved.id}, ADMIN_CONTEXT
    )

    assert result == "published: [skill] weather scenario"
    assert (await instructions.get_by_name("weather scenario")).owner_id is None


async def test_publish_instruction_reports_an_unknown_id(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute(
        {"action": ACTION_PUBLISH_INSTRUCTION, "id": "missing"}, ADMIN_CONTEXT
    )

    assert result == PUBLISH_NOT_FOUND_MESSAGE


async def test_publish_instruction_requires_an_id(
    stores: StoresTuple,
) -> None:
    tool = make_tool(stores)

    result = await tool.execute({"action": ACTION_PUBLISH_INSTRUCTION}, ADMIN_CONTEXT)

    assert result.startswith("error:")


class OneProfileDirectory:
    """MemberDirectory fake with a single known profile."""

    def __init__(self, profile: MemberProfile) -> None:
        self._profile = profile

    async def record(
        self, user_id: str, first_name: str, last_name: str, username: str | None
    ) -> None:
        raise AssertionError("the admin tool must not write profiles")

    async def get(self, user_id: str) -> MemberProfile | None:
        return self._profile if user_id == self._profile.user_id else None

    async def list_all(self) -> list[MemberProfile]:
        return [self._profile]


async def test_list_users_shows_name_and_invite_attribution(stores: StoresTuple) -> None:
    invites, cron_store, messages, dialogs, instructions = stores
    invite_id = await claim_invite(stores)
    invite = await invites.get_by_id(invite_id)
    assert invite is not None
    profile = MemberProfile(
        user_id=USER_ID,
        first_name="Alice",
        last_name="Smith",
        username="alice",
        first_seen_at=utc_now(),
        last_seen_at=utc_now(),
    )
    tool = AdminManageTool(
        AdminStores(
            invites=invites,
            cron_store=cron_store,
            messages=messages,
            dialogs=dialogs,
            instructions=instructions,
            directory=OneProfileDirectory(profile),
        ),
        AdminAccess(admin_ids=frozenset({ADMIN_TELEGRAM_ID})),
    )
    output = await tool.execute({"action": "list_users"}, ADMIN_CONTEXT)
    assert "Alice Smith (@alice)" in output
    assert f"claimed via invite {invite.code}" in output
    assert NOTE in output
