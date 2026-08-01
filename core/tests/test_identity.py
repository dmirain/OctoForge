"""Identity: one person, several surfaces, and the account that must not be shared.

The rules here are what make a person survive changing their Telegram account,
and what keeps two people from ever answering to one. Both are the sort of
thing that fails silently — a stolen account delivers somebody else's
messages, a lost one strands everything they own — so each is asserted rather
than described.
"""

from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.identity.api import (
    IdentityNotFoundError,
    IdentityTakenError,
    UserNotFoundError,
)
from octoforge_core.identity.store import SqlAlchemyIdentityStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TELEGRAM = "telegram"
WEB = "web"
ACCOUNT = "102161717"
OTHER_ACCOUNT = "500500500"


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyIdentityStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyIdentityStore(create_session_factory(engine))
    await engine.dispose()


async def test_a_persons_id_carries_no_structure(store: SqlAlchemyIdentityStore) -> None:
    """The delivery address used to be parsed back out of the identity. An id
    that carries nothing makes that impossible rather than discouraged."""
    user = await store.create_user()

    assert TELEGRAM not in user.id
    assert ACCOUNT not in user.id
    assert ":" not in user.id


async def test_an_account_leads_to_its_person(store: SqlAlchemyIdentityStore) -> None:
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)

    assert await store.resolve(TELEGRAM, ACCOUNT) == user.id


async def test_an_unknown_account_belongs_to_nobody(store: SqlAlchemyIdentityStore) -> None:
    assert await store.resolve(TELEGRAM, ACCOUNT) is None


async def test_the_same_id_on_two_surfaces_is_two_people(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Surfaces number their users independently; `42` on one is not `42` on
    another, and treating them as one would hand over a stranger's dialogs."""
    telegram_user = await store.create_user()
    web_user = await store.create_user()
    await store.link(telegram_user.id, TELEGRAM, "42")
    await store.link(web_user.id, WEB, "42")

    assert await store.resolve(TELEGRAM, "42") == telegram_user.id
    assert await store.resolve(WEB, "42") == web_user.id


async def test_one_account_never_belongs_to_two_people(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The invariant the whole table exists for. Silently reassigning would
    deliver one person's messages to another."""
    first = await store.create_user()
    second = await store.create_user()
    await store.link(first.id, TELEGRAM, ACCOUNT)

    with pytest.raises(IdentityTakenError):
        await store.link(second.id, TELEGRAM, ACCOUNT)

    assert await store.resolve(TELEGRAM, ACCOUNT) == first.id


async def test_linking_an_account_again_to_its_own_person_is_not_a_conflict(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Coming back is not a collision — and a revoked identity revives."""
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)
    await store.deactivate(TELEGRAM, ACCOUNT)

    revived = await store.link(user.id, TELEGRAM, ACCOUNT)

    assert revived.active
    assert await store.resolve(TELEGRAM, ACCOUNT) == user.id


async def test_a_person_keeps_everything_when_their_account_changes(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The whole point: the core id does not move, so dialogs, memories,
    skills and secrets follow without being touched."""
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)

    await store.reseat(TELEGRAM, user.id, OTHER_ACCOUNT)

    assert await store.resolve(TELEGRAM, OTHER_ACCOUNT) == user.id
    assert await store.resolve(TELEGRAM, ACCOUNT) is None


async def test_reseating_onto_somebody_elses_account_is_refused(
    store: SqlAlchemyIdentityStore,
) -> None:
    mover = await store.create_user()
    occupant = await store.create_user()
    await store.link(mover.id, TELEGRAM, ACCOUNT)
    await store.link(occupant.id, TELEGRAM, OTHER_ACCOUNT)

    with pytest.raises(IdentityTakenError):
        await store.reseat(TELEGRAM, mover.id, OTHER_ACCOUNT)

    assert await store.resolve(TELEGRAM, OTHER_ACCOUNT) == occupant.id


async def test_reseating_somebody_with_no_identity_there_is_refused(
    store: SqlAlchemyIdentityStore,
) -> None:
    """There is nothing to move, and inventing one would be a link in disguise."""
    user = await store.create_user()

    with pytest.raises(IdentityNotFoundError):
        await store.reseat(TELEGRAM, user.id, ACCOUNT)


async def test_a_revoked_account_is_not_a_way_in(store: SqlAlchemyIdentityStore) -> None:
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)

    await store.deactivate(TELEGRAM, ACCOUNT)

    assert await store.resolve(TELEGRAM, ACCOUNT) is None


async def test_a_revoked_account_keeps_its_history(store: SqlAlchemyIdentityStore) -> None:
    """Deleting the row would lose that it was ever used, and by whom."""
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)
    await store.deactivate(TELEGRAM, ACCOUNT)

    identities = await store.identities_of(user.id)

    assert [(item.surface, item.external_id, item.active) for item in identities] == [
        (TELEGRAM, ACCOUNT, False)
    ]


async def test_a_person_can_be_known_on_several_surfaces(
    store: SqlAlchemyIdentityStore,
) -> None:
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT)
    await store.link(user.id, WEB, "dmirain")

    assert {item.surface for item in await store.identities_of(user.id)} == {TELEGRAM, WEB}
    assert await store.resolve(WEB, "dmirain") == user.id


async def test_surface_extras_ride_along_with_the_identity(
    store: SqlAlchemyIdentityStore,
) -> None:
    """A JSON column is right here — nothing about it needs enforcing — and
    wrong for the account itself, which needs a unique constraint."""
    user = await store.create_user()
    await store.link(user.id, TELEGRAM, ACCOUNT, details={"username": "someone"})

    found = await store.find_by_identity(TELEGRAM, ACCOUNT)

    assert found is not None
    assert found.details == {"username": "someone"}


async def test_an_unknown_person_is_an_error_not_an_empty_user(
    store: SqlAlchemyIdentityStore,
) -> None:
    with pytest.raises(UserNotFoundError):
        await store.get_user("nobody")
