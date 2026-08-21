"""HTTP profile mirror used by the standalone ingestion process."""

import httpx
from octoforge_core.identity.api import IdentityProfile

from octoforge_telegram.api_gateway import CHANNEL_HEADER, USER_ID_HEADER

PROFILE_PATH = "/api/identity/profile"


class ApiProfileMirror:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def update_profile(self, profile: IdentityProfile) -> None:
        response = await self._client.put(
            PROFILE_PATH,
            json={"name": profile.name, "username": profile.username},
            headers={
                USER_ID_HEADER: profile.key.external_id,
                CHANNEL_HEADER: profile.key.surface,
            },
        )
        response.raise_for_status()
