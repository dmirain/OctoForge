"""MCP server store: one row per URL, links for the people who added it."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.mcp.api import McpServer, McpServerNameTakenError
from octoforge_core.mcp.store import SqlAlchemyMcpServerStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
URL = "https://mcp.example.com/mcp"
OTHER_URL = "https://mcp.other.example/mcp"


@pytest.fixture
async def stores() -> AsyncIterator[tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    factory = create_session_factory(engine)
    yield SqlAlchemyMcpServerStore(factory), SqlAlchemyIdentityStore(factory)
    await engine.dispose()


def make_server(name: str = "example", url: str = URL) -> McpServer:
    return McpServer(id=uuid.uuid4().hex, name=name, url=url)


async def test_two_people_adding_one_url_share_one_row(
    stores: tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore],
) -> None:
    """The deduplication the table exists for."""
    store, identities = stores
    first = await identities.create_user()
    second = await identities.create_user()

    added = await store.add(make_server(), first.id)
    again = await store.add(make_server(name="other-name"), second.id)

    assert again.id == added.id
    assert again.name == "example"  # the first name won; the URL is the identity
    assert (await store.list_all()) == [again]
    assert set(await store.list_user_ids(added.id)) == {first.id, second.id}


async def test_adding_twice_is_idempotent(
    stores: tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore],
) -> None:
    store, identities = stores
    user = await identities.create_user()

    added = await store.add(make_server(), user.id)
    again = await store.add(make_server(), user.id)

    assert again.id == added.id
    assert await store.list_user_ids(added.id) == [user.id]


async def test_a_taken_name_with_another_url_is_refused(
    stores: tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore],
) -> None:
    """Mirror records address the server by name: one name, one server."""
    store, identities = stores
    user = await identities.create_user()
    await store.add(make_server(), user.id)

    with pytest.raises(McpServerNameTakenError):
        await store.add(make_server(url=OTHER_URL), user.id)


async def test_concurrent_first_adds_converge_on_one_row(
    stores: tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore],
) -> None:
    store, identities = stores
    users = [await identities.create_user() for _ in range(4)]

    added = await asyncio.gather(*(store.add(make_server(), user.id) for user in users))

    assert len({server.id for server in added}) == 1
    assert len(await store.list_all()) == 1


async def test_sync_outcome_is_recorded(
    stores: tuple[SqlAlchemyMcpServerStore, SqlAlchemyIdentityStore],
) -> None:
    store, identities = stores
    user = await identities.create_user()
    added = await store.add(make_server(), user.id)

    await store.mark_synced(added.id, "boom")
    failed = await store.get_by_name("example")
    await store.mark_synced(added.id, None)
    healed = await store.get_by_name("example")

    assert failed is not None and failed.last_sync_error == "boom"
    assert healed is not None and healed.last_sync_error is None
    assert healed.last_synced_at is not None
