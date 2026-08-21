"""SQL adapter for the public identity store port."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.identity._account_commands import IdentityAccountCommands
from octoforge_core.identity._account_queries import IdentityAccountQueries
from octoforge_core.identity._profile_store import IdentityProfileStore
from octoforge_core.identity._user_store import UserStore
from octoforge_core.identity.api import (
    IdentityKey,
    IdentityLink,
    IdentityProfile,
    IdentityTakenError,
    User,
    UserIdentity,
    UserIdentityList,
    UserList,
    UserStatus,
)


class SqlAlchemyIdentityStore:
    """Users and the surface identities pointing at them."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._users = UserStore(session_factory)
        self._queries = IdentityAccountQueries(session_factory)
        self._commands = IdentityAccountCommands(session_factory)
        self._profiles = IdentityProfileStore(session_factory)

    async def resolve(self, surface: str, external_id: str) -> str | None:
        return await self._queries.resolve(IdentityKey(surface, external_id))

    async def resolve_or_create(self, surface: str, external_id: str) -> str:
        """Resolve an account, minting one person safely on first contact."""
        key = IdentityKey(surface, external_id)
        found = await self._queries.resolve(key)
        if found is not None:
            return found
        user = await self._users.create()
        try:
            await self._commands.link(IdentityLink(user.id, key))
        except IdentityTakenError:
            owner = await self._queries.resolve(key)
            if owner is None:
                raise
            return owner
        return user.id

    async def create_user(self, email: str = "") -> User:
        return await self._users.create(email)

    async def get_user(self, user_id: str) -> User:
        return await self._users.get(user_id)

    async def link(self, request: IdentityLink) -> UserIdentity:
        return await self._commands.link(request)

    async def reseat(self, surface: str, user_id: str, external_id: str) -> UserIdentity:
        return await self._commands.reseat(surface, user_id, external_id)

    async def deactivate(self, surface: str, external_id: str) -> None:
        await self._commands.deactivate(IdentityKey(surface, external_id))

    async def update_profile(self, profile: IdentityProfile) -> None:
        await self._profiles.update(profile)

    async def set_status(self, user_id: str, status: UserStatus) -> None:
        await self._users.set_status(user_id, status)

    async def try_activate(self, user_id: str, max_active: int | None) -> bool:
        return await self._users.try_activate(user_id, max_active)

    async def count_by_status(self) -> dict[UserStatus, int]:
        return await self._users.count_by_status()

    async def list_users(self) -> UserList:
        return await self._users.list()

    async def identities_of(self, user_id: str) -> UserIdentityList:
        return await self._queries.of_user(user_id)

    async def list_identities(self, surface: str) -> UserIdentityList:
        return await self._queries.list(surface)

    async def find_by_identity(self, surface: str, external_id: str) -> UserIdentity | None:
        return await self._queries.find(IdentityKey(surface, external_id))
