"""Identity: one person, several surfaces, and the account that must not be shared.

The rules here are what make a person survive changing their Telegram account,
and what keeps two people from ever answering to one. Both are the sort of
thing that fails silently — a stolen account delivers somebody else's
messages, a lost one strands everything they own — so each is asserted rather
than described.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.identity.api import (
    IdentityKey,
    IdentityLink,
    IdentityNotFoundError,
    IdentityProfile,
    IdentityTakenError,
    UserNotFoundError,
    UserStatus,
)
from octoforge_core.identity.service import AccessService
from octoforge_core.identity.store import SqlAlchemyIdentityStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
TELEGRAM = "telegram"
WEB = "web"
ACCOUNT = "102161717"
OTHER_ACCOUNT = "500500500"
TELEGRAM_ACCOUNT = IdentityKey(TELEGRAM, ACCOUNT)


def link(
    user_id: str, key: IdentityKey = TELEGRAM_ACCOUNT, details: dict[str, str] | None = None
) -> IdentityLink:
    return IdentityLink(user_id, key, details)


def profile(name: str, username: str | None) -> IdentityProfile:
    return IdentityProfile(TELEGRAM_ACCOUNT, name, username)


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
    await store.link(link(user.id))

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
    await store.link(link(telegram_user.id, IdentityKey(TELEGRAM, "42")))
    await store.link(link(web_user.id, IdentityKey(WEB, "42")))

    assert await store.resolve(TELEGRAM, "42") == telegram_user.id
    assert await store.resolve(WEB, "42") == web_user.id


async def test_one_account_never_belongs_to_two_people(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The invariant the whole table exists for. Silently reassigning would
    deliver one person's messages to another."""
    first = await store.create_user()
    second = await store.create_user()
    await store.link(link(first.id))

    with pytest.raises(IdentityTakenError):
        await store.link(link(second.id))

    assert await store.resolve(TELEGRAM, ACCOUNT) == first.id


