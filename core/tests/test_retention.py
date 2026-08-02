"""Retention: what the sweep refuses to delete matters more than what it deletes.

Every test here is about a guard. Getting this wrong does not raise an error —
it quietly destroys history the installation believed it had, which is why the
default is "keep everything" and each rule is asserted rather than described.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.models import SummaryRow
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import DialogRow, ExchangeRow, MessageRow
from octoforge_core.domain import MessageRole
from octoforge_core.retention import RetentionPolicy
from octoforge_core.retention_sweep import RetentionSweeper
from octoforge_core.tasks.api import TaskStatus
from octoforge_core.tasks.models import TaskRow

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
ANCIENT = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=1)
DIALOG = "dialog-1"
USER = "user-1"
CHANNEL = "web"
KEEP_A_YEAR = 365


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(DialogRow(id=DIALOG, user_id=USER, channel=CHANNEL))
        await session.commit()
    yield factory
    await engine.dispose()


async def add_message(
    factory: async_sessionmaker[AsyncSession], seq: int, created_at: datetime
) -> None:
    async with factory() as session:
        session.add(
            MessageRow(
                id=f"m{seq}",
                dialog_id=DIALOG,
                seq=seq,
                role=MessageRole.USER.value,
                content=f"message {seq}",
                created_at=created_at,
            )
        )
        await session.commit()


async def add_summary(factory: async_sessionmaker[AsyncSession], seq_to: int) -> None:
    async with factory() as session:
        session.add(
            SummaryRow(
                id="s1", dialog_id=DIALOG, seq_from=1, seq_to=seq_to, topics=[], content="summary"
            )
        )
        await session.commit()


async def count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_the_default_policy_deletes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An upgrade must never switch retention on by itself."""
    await add_message(session_factory, 1, ANCIENT)
    await add_summary(session_factory, seq_to=10)

    outcome = await RetentionSweeper(session_factory, RetentionPolicy()).sweep()

    assert outcome.total() == 0
    assert await count(session_factory, MessageRow) == 1


async def test_a_message_the_runner_would_reload_is_never_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Everything at or after the compaction boundary is the narrative a
    restarting runner rebuilds from. Age is no reason to drop it — losing one
    would silently change what the agent believes happened."""
    await add_message(session_factory, 1, ANCIENT)  # behind the summary
    await add_message(session_factory, 2, ANCIENT)  # past the boundary, still ancient
    await add_summary(session_factory, seq_to=1)

    await RetentionSweeper(session_factory, RetentionPolicy(messages_days=30)).sweep()

    async with session_factory() as session:
        remaining = (await session.scalars(select(MessageRow.seq))).all()
    assert list(remaining) == [2]


async def test_a_dialog_that_was_never_compacted_keeps_everything(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no summary there is no boundary, so nothing is behind one."""
    await add_message(session_factory, 1, ANCIENT)

    await RetentionSweeper(session_factory, RetentionPolicy(messages_days=30)).sweep()

    assert await count(session_factory, MessageRow) == 1


async def test_recent_messages_survive_regardless_of_the_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await add_message(session_factory, 1, RECENT)
    await add_summary(session_factory, seq_to=10)

    await RetentionSweeper(session_factory, RetentionPolicy(messages_days=30)).sweep()

    assert await count(session_factory, MessageRow) == 1


@pytest.mark.parametrize("status", list(ExchangeStatus))
async def test_only_settled_exchanges_age_out(
    session_factory: async_sessionmaker[AsyncSession], status: ExchangeStatus
) -> None:
    """A live obligation is work in flight; an old timestamp on one usually means
    it was neglected, which is the last thing to clean up silently."""
    async with session_factory() as session:
        session.add(
            ExchangeRow(
                id="e1",
                dialog_id=DIALOG,
                status=status.value,
                title="an obligation",
                created_at=ANCIENT,
            )
        )
        await session.commit()

    await RetentionSweeper(session_factory, RetentionPolicy(exchanges_days=30)).sweep()

    settled = status in (
        ExchangeStatus.ANSWERED,
        ExchangeStatus.CANCELLED,
        ExchangeStatus.FAILED,
    )
    assert await count(session_factory, ExchangeRow) == (0 if settled else 1)


async def test_an_undelivered_task_is_never_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Its result has not reached the user yet; age is not consent to drop it."""
    async with session_factory() as session:
        session.add(
            TaskRow(
                id="t1",
                dialog_id=DIALOG,
                kind="background",
                title="pending delivery",
                input={},
                status=TaskStatus.DONE.value,
                created_at=ANCIENT,
                delivered_at=None,
            )
        )
        session.add(
            TaskRow(
                id="t2",
                dialog_id=DIALOG,
                kind="background",
                title="delivered",
                input={},
                status=TaskStatus.DONE.value,
                created_at=ANCIENT,
                delivered_at=ANCIENT,
            )
        )
        await session.commit()

    await RetentionSweeper(session_factory, RetentionPolicy(tasks_days=30)).sweep()

    async with session_factory() as session:
        remaining = (await session.scalars(select(TaskRow.id))).all()
    assert list(remaining) == ["t1"]


def test_each_entity_is_configured_on_its_own() -> None:
    """Separate knobs, because a transcript, an obligation and a delivered task
    age very differently."""
    policy = RetentionPolicy(messages_days=KEEP_A_YEAR)

    assert policy.enabled() is True
    assert policy.cutoff(policy.messages_days, now=NOW) == NOW - timedelta(days=KEEP_A_YEAR)
    assert policy.cutoff(policy.exchanges_days, now=NOW) is None
    assert "messages" in policy.describe()
    assert "exchanges" not in policy.describe()


def test_an_unconfigured_policy_says_so_in_the_report() -> None:
    assert RetentionPolicy().enabled() is False
    assert "every row is kept" in RetentionPolicy().describe()
