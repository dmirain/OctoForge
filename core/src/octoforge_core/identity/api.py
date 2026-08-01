"""Public boundary of the identity module: one person, several surfaces.

A user used to *be* their transport handle — `tg:12345` was both who they were
and where to write to them. That made changing a Telegram account impossible:
everything they owned was filed under the old handle, with nowhere to say the
new one is the same person.

So a user has an id of their own, opaque on purpose, and each surface records
what *it* calls them. Re-seating then moves an identity rather than a user:
the core id never changes, and everything filed under it follows for free.

The opacity is load-bearing. An id that can be parsed will be parsed — the
delivery address used to be read straight back out of it — and an id that
carries no structure makes that impossible rather than merely discouraged.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class UserNotFoundError(Exception):
    """Raised when a user id does not resolve to a stored user."""


class IdentityNotFoundError(Exception):
    """Raised when a (surface, external id) pair is not linked to anyone."""


class IdentityTakenError(Exception):
    """Raised when a (surface, external id) pair already belongs to somebody.

    The one invariant this module exists to hold: an account belongs to at
    most one person. Silently reassigning it would deliver one person's
    messages to another.
    """


@dataclass(frozen=True, slots=True)
class User:
    """A person, independent of any surface they talk from."""

    id: str
    #: The cross-cutting identifier once registration exists. Empty until then;
    #: it is the account itself, not a surface, which is why it lives here.
    email: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """What one surface calls a user, and what it needs to reach them."""

    user_id: str
    surface: str
    external_id: str
    #: Surface-specific extras. A JSON column is right *here* — nothing about
    #: it needs enforcing — and wrong for the identity itself, which needs a
    #: unique constraint.
    details: dict[str, Any] = field(default_factory=dict)
    #: A revoked identity keeps its history instead of vanishing.
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


UserIdentityList = list[UserIdentity]


class IdentityStore(Protocol):
    """Port over users and the surface identities pointing at them."""

    async def resolve(self, surface: str, external_id: str) -> str | None:
        """The user this account belongs to, or None when nobody claims it.

        The hot path: every incoming message asks this. An inactive identity
        resolves to nobody — a revoked account is not a way in.
        """
        ...

    async def create_user(self, email: str = "") -> User:
        """Mint a person with an id of their own."""
        ...

    async def get_user(self, user_id: str) -> User:
        """Return the user or raise `UserNotFoundError`."""
        ...

    async def link(
        self, user_id: str, surface: str, external_id: str, details: dict[str, Any] | None = None
    ) -> UserIdentity:
        """Attach an account to a person.

        Raises `IdentityTakenError` when that account already belongs to
        somebody else — the whole point of the unique constraint, surfaced as
        an error rather than an overwrite.
        """
        ...

    async def reseat(self, surface: str, user_id: str, external_id: str) -> UserIdentity:
        """Point this person's identity on a surface at a different account.

        What "move me to my new Telegram" means. The core id does not change,
        so dialogs, memories, skills and secrets follow without being touched.
        Raises `IdentityTakenError` if the new account is somebody else's, and
        `IdentityNotFoundError` if this person has no identity there to move.
        """
        ...

    async def deactivate(self, surface: str, external_id: str) -> None:
        """Revoke an account without erasing that it was once used."""
        ...

    async def identities_of(self, user_id: str) -> UserIdentityList:
        """Every surface this person is known on, revoked ones included."""
        ...

    async def find_by_identity(self, surface: str, external_id: str) -> UserIdentity | None:
        """The identity row itself, active or not (None when unknown)."""
        ...