async def test_linking_an_account_again_to_its_own_person_is_not_a_conflict(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Coming back is not a collision — and a revoked identity revives."""
    user = await store.create_user()
    await store.link(link(user.id))
    await store.deactivate(TELEGRAM, ACCOUNT)

    revived = await store.link(link(user.id))

    assert revived.active
    assert await store.resolve(TELEGRAM, ACCOUNT) == user.id


async def test_a_person_keeps_everything_when_their_account_changes(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The whole point: the core id does not move, so dialogs, memories,
    skills and secrets follow without being touched."""
    user = await store.create_user()
    await store.link(link(user.id))

    await store.reseat(TELEGRAM, user.id, OTHER_ACCOUNT)

    assert await store.resolve(TELEGRAM, OTHER_ACCOUNT) == user.id
    assert await store.resolve(TELEGRAM, ACCOUNT) is None


async def test_reseating_onto_somebody_elses_account_is_refused(
    store: SqlAlchemyIdentityStore,
) -> None:
    mover = await store.create_user()
    occupant = await store.create_user()
    await store.link(link(mover.id))
    await store.link(link(occupant.id, IdentityKey(TELEGRAM, OTHER_ACCOUNT)))

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
    await store.link(link(user.id))

    await store.deactivate(TELEGRAM, ACCOUNT)

    assert await store.resolve(TELEGRAM, ACCOUNT) is None


async def test_a_revoked_account_keeps_its_history(store: SqlAlchemyIdentityStore) -> None:
    """Deleting the row would lose that it was ever used, and by whom."""
    user = await store.create_user()
    await store.link(link(user.id))
    await store.deactivate(TELEGRAM, ACCOUNT)

    identities = await store.identities_of(user.id)

    assert [(item.surface, item.external_id, item.active) for item in identities] == [
        (TELEGRAM, ACCOUNT, False)
    ]


async def test_a_person_can_be_known_on_several_surfaces(
    store: SqlAlchemyIdentityStore,
) -> None:
    user = await store.create_user()
    await store.link(link(user.id))
    await store.link(link(user.id, IdentityKey(WEB, "dmirain")))

    assert {item.surface for item in await store.identities_of(user.id)} == {TELEGRAM, WEB}
    assert await store.resolve(WEB, "dmirain") == user.id


async def test_surface_extras_ride_along_with_the_identity(
    store: SqlAlchemyIdentityStore,
) -> None:
    """A JSON column is right here — nothing about it needs enforcing — and
    wrong for the account itself, which needs a unique constraint."""
    user = await store.create_user()
    await store.link(link(user.id, details={"username": "someone"}))

    found = await store.find_by_identity(TELEGRAM, ACCOUNT)

    assert found is not None
    assert found.details == {"username": "someone"}


async def test_an_unknown_person_is_an_error_not_an_empty_user(
    store: SqlAlchemyIdentityStore,
) -> None:
    with pytest.raises(UserNotFoundError):
        await store.get_user("nobody")


async def test_first_contact_gives_the_newcomer_a_person(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Whether they may talk at all was decided before this — by the invite
    gate or the credential in front of the service."""
    user_id = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    assert user_id
    assert await store.resolve(TELEGRAM, ACCOUNT) == user_id


async def test_coming_back_is_the_same_person(store: SqlAlchemyIdentityStore) -> None:
    first = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    assert await store.resolve_or_create(TELEGRAM, ACCOUNT) == first


async def test_two_first_messages_at_once_make_one_person(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Otherwise a newcomer who sends twice quickly ends up as two people,
    each holding half of what they say."""
    ids = await asyncio.gather(*(store.resolve_or_create(TELEGRAM, ACCOUNT) for _ in range(4)))

    assert len(set(ids)) == 1


async def test_the_identity_mirrors_what_the_surface_calls_them(
    store: SqlAlchemyIdentityStore,
) -> None:
    """People rename themselves on a surface; the mirror must not go stale."""
    user = await store.create_user()
    await store.link(link(user.id))

    await store.update_profile(profile("Alice Smith", "alice"))

    found = await store.find_by_identity(TELEGRAM, ACCOUNT)
    assert found is not None
    assert (found.name, found.username) == ("Alice Smith", "alice")


async def test_the_first_surface_to_report_a_name_christens_the_person(
    store: SqlAlchemyIdentityStore,
) -> None:
    user = await store.create_user()
    await store.link(link(user.id))

    await store.update_profile(profile("Alice Smith", None))

    assert (await store.get_user(user.id)).name == "Alice Smith"


async def test_a_named_person_is_not_renamed_by_a_surface(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The canonical name is the person's own once set; only the identity's
    mirror follows a surface rename."""
    user = await store.create_user()
    await store.link(link(user.id))
    await store.update_profile(profile("Alice Smith", "alice"))

    await store.update_profile(profile("A. Smith", "alice2"))

    found = await store.find_by_identity(TELEGRAM, ACCOUNT)
    assert found is not None
    assert (found.name, found.username) == ("A. Smith", "alice2")
    assert (await store.get_user(user.id)).name == "Alice Smith"


async def test_a_surface_lists_everyone_it_knows_revoked_included(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The operator's roll call: one row per account, each naming its person."""
    first = await store.create_user()
    second = await store.create_user()
    await store.link(link(first.id))
    await store.link(link(second.id, IdentityKey(TELEGRAM, OTHER_ACCOUNT)))
    await store.link(link(second.id, IdentityKey(WEB, "dmirain")))
    await store.deactivate(TELEGRAM, OTHER_ACCOUNT)

    listed = await store.list_identities(TELEGRAM)

    assert [(item.external_id, item.user_id, item.active) for item in listed] == [
        (ACCOUNT, first.id, True),
        (OTHER_ACCOUNT, second.id, False),
    ]


async def test_a_profile_for_an_unknown_account_is_dropped(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Recording a profile is a courtesy, not a way to create an identity."""
    await store.update_profile(profile("Alice Smith", "alice"))

    assert await store.find_by_identity(TELEGRAM, ACCOUNT) is None


async def test_first_contact_never_attaches_itself_to_somebody(
    store: SqlAlchemyIdentityStore,
) -> None:
    """Linking to an existing person is what an invite does. If arriving could
    do it, a mistake would hand a stranger their dialogs."""
    existing = await store.create_user()
    await store.link(link(existing.id, IdentityKey(WEB, "dmirain")))

    newcomer = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    assert newcomer != existing.id


async def test_everyone_is_born_waiting(store: SqlAlchemyIdentityStore) -> None:
    user_id = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    assert (await store.get_user(user_id)).status is UserStatus.WAITING


async def test_a_free_slot_activates_and_a_full_house_does_not(
    store: SqlAlchemyIdentityStore,
) -> None:
    first = await store.resolve_or_create(TELEGRAM, ACCOUNT)
    second = await store.resolve_or_create(TELEGRAM, OTHER_ACCOUNT)

    assert await store.try_activate(first, 1)
    assert not await store.try_activate(second, 1)  # the house is full
    assert (await store.get_user(first)).status is UserStatus.ACTIVE
    assert (await store.get_user(second)).status is UserStatus.WAITING
    assert await store.try_activate(second, 2)  # a raised cap frees a slot


async def test_no_cap_activates_everyone_and_zero_activates_nobody(
    store: SqlAlchemyIdentityStore,
) -> None:
    unlimited = await store.resolve_or_create(TELEGRAM, ACCOUNT)
    frozen = await store.resolve_or_create(TELEGRAM, OTHER_ACCOUNT)

    assert await store.try_activate(unlimited, None)
    assert not await store.try_activate(frozen, 0)  # 0 = the pause button


async def test_only_the_waiting_are_ever_touched_by_activation(
    store: SqlAlchemyIdentityStore,
) -> None:
    user_id = await store.resolve_or_create(TELEGRAM, ACCOUNT)
    await store.set_status(user_id, UserStatus.BANNED)

    assert not await store.try_activate(user_id, None)  # a ban is not a queue
    assert (await store.get_user(user_id)).status is UserStatus.BANNED


async def test_set_status_is_the_operators_unconditional_hand(
    store: SqlAlchemyIdentityStore,
) -> None:
    user_id = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    await store.set_status(user_id, UserStatus.ACTIVE)  # past any cap on purpose
    assert (await store.get_user(user_id)).status is UserStatus.ACTIVE

    with pytest.raises(UserNotFoundError):
        await store.set_status("nobody", UserStatus.BANNED)


async def test_count_by_status_names_every_status(store: SqlAlchemyIdentityStore) -> None:
    active = await store.resolve_or_create(TELEGRAM, ACCOUNT)
    await store.resolve_or_create(TELEGRAM, OTHER_ACCOUNT)
    await store.set_status(active, UserStatus.ACTIVE)

    counts = await store.count_by_status()

    assert counts == {UserStatus.ACTIVE: 1, UserStatus.WAITING: 1, UserStatus.BANNED: 0}


async def test_admit_promotes_at_the_door_and_settles_the_rest(
    store: SqlAlchemyIdentityStore,
) -> None:
    """The AccessService flow: born waiting, admitted when a slot frees."""
    cap_holder: list[int | None] = [0]

    async def read_cap() -> int | None:
        return cap_holder[0]

    access = AccessService(store, read_cap)
    user_id = await store.resolve_or_create(TELEGRAM, ACCOUNT)

    assert await access.admit(user_id) is UserStatus.WAITING
    cap_holder[0] = 1
    assert await access.admit(user_id) is UserStatus.ACTIVE
    assert await access.admit(user_id) is UserStatus.ACTIVE  # settled: no cap read needed

    await store.set_status(user_id, UserStatus.BANNED)
    assert await access.admit(user_id) is UserStatus.BANNED
