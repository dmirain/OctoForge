"""Public identity boundary: one opaque person id with several surface accounts."""

from typing import Protocol

from octoforge_core.identity.types import (
    IdentityKey,
    IdentityLink,
    IdentityNotFoundError,
    IdentityProfile,
    IdentityTakenError,
    User,
    UserIdentity,
    UserIdentityList,
    UserList,
    UserNotFoundError,
    UserStatus,
)

__all__ = [
    "IdentityKey",
    "IdentityLink",
    "IdentityNotFoundError",
    "IdentityProfile",
    "IdentityStore",
    "IdentityTakenError",
    "User",
    "UserIdentity",
    "UserIdentityList",
    "UserList",
    "UserNotFoundError",
    "UserStatus",
]


class IdentityStore(Protocol):
    """Port over users and the surface identities pointing at them."""

    async def resolve(self, surface: str, external_id: str) -> str | None:
        """Return the active account's person, or None."""
        ...

    async def resolve_or_create(self, surface: str, external_id: str) -> str:
        """Return the account's person, minting one safely on first contact."""
        ...

    async def create_user(self, email: str = "") -> User:
        """Mint a person with an id of their own."""
        ...

    async def get_user(self, user_id: str) -> User:
        """Return the user or raise `UserNotFoundError`."""
        ...

    async def link(self, request: IdentityLink) -> UserIdentity:
        """Attach an unclaimed account to a person."""
        ...

    async def reseat(self, surface: str, user_id: str, external_id: str) -> UserIdentity:
        """Move a person's identity to an unclaimed account on that surface."""
        ...

    async def deactivate(self, surface: str, external_id: str) -> None:
        """Revoke an account without erasing that it was once used."""
        ...

    async def update_profile(self, profile: IdentityProfile) -> None:
        """Refresh a known account's profile and seed an empty canonical name."""
        ...

    async def set_status(self, user_id: str, status: UserStatus) -> None:
        """Set the person's status unconditionally as an operator action."""
        ...

    async def try_activate(self, user_id: str, max_active: int | None) -> bool:
        """Atomically activate a waiting person when the cap has room."""
        ...

    async def count_by_status(self) -> dict[UserStatus, int]:
        """How many people hold each status (operator console, admission)."""
        ...

    async def list_users(self) -> UserList:
        """Everyone the installation knows, newest first (operator console)."""
        ...

    async def identities_of(self, user_id: str) -> UserIdentityList:
        """Every surface this person is known on, revoked ones included."""
        ...

    async def list_identities(self, surface: str) -> UserIdentityList:
        """List one surface's accounts, including revoked ones, oldest first."""
        ...

    async def find_by_identity(self, surface: str, external_id: str) -> UserIdentity | None:
        """The identity row itself, active or not (None when unknown)."""
        ...
