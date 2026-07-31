"""Dialog ownership: who runs an actor, and how a replaced owner finds out.

Every rule here exists because a second process exists. The two questions a
claim answers are kept apart on purpose and tested apart: *was I replaced*
(the generation) and *is anyone still alive on this dialog* (the heartbeat).
Confusing them is how a live conversation gets handed to a second actor.
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.store import SqlAlchemyClaimRepository, SqlAlchemyDialogRepository
from octoforge_core.time import utc_now

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"
CHANNEL = "web"
NODE = "node-a"
OTHER_NODE = "node-b"
FIRST_GENERATION = 1
SECOND_GENERATION = 2
STALE_AFTER = timedelta(seconds=30)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def dialog_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)
    return dialog.id


@pytest.fixture
async def other_dialog_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(OTHER_USER_ID, CHANNEL)
    return dialog.id


@pytest.fixture
def claims(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyClaimRepository:
    return SqlAlchemyClaimRepository(session_factory)


async def test_a_dialog_nobody_ever_ran_is_free(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    assert await claims.current_generation(dialog_id) is None

    claim = await claims.claim(dialog_id, NODE)

    assert claim.owner == NODE
    assert claim.generation == FIRST_GENERATION


async def test_taking_a_held_dialog_always_succeeds_and_bumps_the_generation(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """Placement is the router's decision, never the database's: a handover
    must not be something a claim can refuse."""
    await claims.claim(dialog_id, NODE)

    taken = await claims.claim(dialog_id, OTHER_NODE)

    assert taken.owner == OTHER_NODE
    assert taken.generation == SECOND_GENERATION
    assert await claims.current_generation(dialog_id) == SECOND_GENERATION


async def test_a_replaced_owner_learns_it_from_its_own_heartbeat(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """The whole point of the generation: nobody has to reach the previous
    owner to tell it, which is what makes this work when it is unreachable."""
    mine = await claims.claim(dialog_id, NODE)
    assert await claims.heartbeat([mine]) == frozenset({dialog_id})

    await claims.claim(dialog_id, OTHER_NODE)

    assert await claims.heartbeat([mine]) == frozenset()


async def test_the_same_process_reclaiming_retires_its_own_older_actor(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """Owner equality is not the test — the pair (owner, generation) is. A
    rebuilt runner in the same process must retire the one it replaced."""
    first = await claims.claim(dialog_id, NODE)
    await claims.claim(dialog_id, NODE)

    assert await claims.heartbeat([first]) == frozenset()


async def test_a_heartbeat_for_a_dialog_that_is_gone_reports_it_lost(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    mine = await claims.claim(dialog_id, NODE)
    await claims.delete_for_dialog(dialog_id)

    assert await claims.heartbeat([mine]) == frozenset()


async def test_release_keeps_a_claim_somebody_else_has_taken_over(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """A tidy shutdown must not drop the NEW owner's claim: this process is
    leaving a dialog it no longer holds."""
    mine = await claims.claim(dialog_id, NODE)
    theirs = await claims.claim(dialog_id, OTHER_NODE)

    await claims.release(mine.dialog_id, mine.owner, mine.generation)

    assert await claims.current_generation(dialog_id) == theirs.generation


async def test_release_frees_the_dialog_at_once(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """A clean shutdown must not make its dialogs wait out the stale window."""
    mine = await claims.claim(dialog_id, NODE)

    await claims.release(mine.dialog_id, mine.owner, mine.generation)

    assert await claims.current_generation(dialog_id) is None


async def test_recovery_sees_a_live_peer_as_holding_its_dialog(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    await claims.claim(dialog_id, OTHER_NODE)

    held = await claims.held_elsewhere(frozenset({dialog_id}), NODE, utc_now() - STALE_AFTER)

    assert held == frozenset({dialog_id})


async def test_recovery_treats_a_silent_peer_as_gone(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    await claims.claim(dialog_id, OTHER_NODE)

    # asked as if the staleness window had already passed
    held = await claims.held_elsewhere(frozenset({dialog_id}), NODE, utc_now() + STALE_AFTER)

    assert held == frozenset()


async def test_a_restarted_process_may_recover_its_own_dialogs_at_once(
    claims: SqlAlchemyClaimRepository, dialog_id: str
) -> None:
    """Its own claim from before the restart cannot be running: the process
    that made it is the one now asking. Waiting out the window here would
    delay recovery of every dialog after every ordinary restart."""
    await claims.claim(dialog_id, NODE)

    held = await claims.held_elsewhere(frozenset({dialog_id}), NODE, utc_now() - STALE_AFTER)

    assert held == frozenset()


async def test_an_unclaimed_dialog_is_never_reported_as_held(
    claims: SqlAlchemyClaimRepository, dialog_id: str, other_dialog_id: str
) -> None:
    """Rows stranded before claims existed at all still have to be recovered,
    so "no claim" must read as free rather than as unknown."""
    await claims.claim(dialog_id, OTHER_NODE)

    held = await claims.held_elsewhere(
        frozenset({dialog_id, other_dialog_id}), NODE, utc_now() - STALE_AFTER
    )

    assert held == frozenset({dialog_id})


async def test_asking_about_nothing_touches_nothing(claims: SqlAlchemyClaimRepository) -> None:
    assert await claims.held_elsewhere(frozenset(), NODE, utc_now()) == frozenset()
    assert await claims.heartbeat([]) == frozenset()
