"""Surface profile mirroring across identity and person rows."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.identity._account_queries import find_identity_row
from octoforge_core.identity.models import UserRow
from octoforge_core.identity.types import IdentityProfile
from octoforge_core.time import utc_now


class IdentityProfileStore:
    """Refresh an account mirror and seed an empty canonical person name."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def update(self, profile: IdentityProfile) -> None:
        async with write_session(self._session_factory) as session:
            identity = await find_identity_row(session, profile.key)
            if identity is None:
                return
            if identity.name != profile.name or identity.username != profile.username:
                identity.name = profile.name
                identity.username = profile.username
                identity.updated_at = utc_now()
            if not profile.name:
                return
            user = await session.get(UserRow, identity.user_id)
            if user is not None and not user.name:
                user.name = profile.name
